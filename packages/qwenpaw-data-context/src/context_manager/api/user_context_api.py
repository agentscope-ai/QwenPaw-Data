"""Section-scoped API for persistent user context in the Trace Graph."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, cast

from fastapi import APIRouter, Path, Request, Response
from neo4j import Driver
from pydantic import BaseModel, Field

from ..utils import neo4j_session
from .response_envelope import fail, success

UserContextSection = Literal["soul", "profile", "principles", "preferences"]
UserId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Opaque user identifier supplied by the authentication system.",
    ),
]

SECTION_ORDER: tuple[UserContextSection, ...] = (
    "soul",
    "profile",
    "principles",
    "preferences",
)
SECTION_TITLES: dict[UserContextSection, str] = {
    "soul": "Soul",
    "profile": "Profile",
    "principles": "Principles",
    "preferences": "Preferences",
}
SECTION_INSTRUCTIONS: dict[UserContextSection, str] = {
    "soul": "Defines what you, the agent, are and the role you should play.",
    "profile": "Describes who the user is and stable context that may help you assist them.",
    "principles": "Lists durable rules you should follow when reasoning and acting.",
    "preferences": "Records how the user prefers answers and collaboration.",
}
DEFAULT_SECTION_CONTENT: dict[UserContextSection, str] = {
    "soul": (
        "You are QwenPaw-Data, a careful and practical enterprise data analysis agent."
    ),
    "profile": "No user profile has been provided yet.",
    "principles": (
        "- Ground conclusions in available evidence.\n"
        "- State important assumptions.\n"
        "- Preserve data provenance and user control."
    ),
    "preferences": (
        "No special preferences are known. Prefer clear and concise answers."
    ),
}
RESERVED_MARKER_PREFIX = "<!-- qwenpaw:"

router = APIRouter(prefix="/api/v1/users", tags=["user-context"])


class UpdateUserContextSectionRequest(BaseModel):
    """Replacement Markdown for one editable user-context section."""

    content: str = Field(..., min_length=1, max_length=16 * 1024)


def _section_tokens(section: UserContextSection) -> tuple[str, str]:
    """Return the stable Markdown boundary tokens for a section."""
    return (
        f"<!-- qwenpaw:{section}:start -->\n",
        f"\n<!-- qwenpaw:{section}:end -->",
    )


def _render_user_context_markdown(
    section_content: Mapping[UserContextSection, str],
) -> str:
    """Render the canonical Markdown document from section content."""
    blocks = ["# User Context"]
    for section in SECTION_ORDER:
        start_token, end_token = _section_tokens(section)
        blocks.append(
            f"## {SECTION_TITLES[section]}\n\n"
            f"> {SECTION_INSTRUCTIONS[section]}\n\n"
            f"{start_token}{section_content[section]}{end_token}"
        )
    return "\n\n".join(blocks) + "\n"


DEFAULT_USER_CONTEXT_MARKDOWN = _render_user_context_markdown(
    DEFAULT_SECTION_CONTENT
)


def _extract_section_content(
    markdown: str,
    section: UserContextSection,
) -> str:
    """Extract one editable section from a canonical Markdown document."""
    start_token, end_token = _section_tokens(section)
    start = markdown.find(start_token)
    if start < 0:
        raise ValueError(f"Missing start marker for section: {section}")
    content_start = start + len(start_token)
    end = markdown.find(end_token, content_start)
    if end < 0:
        raise ValueError(f"Missing end marker for section: {section}")
    return markdown[content_start:end]


def _replace_section_content(
    markdown: str,
    section: UserContextSection,
    content: str,
) -> str:
    """Replace one section while preserving every other Markdown byte."""
    start_token, end_token = _section_tokens(section)
    start = markdown.find(start_token)
    if start < 0:
        raise ValueError(f"Missing start marker for section: {section}")
    content_start = start + len(start_token)
    end = markdown.find(end_token, content_start)
    if end < 0:
        raise ValueError(f"Missing end marker for section: {section}")
    return markdown[:content_start] + content + markdown[end:]


def _validate_section_content(content: str) -> str:
    """Validate editable Markdown without normalizing its whitespace."""
    if not content.strip():
        raise ValueError("content must not be blank")
    if RESERVED_MARKER_PREFIX in content:
        raise ValueError("content must not contain reserved qwenpaw markers")
    return content


def _require_section(section: str) -> UserContextSection:
    """Validate a section path and return its narrowed type."""
    if section not in SECTION_ORDER:
        fail("NOT_FOUND", f"Unknown user-context section: {section}", status_code=404)
    return cast(UserContextSection, section)


def _user_key(user_id: str) -> str:
    """Build the globally unique graph key for a user node."""
    return f"user:{user_id}"


def _create_user_node(driver: Driver, user_id: str) -> dict[str, Any]:
    """Create a default user node once and return its creation status."""
    key = _user_key(user_id)
    with neo4j_session(driver) as session:
        session.run(
            "CREATE CONSTRAINT user_key IF NOT EXISTS "
            "FOR (u:User) REQUIRE u.key IS UNIQUE"
        ).consume()
        record = session.run(
            """
            WITH randomUUID() AS request_id, datetime() AS now
            MERGE (u:User {key: $key})
            ON CREATE SET u.user_id = $user_id,
                          u.zone = 'trace',
                          u.content_markdown = $content_markdown,
                          u.created_at = now,
                          u.updated_at = now,
                          u._creation_request_id = request_id
            ON MATCH SET u.user_id = coalesce(u.user_id, $user_id),
                         u.zone = 'trace'
            WITH u, coalesce(u._creation_request_id = request_id, false) AS created
            REMOVE u._creation_request_id
            RETURN u.key AS key,
                   u.user_id AS user_id,
                   created,
                   toString(u.created_at) AS created_at,
                   toString(u.updated_at) AS updated_at
            """,
            key=key,
            user_id=user_id,
            content_markdown=DEFAULT_USER_CONTEXT_MARKDOWN,
        ).single()
    if record is None:
        raise RuntimeError(f"Failed to initialize user context: {user_id}")
    return dict(record)


def _get_user_node(driver: Driver, user_id: str) -> dict[str, Any] | None:
    """Retrieve one user node and its complete Markdown document."""
    with neo4j_session(driver) as session:
        record = session.run(
            """
            MATCH (u:User {key: $key})
            RETURN u.key AS key,
                   u.user_id AS user_id,
                   u.content_markdown AS content_markdown,
                   toString(u.created_at) AS created_at,
                   toString(u.updated_at) AS updated_at
            """,
            key=_user_key(user_id),
        ).single()
    return dict(record) if record is not None else None


def _set_user_section(
    driver: Driver,
    user_id: str,
    section: UserContextSection,
    content: str,
) -> dict[str, Any] | None:
    """Replace one section in a user node within a single transaction."""
    with neo4j_session(driver) as session:
        with session.begin_transaction() as transaction:
            current = transaction.run(
                "MATCH (u:User {key: $key}) "
                "RETURN u.content_markdown AS content_markdown",
                key=_user_key(user_id),
            ).single()
            if current is None:
                return None
            updated_markdown = _replace_section_content(
                str(current["content_markdown"] or ""),
                section,
                content,
            )
            updated = transaction.run(
                """
                MATCH (u:User {key: $key})
                SET u.content_markdown = $content_markdown,
                    u.updated_at = datetime()
                RETURN u.key AS key,
                       u.user_id AS user_id,
                       u.content_markdown AS content_markdown,
                       toString(u.created_at) AS created_at,
                       toString(u.updated_at) AS updated_at
                """,
                key=_user_key(user_id),
                content_markdown=updated_markdown,
            ).single()
    return dict(updated) if updated is not None else None


def _section_response(
    node: Mapping[str, Any],
    section: UserContextSection,
) -> dict[str, Any]:
    """Build the public response for one user-context section."""
    content = _extract_section_content(
        str(node.get("content_markdown") or ""),
        section,
    )
    return {
        "key": node.get("key"),
        "user_id": node.get("user_id"),
        "section": section,
        "instruction": SECTION_INSTRUCTIONS[section],
        "content": content,
        "is_default": content == DEFAULT_SECTION_CONTENT[section],
        "updated_at": node.get("updated_at"),
    }


@router.post("/{user_id}/context")
def create_user_context(
    user_id: UserId,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Initialize a user's default context after their first login."""
    node = _create_user_node(request.app.state.driver, user_id)
    if node["created"]:
        from .retrieval import invalidate_global_graph_snapshot_cache

        invalidate_global_graph_snapshot_cache()
    response.status_code = 201 if node["created"] else 200
    return success(node)


