"""Settlement 知识检索器：使用 Host 标准 ReAct Agent 召回可追溯证据。"""

from __future__ import annotations

from contextlib import aclosing
from pathlib import Path

from agentscope.agent._config import ReActConfig
from agentscope.mcp import MCPClient
from agentscope.message import Msg, UserMsg
from agentscope.model import ChatModelBase
from agentscope.tool import Read, ToolChunk, Toolkit
from qwenpaw_data.host.core.agent.qwenpaw_data_agent import QwenPawDataAgent
from qwenpaw_data.host.core.integrations.cm import set_cm_mcp_headers
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.settings.cm import CmSettings
from qwenpaw_data.host.core.utils.skill import discover_builtin_skills

from .models import DetectedItem

_PROMPT_PATH = Path(__file__).parent / "prompts" / "RETRIEVER.md"
_GRAPH_TOOLS = (
    "get_graph_schema",
    "query_graph",
    "search_graph_fulltext",
    "search_graph_vector",
)


class _SkillRead(Read):
    """沿用原生 Read，将文件访问限定在 CM Skill 目录。"""

    description = "读取 cm-skills 目录内的参考文件，file_path 使用绝对路径。"

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self._directory = directory.resolve()

    async def call(self, file_path: str, **kwargs) -> ToolChunk:
        path = Path(file_path).resolve()
        if not path.is_relative_to(self._directory):
            raise PermissionError("只允许读取 cm-skills 目录内的文件")
        return await super().call(file_path=str(path), **kwargs)


class SettlementRetriever:
    def __init__(
        self,
        model: ChatModelBase,
        *,
        access_token: str | None,
        session_id: str,
        chat_id: str,
        max_iters: int = 8,
        timeout: float = 180.0,
    ) -> None:
        self._model = model
        self._access_token = access_token
        self._session_id = session_id
        self._chat_id = chat_id
        self._max_iters = max_iters
        self._timeout = timeout
        self._system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")

    async def retrieve(
        self,
        item: DetectedItem,
        *,
        datasource_id: str | None,
        domain: str,
    ) -> str:
        """返回模型整理的检索结果文本，不解析或校验输出格式。"""
        skill_dir = next(
            skill.src_dir
            for skill in discover_builtin_skills()
            if skill.name == "cm-skills"
        )
        client = MCPClient(
            name=CmSettings.MCP_NAME,
            is_stateful=False,
            mcp_config={
                "type": "http_mcp",
                "url": get_settings().cm.mcp_url(),
                "timeout": self._timeout,
            },
            execution_timeout=self._timeout,
            enable_tools=list(_GRAPH_TOOLS),
        )
        await set_cm_mcp_headers(
            client,
            self._access_token,
            datasource_id=datasource_id,
            session_id=self._session_id,
            chat_id=self._chat_id,
        )
        prompt = self._build_user_message(
            item,
            datasource_id=datasource_id,
            domain=domain,
        )
        agent = QwenPawDataAgent(
            name="settlement_retriever",
            system_prompt=self._system_prompt,
            model=self._model,
            toolkit=Toolkit(
                tools=[_SkillRead(skill_dir)],
                skills_or_loaders=[str(skill_dir)],
                mcps=[client],
            ),
            react_config=ReActConfig(
                max_iters=self._max_iters,
                interruption_raise_cancelled_error=True,
            ),
        )
        result = ""
        async with aclosing(
            agent.reply_stream(
                UserMsg(name="user", content=prompt), yield_final_msg=True
            )
        ) as events:
            async for event in events:
                if isinstance(event, Msg):
                    result = event.get_text_content()
        return result

    def _build_user_message(
        self,
        item: DetectedItem,
        *,
        datasource_id: str | None,
        domain: str,
    ) -> str:
        parts = ["## 检索对象", f"类型：{item.type.value}"]
        parts.extend(
            f"- {name}: {value}"
            for name, value in item.fields.items()
            if name != "domain"
        )
        parts.extend(
            [
                "",
                "## 检索条件",
                f"业务域名称：{domain}",
                f"数据源 ID：{datasource_id or '未指定，不预设数据源'}",
            ]
        )
        return "\n".join(parts)
