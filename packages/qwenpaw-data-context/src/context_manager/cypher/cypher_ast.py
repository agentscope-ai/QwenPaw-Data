"""Parse-tree analysis for the deliberately small Scoped Cypher subset."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from antlr4 import CommonTokenStream, InputStream, ParserRuleContext, Token
from antlr4.error.ErrorListener import ErrorListener
from antlr4_cypher import CypherLexer, CypherParser

from ..schema import GRAPH_SCHEMA


class CypherAstError(ValueError):
    """The query is invalid or outside the supported read-only subset."""


@dataclass(frozen=True)
class MatchClause:
    """Source locations and variables for one MATCH parse-tree node."""

    node_variables: tuple[str, ...]
    relationship_variables: tuple[str, ...]
    pattern_end: int
    where_expression_start: int | None = None
    where_expression_end: int | None = None


@dataclass(frozen=True)
class ParsedScopedQuery:
    matches: tuple[MatchClause, ...]
    relationship_edits: tuple[tuple[int, str], ...] = ()


class _CollectingErrorListener(ErrorListener):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[str] = []

    def syntaxError(
        self,
        recognizer: object,
        offendingSymbol: object,
        line: int,
        column: int,
        msg: str,
        e: Exception | None,
    ) -> None:
        self.errors.append(f"line {line}:{column} {msg}")


def _contexts(
    root: ParserRuleContext,
    context_type: type[ParserRuleContext],
) -> Iterator[ParserRuleContext]:
    stack: list[object] = [root]
    while stack:
        item = stack.pop()
        if isinstance(item, context_type):
            yield item
        children = getattr(item, "children", None)
        if children:
            stack.extend(reversed(children))


def _has_ancestor(
    context: ParserRuleContext,
    context_type: type[ParserRuleContext],
) -> bool:
    parent = context.parentCtx
    while parent is not None:
        if isinstance(parent, context_type):
            return True
        parent = parent.parentCtx
    return False


def _reject_unsupported_nodes(
    tree: CypherParser.ScriptContext, *, allow_fulltext: bool
) -> None:
    blocked: tuple[tuple[type[ParserRuleContext], str], ...] = (
        (CypherParser.StandaloneCallContext, "CALL is not supported"),
        (CypherParser.QueryCallStContext, "CALL is not supported"),
        (CypherParser.UpdatingStatementContext, "Write clauses are not supported"),
        (
            CypherParser.PatternComprehensionContext,
            "Pattern comprehensions are not supported",
        ),
        (CypherParser.SubqueryExistContext, "EXISTS subqueries are not supported"),
    )
    for context_type, message in blocked:
        if allow_fulltext and context_type is CypherParser.QueryCallStContext:
            continue
        if next(_contexts(tree, context_type), None) is not None:
            raise CypherAstError(message)

    # A relationship-chain expression outside the MATCH pattern (including one
    # hidden in that MATCH's WHERE expression) has no safe insertion point.
    for chain in _contexts(tree, CypherParser.RelationshipsChainPatternContext):
        if not _has_ancestor(chain, CypherParser.MatchStContext) or not _has_ancestor(
            chain, CypherParser.PatternContext
        ):
            raise CypherAstError("Graph pattern expressions are not supported")


def _analyse_match(
    context: CypherParser.MatchStContext,
    used_symbols: set[str],
    relationship_edits: list[tuple[int, str]],
) -> MatchClause:
    pattern_where = context.patternWhere()
    pattern = pattern_where.pattern()
    node_variables: list[str] = []
    seen_nodes: set[str] = set()
    relationship_variables: list[str] = []
    seen_relationships: set[str] = set()

    for node in _contexts(pattern, CypherParser.NodePatternContext):
        symbol = node.symbol()
        if symbol is None:
            raise CypherAstError("Anonymous nodes are not supported")
        spelling = symbol.getText()
        key = spelling.strip("`")
        if key not in seen_nodes:
            seen_nodes.add(key)
            node_variables.append(spelling)

    # Every relationship must have a scalar variable so the same tenant/owner
    # predicate can be applied. Variable-length relationships bind a list and
    # therefore cannot use the scalar property predicate safely.
    for relationship in _contexts(pattern, CypherParser.RelationshipPatternContext):
        detail = relationship.relationDetail()
        if detail is not None and detail.rangeLit() is not None:
            raise CypherAstError("Variable-length relationships are not supported")
        if detail is None or detail.symbol() is None:
            index = len(relationship_edits)
            spelling = f"__scoped_rel_{index}"
            while spelling in used_symbols:
                index += 1
                spelling = f"__scoped_rel_{index}"
            used_symbols.add(spelling)
            if detail is None:
                position = relationship.SUB(0).getSymbol().stop + 1
                insertion = f"[{spelling}]"
            else:
                position = detail.start.stop + 1
                insertion = spelling
            relationship_edits.append((position, insertion))
        else:
            spelling = detail.symbol().getText()
        key = spelling.strip("`")
        if key not in seen_relationships:
            seen_relationships.add(key)
            relationship_variables.append(spelling)

    if not node_variables:
        raise CypherAstError("MATCH must contain at least one named node")

    where = pattern_where.where()
    if where is None:
        return MatchClause(
            node_variables=tuple(node_variables),
            relationship_variables=tuple(relationship_variables),
            pattern_end=pattern.stop.stop + 1,
        )

    expression = where.expression()
    return MatchClause(
        node_variables=tuple(node_variables),
        relationship_variables=tuple(relationship_variables),
        pattern_end=pattern.stop.stop + 1,
        where_expression_start=expression.start.start,
        where_expression_end=expression.stop.stop + 1,
    )


def parse_cypher(cypher: str) -> CypherParser.ScriptContext:
    """Parse once; read/write admission policies inspect the resulting tree."""

    if not cypher.strip():
        raise CypherAstError("Empty Cypher query")

    lexer_errors = _CollectingErrorListener()
    lexer = CypherLexer(InputStream(cypher))
    lexer.removeErrorListeners()
    lexer.addErrorListener(lexer_errors)

    tokens = CommonTokenStream(lexer)
    tokens.fill()
    # The upstream grammar omits whitespace before UNION; preserve source offsets.
    for previous, current in zip(tokens.tokens, tokens.tokens[1:]):
        if previous.type == CypherLexer.SP and current.type == CypherLexer.UNION:
            previous.channel = Token.HIDDEN_CHANNEL
    parser_errors = _CollectingErrorListener()
    parser = CypherParser(tokens)
    parser.removeErrorListeners()
    parser.addErrorListener(parser_errors)
    tree = parser.script()

    if lexer_errors.errors or parser_errors.errors:
        details = (lexer_errors.errors + parser_errors.errors)[0]
        raise CypherAstError(f"Invalid or unsupported Cypher syntax: {details}")

    return tree


def analyse_scoped_query(
    tree: CypherParser.ScriptContext, *, allow_fulltext: bool = False
) -> ParsedScopedQuery:
    """Find scope edit points; full-text calls must already pass admission."""

    _reject_unsupported_nodes(tree, allow_fulltext=allow_fulltext)

    used_symbols = {
        symbol.getText().strip("`").replace("``", "`")
        for symbol in _contexts(tree, CypherParser.SymbolContext)
    }
    relationship_edits: list[tuple[int, str]] = []
    matches = [
        _analyse_match(clause, used_symbols, relationship_edits)
        for clause in _contexts(tree, CypherParser.MatchStContext)
    ]
    # Internal bindings must not change wildcard columns or grouping semantics.
    if relationship_edits and any(
        projection.MULT() is not None
        for projection in _contexts(tree, CypherParser.ProjectionItemsContext)
    ):
        raise CypherAstError(
            "Anonymous relationships require explicit WITH and RETURN projections"
        )
    if allow_fulltext:
        for call in _contexts(tree, CypherParser.QueryCallStContext):
            yielded = call.yieldItems()
            variable = next(
                item.symbol()[-1].getText()
                for item in yielded.yieldItem()
                if item.symbol(0).getText().strip("`") == "node"
            )
            where = yielded.where()
            expression = where.expression() if where is not None else None
            matches.append(
                MatchClause(
                    node_variables=(variable,),
                    relationship_variables=(),
                    pattern_end=yielded.stop.stop + 1,
                    where_expression_start=expression.start.start
                    if expression
                    else None,
                    where_expression_end=expression.stop.stop + 1
                    if expression
                    else None,
                )
            )

    return ParsedScopedQuery(
        matches=tuple(matches), relationship_edits=tuple(relationship_edits)
    )


def parse_scoped_query(cypher: str) -> ParsedScopedQuery:
    """Parse and validate a read-only query, returning AST-derived edit points."""
    return analyse_scoped_query(parse_cypher(cypher))


def scope_cypher(
    cypher: str,
    *,
    workspace_id: str,
    user_id: str,
    is_admin: bool,
    global_mg_only: bool = False,
    include_global: bool = True,
) -> tuple[str, dict]:
    """Apply develop's node and relationship predicates at AST edit points."""
    return scope_parsed_query(
        cypher,
        parse_scoped_query(cypher),
        workspace_id=workspace_id,
        user_id=user_id,
        is_admin=is_admin,
        global_mg_only=global_mg_only,
        include_global=include_global,
    )


