"""Admit a deliberately bounded Raw Cypher subset using the Cypher parser."""

import re

from antlr4 import ParserRuleContext
from antlr4_cypher import CypherParser as P

from ....cypher.cypher_ast import (
    CypherAstError,
    analyse_scoped_query,
    parse_cypher,
    scope_parsed_query,
)
from ....schema import GRAPH_SCHEMA

WORKSPACE_PARAM = "__raw_workspace_id"
OWNER_PARAM = "__raw_owner_id"
WRITABLE_LABELS = frozenset({"Entity", "Event"})
WRITABLE_RELATIONS = frozenset(
    edge.relationship
    for edge in GRAPH_SCHEMA.edges
    if edge.source.name in WRITABLE_LABELS and edge.target.name in WRITABLE_LABELS
)
_RESERVED = {"workspace_id", "owner_id", "key", "zone", "datasource_id"}


class RawQueryViolation(ValueError):
    def __init__(self, code, message, status_code=403):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def contexts(root, kind):
    if isinstance(root, kind):
        yield root
    for child in getattr(root, "children", ()) or ():
        yield from contexts(child, kind)


def _reject(message):
    raise RawQueryViolation("WRITE_BLOCKED", message)


def admit(cypher, *, workspace_id, user_id):
    try:
        tree = parse_cypher(cypher)
    except CypherAstError as exc:
        raise RawQueryViolation(
            "INVALID_CYPHER", "Cypher 语法不合法或不在支持范围内", 400
        ) from exc
    # Semicolon tokens are forbidden; semicolons in literal text remain valid.
    if any(node.getText() == ";" for node in _terminals(tree)):
        raise RawQueryViolation("MULTI_STATEMENT", "仅支持单条 Cypher")
    # Custom functions could hide effects or unscoped graph access.
    functions = {
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
        "size",
        "tolower",
        "toupper",
        "trim",
        "elementid",
        "id",
        "labels",
        "type",
        "properties",
        "keys",
    }
    for function in contexts(tree, P.FunctionInvocationContext):
        if (
            function.invocationName().getText().lower() not in functions
            or function.patternElem()
        ):
            _reject("Raw Cypher 包含不支持的函数")
    is_write = next(contexts(tree, P.UpdatingStatementContext), None) is not None
    if not is_write:
        try:
            statement, bound = scope_parsed_query(
                cypher,
                analyse_scoped_query(tree),
                workspace_id=workspace_id,
                user_id=user_id,
                is_admin=True,
            )
        except CypherAstError as exc:
            raise RawQueryViolation("SCOPE_ERROR", str(exc), 400) from exc
        return False, statement, bound
    for kind in (
        P.WithStContext,
        P.UnionStContext,
        P.StandaloneCallContext,
        P.QueryCallStContext,
        P.UnwindStContext,
        P.PatternComprehensionContext,
        P.SubqueryExistContext,
        P.RelationshipsChainPatternContext,
    ):
        if next(contexts(tree, kind), None) is not None:
            _reject(
                "Raw 写入不支持 WITH / UNION / CALL / UNWIND / 子查询或图模式表达式"
            )
    variables = {}
    patterns = sorted(
        (
            *contexts(tree, P.NodePatternContext),
            *contexts(tree, P.RelationDetailContext),
        ),
        key=lambda n: n.start.start,
    )
    for pattern in patterns:
        symbol = pattern.symbol()
        if symbol is None or not re.fullmatch(r"[A-Za-z_]\w*", symbol.getText()):
            _reject("Raw 写入中的节点和关系必须使用普通命名变量")
        name = symbol.getText()
        node = isinstance(pattern, P.NodePatternContext)
        labels = pattern.nodeLabels() if node else pattern.relationshipTypes()
        names = {n.getText() for n in labels.name()} if labels else set()
        allowed = WRITABLE_LABELS if node else WRITABLE_RELATIONS
        if names - allowed or (names and len(names) != 1):
            _reject("Raw 写入只允许知识图谱节点及其关系")
        if not node and pattern.rangeLit() is not None:
            _reject("Raw 写入不支持变长关系")
        previous = variables.get(name)
        if previous is None and not names:
            _reject("Raw 写入目标必须绑定明确的节点标签或关系类型")
        if previous and (previous[0] != node or (names and previous[1] != names)):
            _reject("Raw 写入不支持重新绑定变量类型")
        props = pattern.properties()
        if props:
            literal = props.mapLit()
            if literal is None:
                _reject("Raw 写入不支持动态属性映射")
            pairs = literal.mapPair()
            values = {
                pair.name().getText(): pair.expression().getText() for pair in pairs
            }
            if len(values) != len(pairs):
                _reject("属性映射中不能有重复字段")
            scoped = (
                values.get("workspace_id") == "$" + WORKSPACE_PARAM
                and values.get("owner_id") == "$" + OWNER_PARAM
            )
            if not scoped:
                _reject(
                    "节点和关系必须用 $__raw_workspace_id、$__raw_owner_id 绑定写入范围"
                )
        elif previous is None:
            _reject("首次绑定节点和关系时必须提供服务端范围参数")
        variables[name] = (node, names or previous[1])
    for relationship in contexts(tree, P.RelationshipPatternContext):
        if relationship.relationDetail() is None:
            _reject("Raw 写入不支持匿名关系")
    for item in (
        *contexts(tree, P.SetItemContext),
        *contexts(tree, P.RemoveItemContext),
    ):
        prop = item.propertyExpression()
        match = re.fullmatch(
            r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)", prop.getText() if prop else ""
        )
        if not match or match[1] not in variables or match[2] in _RESERVED:
            _reject("只允许修改普通属性；不能改归属、key、标签或整体替换属性映射")
    for deletion in contexts(tree, P.DeleteStContext):
        if deletion.DETACH():
            _reject("请显式删除已绑定范围的关系，不支持 DETACH DELETE")
        for expr in deletion.expressionChain().expression():
            if expr.getText() not in variables:
                _reject("DELETE 仅支持已绑定范围的变量")
    return True, cypher, {WORKSPACE_PARAM: workspace_id, OWNER_PARAM: user_id}


def _terminals(tree):
    for child in getattr(tree, "children", ()) or ():
        if isinstance(child, ParserRuleContext):
            yield from _terminals(child)
        else:
            yield child
