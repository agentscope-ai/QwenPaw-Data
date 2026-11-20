"""Prove native read scope before executing model-authored statements."""

from __future__ import annotations

import re
from collections.abc import Mapping

import sqlglot
from antlr4 import ParserRuleContext
from antlr4_cypher import CypherParser as P
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from .cypher_ast import CypherAstError, _contexts, parse_cypher

_CYPHER_FUNCTIONS = frozenset(
    [
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "collect",
        "coalesce",
        "tostring",
        "tointeger",
        "tofloat",
        "toboolean",
        "size",
        "length",
        "tolower",
        "toupper",
        "trim",
        "ltrim",
        "rtrim",
        "replace",
        "substring",
        "left",
        "right",
        "split",
        "abs",
        "ceil",
        "ceiling",
        "floor",
        "round",
        "sign",
        "sqrt",
        "log",
        "log10",
        "exp",
        "head",
        "last",
        "tail",
        "reverse",
        "range",
        "keys",
        "properties",
        "labels",
        "label",
        "type",
        "id",
        "elementid",
        "nodes",
        "relationships",
        "startnode",
        "endnode",
        "timestamp",
        "date",
        "datetime",
        "duration",
    ]
)
_SQL_FUNCTIONS = frozenset(
    [
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "coalesce",
        "nullif",
        "greatest",
        "least",
        "abs",
        "ceil",
        "floor",
        "round",
        "lower",
        "upper",
        "length",
        "char_length",
        "substring",
        "substr",
        "trim",
        "ltrim",
        "rtrim",
        "replace",
        "concat",
        "concat_ws",
        "split_part",
        "array_length",
        "array_ndims",
        "cardinality",
        "unnest",
        "json_build_object",
        "json_build_array",
        "jsonb_build_object",
        "jsonb_build_array",
        "json_agg",
        "jsonb_agg",
        "array_agg",
        "array",
        "to_json",
        "to_jsonb",
        "json_extract",
        "json_extract_scalar",
        "agtype_to_json",
        "agtype_object_field_text",
        "text_search",
        "cosine_distance",
        "l2_distance",
        "ip_distance",
        "approx_cosine_distance",
    ]
)
_SQL_WRITES = (exp.DML, exp.DDL, exp.Command, exp.Transaction, exp.Lock, exp.Into)


class GraphQueryError(ValueError):
    """The query is invalid or its read scope cannot be established."""


def _admit_fulltext(
    call: P.QueryCallStContext,
    params: Mapping[str, object],
    indexes: Mapping[str, str],
) -> None:
    """Allow declared full-text indexes with an explicit node output to scope."""
    if call.invocationName().getText() != "db.index.fulltext.queryNodes":
        raise GraphQueryError("Only scoped full-text CALL is admitted")
    chain = call.parenExpressionChain().expressionChain()
    args = chain.expression() if chain is not None else []
    index = args[0].getText() if args else ""
    if re.fullmatch(r"\$(?:[A-Za-z_][A-Za-z_0-9]*|`[^`]+`)", index):
        index = params.get(index[1:].strip("`"))
    elif len(index) >= 2 and index[0] in {"'", '"'} and index[-1] == index[0]:
        index = index[1:-1]
    else:
        index = None
    if len(args) != 2 or not isinstance(index, str) or index not in indexes:
        raise GraphQueryError(
            "Full-text search requires a declared index and two arguments"
        )
    yielded = call.yieldItems()
    names = (
        {item.symbol(0).getText().strip("`") for item in yielded.yieldItem()}
        if yielded is not None
        else set()
    )
    if "node" not in names or not names <= {"node", "score"}:
        raise GraphQueryError("Full-text search must yield node and optionally score")


