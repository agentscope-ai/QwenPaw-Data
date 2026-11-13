"""Public presentation settings. Service URLs and credentials are never included."""

from pydantic import BaseModel, ConfigDict, Field


class ConsoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = "QwenPaw Data"
    assistant_name: str = "Data Analyst"
    welcome_title: str = "开始新的数据分析"
    welcome_description: str = "选择数据源并描述你希望验证的业务问题。"
    workspace_name: str = "本地数据空间"
    logo_url: str = "/qwenpaw-data-brand.png"
    avatar_url: str = "/mascot-blue-transparent.png"
    sample_questions: list[str] = Field(default_factory=list, max_length=12)

    def browser_config(self, *, databridge: bool) -> dict:
        return {
            **self.model_dump(),
            "console_protocol": 1,
            "capabilities": {
                "skillsHub": True,
                "mcpHub": False,
                # Configuration is available; individual channels remain disabled
                # until explicitly configured and enabled in Host.
                "channels": True,
                "contextConsole": databridge,
            },
            "databridge": {
                "base_path": "/databridge",
                "api_base_url": "/databridge",
                "auth_mode": "portal",
                "console_url": "/console",
            }
            if databridge
            else None,
        }