def scope_parsed_query(
    cypher: str,
    parsed: ParsedScopedQuery,
    *,
    workspace_id: str,
    user_id: str,
    is_admin: bool,
    global_mg_only: bool = False,
    include_global: bool = True,
) -> tuple[str, dict]:
    """Inject scope into the query from which these edit points were derived."""
    edits = list(parsed.relationship_edits)
    params: dict = {}
    for match in parsed.matches:
        fragments = []
        for variable in (*match.node_variables, *match.relationship_variables):
            predicate = f"{variable}.workspace_id = $scoped_ws_id"
            params["scoped_ws_id"] = workspace_id
            if variable in match.node_variables:
                predicate += "".join(
                    f" AND NOT {variable}:{spec.name}"
                    for spec in GRAPH_SCHEMA.nodes
                    if spec.internal
                )
            if global_mg_only and is_admin:
                predicate += f" AND {variable}.owner_id = '__global__'"
            elif not is_admin:
                owner_predicate = f"{variable}.owner_id = $scoped_uid"
                if include_global:
                    owner_predicate += f" OR {variable}.owner_id = '__global__'"
                predicate += f" AND ({owner_predicate})"
                params["scoped_uid"] = user_id
            fragments.append(f"({predicate})")
        scope_predicate = " AND ".join(fragments)
        if match.where_expression_start is None:
            edits.append((match.pattern_end, f" WHERE {scope_predicate}"))
        else:
            edits.append((match.where_expression_start, f"{scope_predicate} AND ("))
            edits.append((match.where_expression_end, ")"))

    for position, text in sorted(edits, key=lambda edit: edit[0], reverse=True):
        cypher = cypher[:position] + text + cypher[position:]
    return cypher, params
