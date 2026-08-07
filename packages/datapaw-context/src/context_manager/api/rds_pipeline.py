"""RDS / Postgres 元数据导入流水线说明（供 ``GET /api/rds_import_pipeline`` 使用）。

本仓库没有「解析任意 DDL 字符串直接写 Neo4j」的单步接口；标准路径是：

    DDL 文件 → 在 Postgres 上执行（生成 catalog）→ SQL 反射 → Neo4j 图 →（可选）拓扑层与向量索引。
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def build_rds_import_pipeline_payload() -> dict:
    """返回前端与外部工具可消费的流水线 JSON（无密钥，仅仓库相对命令）。"""
    return {
        "id": "postgres_ddl_to_neo4j",
        "title": "基于 Postgres（含云 RDS）的元数据导入流水线",
        "summary": (
            "将 DDL 在 PostgreSQL 上执行，使实例内形成 information_schema / pg_catalog；"
            "再用反射 API（SQLAlchemy/information_schema）读取 catalog，写入 Neo4j；"
            "完整 Default 场景还包括拓扑语义层与列/Metric 向量索引。"
        ),
        "assumptions": [
            "引擎为 **PostgreSQL**（阿里云 RDS PostgreSQL、自建 PG、本地 Docker postgres 均可）。",
            "DDL 需为目标方言可执行脚本；本仓库示例使用 `scripts/setup/load_sample_ddl.py` 清洗 Hologres 方言后灌入。",
            "Neo4j 已可用，`.env` 中配置 `NEO4J_URI`、`NEO4J_USER`、`NEO4J_PASSWORD`。",
            "数据源连接信息在语义配置层（`semantic_config.db`）中配置并 sync，无需在 `.env` 中单独设置 `PG_*` 环境变量。",
        ],
        "flow_diagram_mermaid": """flowchart LR
  DDL[DDL 文件] --> APPLY[在 PG 上执行 CREATE TABLE / COMMENT]
  APPLY --> CAT[(PG catalog)]
  CAT --> REFL[reflect_postgres / ingest_postgres]
  REFL --> NEO[(Neo4j 物理 schema 图)]
  NEO --> TOPO[build_topology 语义/Join/知识/轨迹]
  TOPO --> VEC[index_embeddings 向量索引]
""",
        "machine_readable_api": {
            "description": "本流水线说明由 HTTP GET 返回，便于 UI 与自动化拼接命令。",
            "endpoint": "GET /api/rds_import_pipeline",
        },
        "cli_interfaces": [
            {
                "name": "一键初始化（推荐）",
                "description": (
                    "Docker 起 Neo4j；默认 `make setup-default` 只从数据源反射建 Neo4j + 拓扑 + 向量（不改数据源对象）。"
                    " 首次空库需灌示例 DDL：`make setup-default LOAD_SAMPLE_DDL=1`。"
                ),
                "commands": [
                    {"shell": "make docker-up"},
                    {"shell": "make setup-default"},
                    {
                        "shell": "make setup-default LOAD_SAMPLE_DDL=1",
                        "note": "仅在需要从仓库 `data/test/ddl.txt` 写入 Postgres 时使用（例如本地空库）。",
                    },
                ],
            },
            {
                "name": "分步（与 make setup-default 等价顺序）",
                "steps_ref": ["infra", "ddl_apply", "neo4j_physical", "topology", "embeddings"],
            },
        ],
        "steps": [
            {
                "id": "infra",
                "title": "1. 基础设施",
                "input": "Docker 或已有 Neo4j（数据源连接串在语义配置层管理）",
                "output": "可连通的 Neo4j",
                "commands": [
                    {
                        "shell": "docker compose up -d",
                        "note": "本地容器名见 Makefile / docker-compose.yml；若使用云 RDS 可跳过，仅需在语义配置层配好数据源连接信息。",
                    },
                ],
            },
            {
                "id": "ddl_apply",
                "title": "2. DDL → Postgres catalog（apply）",
                "input": "DDL 文本文件（示例默认 `data/test/ddl.txt`）",
                "output": "实例内真实表结构，可被 information_schema 反射",
                "commands": [
                    {
                        "shell": "python scripts/setup/load_sample_ddl.py",
                        "note": "将清洗后的语句执行到数据源所指向的库；也可用自有迁移工具执行等价 DDL。`make setup-default` 默认跳过此步，需显式 `LOAD_SAMPLE_DDL=1` 或手动运行本命令。",
                    },
                    {
                        "shell": "python scripts/setup/load_sample_ddl.py --dump cleaned.sql",
                        "note": "仅生成清洗后的 SQL，不连库。",
                    },
                ],
                "code_paths": [
                    "scripts/setup/load_sample_ddl.py",
                ],
            },
            {
                "id": "neo4j_physical",
                "title": "3. Reflect catalog → Neo4j 物理 schema 图",
                "input": "数据源（PostgreSQL/Hologres）中已存在的 schema",
                "output": "Neo4j 中 Database/Table/Column 及 REFERENCES/JOINS 等（与 ingest 路径一致）",
                "commands": [
                    {
                        "shell": (
                            "python scripts/setup/with_dataset_neo4j.py appdata -- "
                            "scripts/setup/build_graph.py --postgres"
                        ),
                        "note": "`--pg-graph-id` 可覆盖写入 Neo4j 的 db_id（默认用 active datasource 的 dbname）。",
                    },
                ],
                "code_paths": [
                    "context_manager/ingest.py (`ingest_postgres`, `reflect_postgres`)",
                    "scripts/setup/build_graph.py",
                ],
            },
            {
                "id": "topology",
                "title": "4. 拓扑层（Default：Join 推断 + metrics_dict + 知识图 + 轨迹）",
                "input": "物理层已在 Neo4j；`data/test/metrics_dict.yaml` 等 YAML",
                "output": "Domain/Metric/Table 桥接、JOINS_ON、事件与轨迹节点",
                "commands": [
                    {
                        "shell": (
                            "python scripts/setup/with_dataset_neo4j.py appdata -- "
                            "scripts/setup/build_topology.py"
                        ),
                        "note": "可用 `--no-trace`、`--no-knowledge` 等跳过子阶段；`--drop-topology` 清空拓扑节点。",
                    },
                ],
                "code_paths": [
                    "context_manager/topology/runner.py",
                    "context_manager/topology/physical.py",
                ],
            },
            {
                "id": "embeddings",
                "title": "5. 向量索引（列 / Metric / Dimension）",
                "input": "上图谱已有语义节点与列文本",
                "output": "Neo4j 向量属性 + vector index，供 hybrid 检索",
                "commands": [
                    {
                        "shell": (
                            "python scripts/setup/with_dataset_neo4j.py appdata -- "
                            "scripts/setup/index_embeddings.py"
                        ),
                    },
                ],
                "code_paths": [
                    "context_manager/topology/embeddings.py",
                    "scripts/setup/index_embeddings.py",
                ],
            },
        ],
        "env_keys_documentation": [
            "NEO4J_URI",
            "NEO4J_USER",
            "NEO4J_PASSWORD",
        ],
        "repo_root": str(REPO_ROOT),
        "documentation_file": "docs/rds_import_pipeline.md",
    }
