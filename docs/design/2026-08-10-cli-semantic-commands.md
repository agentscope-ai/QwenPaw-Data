# Design Doc: DataPaw CLI 数据源与语义操作命令

- 状态:Reviewed(开放问题已决议,可开工)
- 目标版本:0.2.0(新增命令,不破坏既有命令)
- 后端改动:**无**(全部复用现有 `/api/semantic-config/*` 与 `/api/v1/cm/*` 路由)
- 关联:与 QwenPaw-Data-Cloud 共享同一 API 契约;本设计以 API 契约对齐,CLI 可同时用于本地版与 Cloud 部署

## 1. 背景与目标

当前 `datapaw` CLI 仅提供 `datasource list` 一个管理类命令。数据源的增删改查、
连通性测试,以及语义层(业务域、数据集、维度、指标等)的全部配置操作只能通过
DataBridge UI(localhost:3000)或直接调用 REST API 完成,无法脚本化、无法进入
CI/自动化流程。

本设计交付两组命令:

1. **数据源及配置态操作**:`datapaw datasource` 扩展为完整 CRUD + 连通性测试。
2. **语义操作(CRUD)**:新增 `datapaw semantic`,覆盖业务域、数据集、列、维度、
   维度绑定、指标、指标公式的 CRUD,以及 Excel 导入与「织入」(weave,即配置态
   发布到图谱)的任务管理。

### 配置态模型

DataBridge 的语义配置存储在本地 SQLite(草稿态),通过 weave 任务异步发布到
图存储(Neo4j)。没有独立的 draft/published 字段,**weave 任务生命周期即发布
机制**(pending / running / success / failed / killed)。CLI 将其暴露为
`semantic weave` 子命令。

### 非目标(Out of Scope)

- KG 文档管理(`/api/v1/docs/*`)、轨迹图管理:后续单独设计。
- 后端新增路由、鉴权模型改动:不涉及。
- 表格化输出(table format):首版仅 JSON,保持与 `datasource list` 一致。

## 2. 交付命令总览

```text
datapaw datasource
  list                                    # 既有,保持不变(免凭证端点)
  get <datasource_id> [--show-config]
  create --name <n> --type <t> (--config-file <f.json> | --config '<json>') [--test]
  update <datasource_id> [--name <n>] [--type <t>] [--config-file <f> | --config '<json>']
  delete <datasource_id> [--yes]
  test (<datasource_id> | --type <t> (--config-file <f> | --config '<json>'))

datapaw semantic
  domain     list|get|create|update|delete     # 业务域   biz-domain
  dataset    list|get|create|update|delete     # 数据集   dataset-meta
  column     list|get|create|update|delete     # 数据集列 dataset-column-meta
  dimension  list|get|create|update|delete     # 维度     dimension
  binding    list|get|create|update|delete     # 维度绑定 dataset-dimension
  metric     list|get|create|update|delete     # 指标     metric-lib
  formula    list|get|create|update|delete     # 指标公式 metric-formula-lib
  import --file <semantic_config.xlsx>         # Excel 导入语义配置
  weave submit --datasource-id <id> [--mode FULL] [--name <task>] [--wait [--timeout <sec>]]
  weave list [--datasource-name <n>] [--task-name <n>] [--page --size]
  weave kill <task_id>
```

7 类语义对象的 CRUD 完全同构(见 §5 资源描述表),用统一实现驱动。

## 3. 命令规格与用法

### 3.1 通用约定

- **输出**:统一 JSON(`ensure_ascii=False, indent=2`),列表输出
  `{"items": [...], "total": N}`,单对象直接输出对象本身;与既有
  `datasource list` 保持一致。
- **退出码**:`0` 成功;`1` 失败(参数错误、HTTP 错误、连接失败、用户取消);
  `130` 中断(Ctrl-C)。沿用 `main.py` 既有约定。
- **分页**:所有 `list` 支持 `--page`(默认 1)、`--size`(默认 20);
  `--all` 拉取全部页(沿用 `cm_client` 的分页校验逻辑)。
- **请求体输入**:`create`/`update` 支持两种互斥方式:
  - `--file <payload.json>`:从 JSON 文件读取请求体;
  - 一等公民 flags(见各资源小节),适合简单场景;
  - 两者同时给出时报错。
- **敏感信息掩码**:任何输出中 `config` 内的
  `password / access_key_id / access_key_secret / sts_token` 一律输出 `******`
  (复用既有 `SENSITIVE_CONFIG_FIELDS`)。**CLI 无任何选项可输出明文凭证**。
