# datapaw-context

DataPaw 的**上下文管理 + 图记忆（graph memory）基础模块**。它把数据元数据、拓扑与知识组织成可连接、可检索、可演化的图状记忆，对外只暴露语义信息需求，不暴露内部图遍历 / 向量检索拓扑。类比 Mem0 / Zep / MemOS 之于 LLM 应用，差异在于本包同时管理 fact / episode 与 table / column / metric / formula 等结构化节点。

核心架构：**三图**（Metadata Graph / Topology Graph / Knowledge Graph）+ **五阶段**（Build → Store → Retrieve → Learn → Govern）。

本包由原独立项目 `context-management` 合并而来，作为 CLI host、qwenpaw plugin、skills 等共享的 host 无关记忆能力底座。

## 目录结构

```text
packages/datapaw-context/
├── src/
│   ├── datapaw/context/         ← DataPaw 命名空间占位（对外 API 预留）
│   ├── context_manager/          ← CM 核心：图构建 / 检索 / 管线 / FastAPI(api/server.py)
│   └── semantic_config/          ← 语义配置编辑层（SQLite CRUD + Excel 导入 + 编织）
├── frontend/                     ← DataBridge 前端（Vite，固定端口 3000）
├── scripts/
│   ├── serve.py                  ← Web / API 服务入口
│   └── setup/                    ← 构图、下载数据集、向量索引
├── config/
│   ├── agent_explorer.json       ← Explorer / Agent 超参
│   └── datasources.json          ← 数据源登记
├── Makefile                      ← 快捷命令（serve / setup）
├── pyproject.toml                ← 包定义 + 依赖声明（hatchling）
├── requirements.txt              ← 依赖说明（含注释）
├── requirements.lock.txt         ← 已验证可用的精确版本快照（用于复现安装）
├── semantic_config.db            ← 编辑层 SQLite（本地，含连接信息，默认不提交）
└── .venv/                        ← 本包**独立** uv 虚拟环境（隔离，不提交）
```

> `frontend/` 与 API 同属 DataBridge 本地运行时，由仓库脚本统一初始化和启动。

## 前置依赖

