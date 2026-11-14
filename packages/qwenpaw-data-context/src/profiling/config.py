"""探查编排层配置。**与 semantic_config 并列，不继承。**

------------------------------------------------------------------------
三个配置类的分工
------------------------------------------------------------------------
====================================  ====================================
``semantic_config.config.Settings``   语义配置子系统自己的键
``profiling.config.Settings``         探查**编排层**自己的键（本类）
``profiling.algo.config.AlgoConfig``  探查**算法层**的全部参数
====================================  ====================================

三者**并列**，各自直接读同一份 env 文件。profiling 曾经继承
semantic_config，把 ``probe_out_dir`` / ``probe_query_max_rows`` /
``probe_log_*`` / ``llm_model`` / ``openai_api_key`` 借道语义配置声明一遍
再继承下来 —— 于是同一个键有两个声明处，而读的人（``load_provider`` /
``services.context``）用的是父类那份，子类改了不生效。现在这些键各归其位：

- 算法参数 → ``AlgoConfig``（``store_out_dir`` / ``execute_sql_max_rows`` /
  ``input_query_log_start_ts`` / ``llm_model`` / ``llm_api_key``）
- 编排层参数 → 本类

需要语义配置的值时 ``from semantic_config.config import get_settings`` 去读，
**不继承**。目前一个都不需要 —— 共享的是引擎（``semantic_config.engine``），
不是配置。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from qwenpaw_data.context.env import qwenpaw_data_env_file


class Settings(BaseSettings):
    """探查编排层配置。算法参数不在这里，见 ``profiling.algo.config``。"""

    model_config = SettingsConfigDict(
        env_file=str(qwenpaw_data_env_file()), env_file_encoding="utf-8", extra="ignore"
    )

    #: 可插拔算法 Provider：``模块:属性``（类或模块级函数集合）。
    #: 改 .env 的 ``PROBE_PROVIDER_PATH`` 即可切换与回滚。见 data-profiling SPEC §5.5。
    probe_provider_path: str = "profiling.algo_provider:AlgoProvider"


@lru_cache
def get_settings() -> Settings:
    return Settings()