- **删除确认**:`delete` 默认在 TTY 中要求输入 `y` 确认;`--yes` 跳过;
  非 TTY(脚本)且未给 `--yes` 时直接失败并提示。
- **错误显示**:服务端 `/api/semantic-config/*` 错误协议为
  `{timestamp, status, error, message}`;CLI 提取 `message` 输出为
  `datapaw: error: <message>`。`401/403` 时附加提示:
  `hint: set DATAPAW_CLIENT_API_TOKEN (or DATAPAW_API_TOKEN) with the required scope`。

### 3.2 `datapaw datasource`(数据源及配置态)

对应路由:`/api/semantic-config/datasource*`(scope:`credentials:manage`);
`list` 保持走免凭证的 `/api/v1/cm/datasources`(scope:`query`)。

| 子命令 | 方法与路径 | 说明 |
| --- | --- | --- |
| `list` | `GET /api/v1/cm/datasources` | 既有行为不变;不含 config |
| `get <id>` | `GET /api/semantic-config/datasource/{id}` | 默认不输出 config;`--show-config` 输出掩码后的 config |
| `create` | `POST /api/semantic-config/datasource` | `datasource_id` 由后端生成(`type-uuid`);`--test` 先调 test-connection,失败则不落盘(无跳过选项) |
| `update <id>` | `PUT /api/semantic-config/datasource/{id}` | config 传入即整体替换,未传保持不变(与后端语义一致) |
| `delete <id>` | `DELETE /api/semantic-config/datasource/{id}` | 需确认;后端会级联处理图谱侧数据 |
| `test <id>` | `POST /api/semantic-config/datasource/{id}/test-connection` | 测试已保存数据源 |
| `test --type --config*` | `POST /api/semantic-config/datasource/test-connection` | 存盘前测试 |

用法示例:

```bash
# 新建 PostgreSQL 数据源,先测连通再落盘
datapaw datasource create \
  --name "demo-pg" --type postgresql \
  --config-file examples/demo/postgres/datasource.example.json \
  --test

# 输出(config 已掩码):
# {
#   "datasource_id": "postgresql-3f2a...",
#   "datasource_name": "demo-pg",
#   "datasource_type": "postgresql",
#   "config": { "host": "127.0.0.1", "port": 5432, "password": "******", ... }
# }

datapaw datasource get postgresql-3f2a... --show-config
datapaw datasource update postgresql-3f2a... --name "demo-pg-v2"
datapaw datasource test postgresql-3f2a...
# { "success": true, "message": "ok", "tables_found": 12, "elapsed_ms": 45 }
datapaw datasource delete postgresql-3f2a... --yes
```

### 3.3 `datapaw semantic <resource>`(语义 CRUD)

对应路由:`/api/semantic-config/<resource-path>`;GET 需 `query` scope,
POST/PUT/DELETE 需 `manage` scope。

各资源的一等公民 flags(均可用 `--file` 替代;`update` 时同名 flag 覆盖对应字段):

| 资源 | 路径 | ID 字段 | list 过滤 flags | create 一等 flags |
| --- | --- | --- | --- | --- |
| `domain` | `biz-domain` | `domain_id` | `--datasource-id --name` | `--datasource-id --name --display-name --description --aliases` |
| `dataset` | `dataset-meta` | `dataset_id` | `--datasource-id --domain-id --name --type` | `--datasource-id --domain-id --name --comment --type --sql --parents` |
| `column` | `dataset-column-meta` | `col_id` | `--datasource-id --domain-id --dataset-id` | `--dataset-id --file`(字段多,推荐 `--file`) |
| `dimension` | `dimension` | `dim_id` | `--datasource-id --domain-id --name` | `--datasource-id --domain-id --name --description --parent-name --synonyms --enums` |
| `binding` | `dataset-dimension` | `dd_id` | `--datasource-id --domain-id --dataset-id --dataset-name --dimension-name` | `--dataset-id --file` |
| `metric` | `metric-lib` | `metric_id` | `--datasource-id --domain-id --name` | `--datasource-id --domain-id --name --description --unit --synonyms --tags [--polaris]` |
| `formula` | `metric-formula-lib` | `fid` | `--datasource-id --domain-id --metric-id --dataset-id` | `--metric-id --dataset-id --formula --file` |

用法示例:

