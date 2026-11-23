"""Installed downstream applications; no application-specific imports in Host.

Entry points are trusted deployment code, never supplied by browser requests.
The application owns domain routers and state. Host retains execution, command
storage, authorization, cancellation and streaming.
"""
from functools import lru_cache
from importlib.metadata import entry_points


class HostApplication:
    """Trusted deployment extension, selected by the session's agent ID.

    Host owns execution and durable interaction. Extensions contribute tools,
    validate domain choices through context.clarification, and emit namespaced
    presentation payloads. These hooks are never loaded from request data.
    """

    api_version = 1
    required_skills = ()
    required_mcps = ()
    wrapped_mcps = ()
    mcp_defaults = {}

    def register(self, app):
        pass

    async def startup(self, app, cleanup):
        pass

    async def prepare_run(self, context, envelope):
        pass

    async def prepare_turn(self, context, turn):
        return turn

    async def close_run(self, context):
        pass

    def tools(self, context):
        return []

    async def handle_external(self, context, required):
        # None leaves standard questions with the native executor.
        return None


@lru_cache(maxsize=1)
def applications() -> dict:
    result = {}
    for entry in entry_points(group="qwenpaw_data.applications"):
        if entry.name in result:
            raise RuntimeError(f"Duplicate application: {entry.name}")
        application = entry.load()
        if not isinstance(application, HostApplication) or application.api_version != 1:
            raise RuntimeError(f"Unsupported Host application contract: {entry.name}")
        result[entry.name] = application
    return result


def application_for(agent_id: str):
    return applications().get(agent_id)