@router.get("/{user_id}/context/{section}")
def get_user_context_section(
    user_id: UserId,
    section: str,
    request: Request,
) -> dict[str, Any]:
    """Return one user-context section."""
    section_name = _require_section(section)
    node = _get_user_node(request.app.state.driver, user_id)
    if node is None:
        fail("NOT_FOUND", f"User context not found: {user_id}", status_code=404)
    try:
        return success(_section_response(node, section_name))
    except ValueError as exc:
        fail("MALFORMED_CONTEXT", str(exc), status_code=500)


@router.put("/{user_id}/context/{section}")
def update_user_context_section(
    user_id: UserId,
    section: str,
    body: UpdateUserContextSectionRequest,
    request: Request,
) -> dict[str, Any]:
    """Replace only the editable content of one user-context section."""
    section_name = _require_section(section)
    try:
        content = _validate_section_content(body.content)
    except ValueError as exc:
        fail("VALIDATION_ERROR", str(exc))
    try:
        node = _set_user_section(
            request.app.state.driver,
            user_id,
            section_name,
            content,
        )
    except ValueError as exc:
        fail("MALFORMED_CONTEXT", str(exc), status_code=500)
    if node is None:
        fail("NOT_FOUND", f"User context not found: {user_id}", status_code=404)
    from .retrieval import invalidate_global_graph_snapshot_cache

    invalidate_global_graph_snapshot_cache()
    return success(_section_response(node, section_name))


@router.delete("/{user_id}/context/{section}")
def reset_user_context_section(
    user_id: UserId,
    section: str,
    request: Request,
) -> dict[str, Any]:
    """Reset one user-context section to its built-in default."""
    section_name = _require_section(section)
    try:
        node = _set_user_section(
            request.app.state.driver,
            user_id,
            section_name,
            DEFAULT_SECTION_CONTENT[section_name],
        )
    except ValueError as exc:
        fail("MALFORMED_CONTEXT", str(exc), status_code=500)
    if node is None:
        fail("NOT_FOUND", f"User context not found: {user_id}", status_code=404)
    from .retrieval import invalidate_global_graph_snapshot_cache

    invalidate_global_graph_snapshot_cache()
    return success(_section_response(node, section_name))