def admit_cypher(
    query: str,
    params: Mapping[str, object],
    *,
    fulltext_indexes: Mapping[str, str] | None = None,
) -> P.ScriptContext:
    """Reject writes and calls that could escape the native read boundary."""
    try:
        tree = parse_cypher(query)
    except CypherAstError as exc:
        raise GraphQueryError(str(exc)) from exc
    for node in _contexts(tree, ParserRuleContext):
        if isinstance(node, (P.UpdatingStatementContext, P.StandaloneCallContext)):
            raise GraphQueryError("Writes and CALL cannot be admitted as graph reads")
        if isinstance(node, P.QueryCallStContext):
            if fulltext_indexes is None:
                raise GraphQueryError("CALL cannot be admitted as a graph read")
            _admit_fulltext(node, params, fulltext_indexes)
        if (
            isinstance(node, P.FunctionInvocationContext)
            and node.invocationName().getText().lower() not in _CYPHER_FUNCTIONS
            and not (
                fulltext_indexes is not None
                and node.invocationName().getText().lower()
                in {"vector.similarity.cosine", "vector.similarity.euclidean"}
            )
        ):
            raise GraphQueryError("The query calls a function outside the read subset")
    return tree


def admit_age_sql(
    query: str,
    *,
    graph: str,
) -> None:
    """Allow native SELECTs only within the authorized graph and read functions."""
    try:
        statements = sqlglot.parse(query, read="postgres")
    except SqlglotError as exc:
        raise GraphQueryError("Invalid AGE SQL") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise GraphQueryError("AGE SQL must be one read-only query")
    statement = statements[0]
    if any(isinstance(node, _SQL_WRITES) for node in statement.walk()):
        raise GraphQueryError("Writes, locks and transaction commands are forbidden")
    cte_references = {
        node
        for sql_scope in traverse_scope(statement)
        for node, source in sql_scope.selected_sources.values()
        if isinstance(source, Scope)
    }
    for table in statement.find_all(exp.Table):
        if isinstance(table.this, exp.Anonymous):
            if (
                table.this.name.lower() != "cypher"
                or table.db != "ag_catalog"
                or table.catalog
            ):
                raise GraphQueryError(
                    "Only ag_catalog.cypher is allowed as a graph table function"
                )
            continue
        if table in cte_references:
            continue
        if table.catalog or table.db != graph:
            raise GraphQueryError(
                "SQL references a table outside the authorized graph schema"
            )
    for function in statement.find_all(exp.Func):
        namespace = (
            function.parent.this.name if isinstance(function.parent, exp.Dot) else ""
        )
        if namespace not in {"", "ag_catalog", "pg_catalog"}:
            raise GraphQueryError("SQL function schema is not permitted")
        # CASE/WHEN and casts are syntax. JSON_AGG has no SQL-name alias in SQLGlot.
        if isinstance(function, (exp.Cast, exp.Case, exp.JSONArrayAgg)) or (
            isinstance(function, exp.If) and isinstance(function.parent, exp.Case)
        ):
            continue
        names = (
            {function.name.lower()}
            if isinstance(function, exp.Anonymous)
            else {name.lower() for name in function.sql_names()}
        )
        if "cypher" not in names:
            if not names & _SQL_FUNCTIONS:
                raise GraphQueryError("SQL calls a function outside the read subset")
            continue
        table = function.parent
        if not isinstance(table, exp.Table) or table.db != "ag_catalog":
            raise GraphQueryError("cypher must be used as an ag_catalog table function")
        args = function.expressions
        if len(args) not in {2, 3} or not all(
            isinstance(arg, exp.RawString)
            or isinstance(arg, exp.Literal)
            and arg.is_string
            for arg in args[:2]
        ):
            raise GraphQueryError("cypher requires literal graph and query arguments")
        if args[0].this != graph:
            raise GraphQueryError("Cypher targets an unauthorized graph schema")
        admit_cypher(args[1].this, {})
    for kind in statement.find_all(exp.DataType):
        if kind.this == exp.DataType.Type.USERDEFINED:
            name = kind.args.get("kind")
            spelling = (
                name.sql(dialect="postgres")
                if isinstance(name, exp.Expression)
                else str(name)
            )
            if spelling.lower() not in {
                "agtype",
                "ag_catalog.agtype",
                "graphid",
                "ag_catalog.graphid",
            }:
                raise GraphQueryError(
                    "SQL casts may only use built-in or AGE value types"
                )
