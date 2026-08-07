from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from datapaw.context.env import datapaw_env_file
from datapaw.context.paths import semantic_config_db_path

_PKG_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """应用配置，来源仓库根 .env（dotenv），未知键忽略。"""

    model_config = SettingsConfigDict(
        env_file=str(datapaw_env_file()), env_file_encoding="utf-8", extra="ignore"
    )

    app_host: str = "127.0.0.1"
    app_port: int = 8000

    db_path: str = str(semantic_config_db_path())
    # 默认指向包内 schema.sql（绝对路径，避免受 CWD 影响）
    schema_path: str = str(_PKG_DIR / "schema.sql")

    tz_offset: str = "+08:00"

    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    # 语义编织：合并后进程内直接调用 CM 的语义导入逻辑（不再走 HTTP）。
    # callback_url 随载荷一并下发，供远程/兜底回调路径更新 weave 任务状态。
    weave_callback_url: str = "http://127.0.0.1:8765/api/semantic-config/weave-task/callback"
    # 推送到 CM 的 schema 名（纯语义导入，一般用默认）
    weave_schema_name: str = "public"
    weave_cm_kill_url: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