- **Python 3.12**（见 `.python-version`）
- **[uv](https://docs.astral.sh/uv/)**（用于虚拟环境与依赖管理）
- **Node.js + npm**（用于 DataBridge 前端）
- **Neo4j 5.20+ Community**（图库；可选，仅图相关能力需要）

> 服务本身在图库 / PG 未启动时也能启动：`GET /api/health` 仅返回最小存活状态，而基于 **SQLite** 的语义配置编辑层（`/api/semantic-config/*`）无需图库即可 CRUD。

### 启动数据库（可选，Docker）

```bash
# Neo4j
export NEO4J_PASSWORD="$(openssl rand -hex 32)"
docker run -d --name neo4j \
  -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH="neo4j/${NEO4J_PASSWORD}" \
  -e NEO4J_PLUGINS='["apoc"]' \
  -e NEO4J_dbms_security_procedures_unrestricted='apoc.*' \
  neo4j:5.20-community
```

## 安装

推荐从仓库根目录初始化 DataBridge 的 Python 和前端依赖：

```bash
scripts/init_databridge.sh
```

如需手工安装本包独立 uv 虚拟环境，可执行：

在 `packages/datapaw-context/` 目录下执行：

```bash
# 1) 创建隔离虚拟环境（Python 3.12）
uv venv --python 3.12 .venv

# 2) 安装依赖
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -r requirements.lock.txt

# 3) 以可编辑方式注册本包（context_manager / semantic_config / datapaw.context）
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e . --no-deps
```

> 若需按声明式约束重新解析依赖（而非锁定快照），可用 `uv pip install -e .` 走 `pyproject.toml` 的 `dependencies`。

### 配置环境变量

```bash
cd ../..
cp .env.example .env
```

编辑仓库根目录 `.env`，填写模型配置：

```bash
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-max
EMBED_MODEL=text-embedding-v3
EMBED_DIM=1024
```

将 `.env` 中的 `NEO4J_PASSWORD=YOUR_PASSWORD` 替换为本地 Neo4j 密码。
本地 Neo4j 的地址和用户名已有默认值；只有连接外部实例时，才需要额外配置
`NEO4J_URI` 和 `NEO4J_USER`。

> 数据源（PostgreSQL / MySQL / ODPS / Hologres 等）的连接信息请在语义配置层中配置（`/api/semantic-config/datasource`），无需在 `.env` 中单独设置。

## 启动服务

```bash
# 推荐：同时启动 DataBridge 前端和 API
scripts/start_databridge.sh

# 仅启动数据库和 API
scripts/start_databridge.sh --skip-frontend
```

启动成功后可见：

```text
INFO api.server: Neo4j driver opened: bolt://localhost:7687
INFO api.server: semantic-config SQLite initialized
INFO:     Uvicorn running on http://127.0.0.1:8765
```

默认服务地址：

```text
DataBridge UI:      http://localhost:3000
DataBridge API:     http://localhost:8765
OpenAPI:            http://localhost:8765/docs
```

前端由 Vite 提供并启用热更新，端口固定为 3000；若端口被占用，启动会明确失败，
不会自动切换到其他端口。

常用开关：`--reload`（后端源码热重载）、`--host`、`--log-level`、
`--skip-frontend`。前端源码更新始终由 Vite HMR 处理。

## API 概览

服务在单进程、单端口（默认 8765）内共存三套路由。

### CM 语义能力（REST 前缀 `/api/v1/cm`，MCP 前缀 `/mcp/v1/cm`）

L1 — 意图理解：

| 接口 | 方法 | 说明 |
|------|------|------|
| `/search_context` | POST | 自然语言 → 结构化语义上下文（SSE 流式） |

L2 — 上下文操作：`/explore_entity`、`/compare_entities`、`/search_event`、`/execute_sql`（均 POST）。

L3 — 实体查询（GET）：`/domains`、`/domain-overview`、`/metrics`、`/search-metrics`、`/north-star-metrics`、`/dimensions`、`/dimension-hierarchy`、`/dimension-values`、`/datasets`、`/dataset-relations`。

### 语义配置编辑层（前缀 `/api/semantic-config`）

用本地 **SQLite** 管理数据源 / 业务域 / 数据集 / 维度 / 指标等实体（CRUD + Excel 导入），再通过“编织(weave)”把配置推成 CM 的图。与 CM 同进程、同端口。

- 主要路由：`/datasource`、`/biz-domain`、`/dataset-meta`、`/dataset-column-meta`、`/dataset-dimension`、`/dimension`、`/metric-lib`、`/metric-formula-lib`、`/import/excel`、`/weave-task/*`。
- **编织(weave)**：`POST /weave-task/submit` 按 datasource 整库组装语义载荷，进程内直接调用 CM 的语义导入逻辑（`context_manager.graph.semantic_import_service.run_semantic_import_async`）写入图库；回调地址见 `.env` 的 `WEAVE_CALLBACK_URL`。
- 存储不依赖 Neo4j/PG：即便未起图库，CRUD / Excel 入库仍可用（仅“编织”需图库在线）。
- 错误协议：`/api/semantic-config/*` 返回 `{timestamp,status,error,message}`；CM `/api/v1/*` 保持原协议。
- 定位：**仅供本地 / 内网使用，不含鉴权**。

### 图浏览 / 运维 / 探索（前缀 `/api`）

`/api/health`、`/api/agent_query`、`/api/chat_stream`、`/api/execute_sql`、`/api/global_graph`、`/api/domain_graph`、`/api/search_nodes` 等（供前端页面与脚本使用）。完整列表见 `/docs`。

## 路径与配置说明（合并后）

- 后端可导入包位于 `src/`（`context_manager`、`semantic_config`），运维目录与资源（`scripts/`、`config/`、`semantic_config.db`）位于包根；环境变量统一使用仓库根目录 `.env`。
- `context_manager` 与 `semantic_config` 的路径推导已锚定到**包根**（基于 `__file__` 的绝对路径），不依赖启动目录（CWD），可在任意目录启动。
- `.venv/`、仓库根目录 `.env`、`*.db` 已在 `.gitignore` 中忽略（`semantic_config.db` 含数据源连接信息，勿提交）。