```bash
# 业务域
datapaw semantic domain create --datasource-id postgresql-demo-gaap \
  --name gaap --display-name "GAAP 域" --description "GAAP 指标域"
datapaw semantic domain list --datasource-id postgresql-demo-gaap

# 指标 CRUD
datapaw semantic metric create --datasource-id postgresql-demo-gaap \
  --domain-id 1 --name avg_gaap_value --unit CNY --description "有效用户平均 GAAP 值"
datapaw semantic metric list --domain-id 1 --page 1 --size 50
datapaw semantic metric update 3 --description "..."
datapaw semantic metric delete 3 --yes

# 复杂对象用 JSON 文件
datapaw semantic formula create --file formula.json

# binding / formula 支持 dataset 级批量删除(对齐 DELETE .../dataset/{dataset_id})
datapaw semantic binding delete --dataset-id 12 --yes
datapaw semantic formula delete --dataset-id 12 --yes
```

`binding` / `formula` 的 `delete` 接受**互斥**的两种目标:位置参数 `<id>`(单条)
或 `--dataset-id <id>`(批量,删除该数据集下全部绑定/公式);批量删除同样走
确认流程。

### 3.4 `datapaw semantic import`(Excel 导入)

对应 `POST /api/semantic-config/import/excel`(multipart,scope `manage`)。

```bash
datapaw semantic import --file examples/demo_semantic_config.xlsx
# 输出后端导入统计(新增/更新的各类对象计数)
```

### 3.5 `datapaw semantic weave`(配置态发布)

对应 `/api/semantic-config/weave-task/*`(submit/callback:`write`;
kill:`manage`;list:`query`)。

```bash
# 提交 FULL 织入并等待完成(轮询 weave list,默认 2s 间隔)
datapaw semantic weave submit --datasource-id postgresql-demo-gaap \
  --mode FULL --wait --timeout 600
# 终态 success → exit 0;failed/killed/超时 → exit 1 并输出 error_msg

datapaw semantic weave list --datasource-name "Demo PG"
datapaw semantic weave kill 7d9c...
```

`--wait` 语义:提交成功后轮询任务状态直到进入终态或超时;等待期间每次轮询向
stderr 打一个进度点(`.`),状态变化时换行输出新状态(如 `pending → running`),
最终结果 JSON 仍输出到 stdout,不污染管道。超时仅停止等待,不会自动 kill 任务
(输出提示如何手工 `weave kill`)。

### 3.6 端到端脚本示例(替代 UI 的完整链路)

```bash
datapaw datasource create --name demo-pg --type postgresql --config-file ds.json --test
datapaw semantic import --file demo_semantic_config.xlsx
datapaw semantic weave submit --datasource-id postgresql-xxxx --mode FULL --wait
datapaw run --datasource-id postgresql-xxxx "分析 3 月有效用户平均 GAAP 值走势"
```

## 4. 配置与认证

复用既有环境变量,无新增:

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `DATAPAW_CM_BASE_URL` | DataBridge API 地址 | `http://127.0.0.1:8765` |
| `DATAPAW_CLIENT_API_TOKEN` | CLI Bearer token(优先) | 空 |
| `DATAPAW_API_TOKEN` | 全 scope 兼容 token(回退) | 空 |
| `DATAPAW_ENV_FILE` | dotenv 文件位置 | 仓库根 `.env` |

所需 scope 汇总(fail-closed,见 `context_manager/api/authorization.py`):

| 命令 | 所需 scope |
| --- | --- |
| `datasource list` | `query` |
| `datasource get/create/update/delete/test` | `credentials:manage` |
| `semantic * list/get` | `query` |
| `semantic * create/update/delete`、`import`、`weave kill` | `manage` |
| `weave submit` | `write` |

本地默认部署(未配 token、绑定 127.0.0.1)下鉴权中间件放行,CLI 零配置可用;
配置了 scoped keys 的部署需要为 CLI 发一个带上述 scope 的 key。

## 5. 架构设计

### 5.1 分层

```text
datapaw-cli   commands/datasource.py   (扩展)
              commands/semantic.py     (新增, 资源表驱动)
                     │  仅做:参数解析 → 调 client → 掩码/格式化输出
datapaw-host-core    semantic_config_client.py  (新增)
                     │  SemanticConfigClient:HTTP、鉴权头、分页、错误协议解析
                     └  复用 cm_client 的 resolve_cm_base_url / token 解析 / loopback 免代理
```

- `ContextManagerClient`(`cm_client.py`)保持不变,继续服务 `datasource list`。
- 新增 `SemanticConfigClient`:通用 `request(method, path, *, params, json, files)`
  + `list_pages(path, params)`(复用既有 total/page/size 一致性校验),错误统一
  抛 `SemanticConfigClientError(status, message)`(从 `{timestamp,status,error,message}`
  解析,非该协议时回退 HTTP 状态行)。

