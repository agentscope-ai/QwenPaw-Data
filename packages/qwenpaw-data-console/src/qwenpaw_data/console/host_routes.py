"""Explicit gateway contracts for the product features added in Host 0.4.

Do not derive this policy from upstream OpenAPI: new Host administrative routes
must not automatically become reachable with the gateway's service credential.
"""

import re

_ID = r"[A-Za-z0-9_-]+"
# Names may contain Unicode, dots and spaces, but never URL/path delimiters.
_NAME = r"[^/\\%?#\x00-\x1f\x7f]+"
_SKILLS = rf"skills/agents/{_NAME}"
_SESSION = rf"sessions/{_ID}"
_DRAFTS = rf"{_SESSION}/skill-drafts"
_PROPOSALS = rf"{_SESSION}/settlement/skill-proposals"
_CHANNEL = r"system/channel-config/(?:feishu|dingtalk|wecom|wechat)"

# Specific reserved paths precede name parameters. First match wins, as in the
# Host router; an unsupported method never falls back to a more general policy.
FEATURE_ROUTES = tuple(
    (re.compile(pattern), frozenset(methods.split()))
    for pattern, methods in (
        (rf"{_SKILLS}/import", "POST"),
        (rf"{_SKILLS}/{_NAME}/revisions/[0-9]+/revert", "POST"),
        (rf"{_SKILLS}/{_NAME}/revisions(?:/[0-9]+)?", "GET"),
        (rf"{_SKILLS}/{_NAME}/(?:enable|disable)", "POST"),
        (rf"{_SKILLS}/{_NAME}", "GET PUT DELETE"),
        (_SKILLS, "GET POST"),
        (rf"mcp/{_NAME}/tools", "GET PUT"),
        (rf"mcp/{_NAME}/(?:enable|disable)", "POST"),
        (rf"mcp/{_NAME}", "GET PUT DELETE"),
        ("mcp", "GET POST"),
        ("system/channel-config/", "GET"),
        (_CHANNEL, "PUT"),
        (rf"{_CHANNEL}/test", "POST"),
        (r"system/channel-config/wechat/qrcode(?:/status)?", "GET"),
        (r"settlement/(?:cards|skill-proposals)", "GET"),
        (rf"{_DRAFTS}/{_ID}/(?:revise|confirm|dismiss)", "POST"),
        (rf"{_DRAFTS}/{_ID}", "GET"),
        (_DRAFTS, "GET POST"),
        (rf"{_PROPOSALS}/{_ID}/(?:accept|reject)", "POST"),
        (rf"{_PROPOSALS}(?:/{_ID})?", "GET"),
        (rf"{_SESSION}/files/(?:share|compare|diff|compare/recommendations|upload)", "POST"),
    )
)


def safe_host_path(path: str, raw_path: bytes) -> bool:
    """Reject traversal, double encoding and URL delimiters before forwarding."""
    if re.search(r"[\\%?#\x00-\x1f\x7f]", path):
        return False
    if any(part in {"", ".", ".."} or part != part.strip()
           for part in path.rstrip("/").split("/")):
        return False
    return not re.search(br"%(?:2f|5c)", raw_path, re.IGNORECASE)


def feature_methods(path: str) -> frozenset[str] | None:
    for pattern, methods in FEATURE_ROUTES:
        if pattern.fullmatch(path):
            return methods
    return None