### 5.2 资源表驱动的 CRUD

`commands/semantic.py` 用声明式描述表生成 7 组同构子命令,避免七份复制:

```python
@dataclass(frozen=True)
class Resource:
    name: str            # CLI 子命令名, e.g. "metric"
    path: str            # API 路径段, e.g. "metric-lib"
    id_field: str        # 响应 ID 字段, e.g. "metric_id" / "id"
    list_filters: tuple[Filter, ...]   # (--flag, query 参数名, 类型)
    create_fields: tuple[Field, ...]   # 一等公民 flags → 请求体字段
    update_fields: tuple[Field, ...]
```

`import` / `weave` 为独立 handler(multipart 上传、轮询等待)。

### 5.3 改动文件清单

| 文件 | 动作 |
| --- | --- |
| `packages/datapaw-host-core/src/datapaw/host/core/semantic_config_client.py` | 新增 |
| `packages/datapaw-host-core/tests/test_semantic_config_client.py` | 新增 |
| `packages/datapaw-cli/src/datapaw/cli/commands/datasource.py` | 扩展 get/create/update/delete/test |
| `packages/datapaw-cli/src/datapaw/cli/commands/semantic.py` | 新增 |
| `packages/datapaw-cli/src/datapaw/cli/commands/__init__.py` | 注册 `semantic` |
| `packages/datapaw-cli/tests/test_cli_datasource.py` | 扩展 |
| `packages/datapaw-cli/tests/test_cli_semantic.py` | 新增 |
| `README.md` / `README_ZH.md` / `packages/datapaw-cli/README.md` | 命令表更新 |
| `CHANGELOG.md` | Unreleased → Added |

后端(`datapaw-context`)零改动。

## 6. 与 Cloud 的对齐说明

- Cloud 与 OSS 共享 `/api/semantic-config/*` 契约(路由/模型逐文件比对一致),
  本 CLI 直接以该契约实现,两侧通用。
- Cloud 的 `cm_client` 用带 config 的 `/api/semantic-config/datasource` 做
  list;OSS 有意改为免凭证的 `/api/v1/cm/datasources` 并强制 `config=None`。
  **保留 OSS 行为**:`list` 永不接触凭证,凭证相关操作集中在需要
  `credentials:manage` 的子命令且输出掩码。
- Cloud 的 URL 解析走 Settings(`get_settings().cm.client_base_url()`),OSS 走
  环境变量;CLI 层不感知该差异(封装在 host-core)。

## 7. 测试计划

- **单元(CLI)**:沿用 `test_cli_datasource.py` 的 fake-client monkeypatch 模式,
  覆盖:各子命令成功路径、参数互斥(`--file` vs flags)、掩码、删除确认
  (TTY/非 TTY/`--yes`)、401/403 提示、weave `--wait` 的终态/超时。
- **单元(client)**:`httpx.MockTransport` 覆盖分页校验、错误协议解析、
  multipart 上传、loopback trust_env。
- **集成**:扩展 `scripts/e2e_cli.py` / `examples/smoke_test.py`,对本地
  DataBridge 跑一条「create datasource → import excel → weave --wait →
  metric list」链路(确定性、无需模型 key)。
- 全量 `scripts/verify.sh` 通过后合入。

## 8. 交付计划

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| M1 | `SemanticConfigClient` + `datasource` 完整 CRUD/test | 单测 + 对本地 DataBridge 手工验证 |
| M2 | `semantic` 7 资源 CRUD(表驱动)+ `import` | 单测 + Excel 导入示例可复现 |
| M3 | `weave submit/list/kill`(含 `--wait`)+ e2e + 文档 | smoke 链路通过,README 更新 |

## 9. 已决议的设计问题

1. **dataset 级批量删除:暴露。** `binding` / `formula` 的 `delete` 支持
   `--dataset-id <id>` 批量删除,与后端 `DELETE .../dataset/{dataset_id}`
   端点对齐(见 §3.3)。
2. **`weave submit --wait` 进度反馈:打进度点。** 每次轮询向 stderr 输出 `.`,
   状态变化换行提示;结果 JSON 走 stdout(见 §3.5)。
3. **`datasource create --test` 不提供 `--force`。** 连通性测试失败即退出
   (exit 1),不落盘,保持行为简单可预期(见 §3.2)。
