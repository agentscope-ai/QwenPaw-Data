# qwenpaw-data-auth

QwenPawData unified authentication and authorization service.

QwenPawData 统一认证与授权服务，负责平台/租户/Workspace 三级组织的账号体系、角色权限（RBAC）、Token 签发与审计。控制面数据存放在统一数据库（`QWENPAW_DATA_DB_URL`，本地默认 SQLite、云上 Hologres），每个 Workspace 对应一个物理隔离的 Neo4j 实例。

## 运行模式

认证模式由 `QWENPAW_DATA_AUTH_MODE` 决定，默认 `local`：

- `local`（默认，本地单用户）：只为默认租户 / Workspace 播种本地管理员 `local_admin`（口令为随机哈希，不存在固定口令）。无凭据访问仅对直连 loopback、不带转发头且 Host / Origin 均为本机的请求开放；配置了 `QWENPAW_DATA_API_TOKEN` 后即使在 loopback 也必须携带该 token；
- `multi-user`：使用下文的平台 / 租户 / Workspace 账号体系，不会自动播种任何管理员。

未配置 `QWENPAW_DATA_AUTH_SECRET` / `QWENPAW_DATA_SERVICE_SECRET` 时，Auth 启动会生成两者并持久化到 `$QWENPAW_DATA_HOME/auth/secrets.json`（默认 `~/.qwenpaw-data`，POSIX 下权限 0600），重启后复用。下文除特别说明外均描述 `multi-user` 模式。

## 服务与端口

由仓库根目录 `scripts/start_all.sh` 一键启动（首次运行会自动生成缺失的密钥并写入根目录 `.env`）：

| 服务 | 默认地址 | 说明 |
| --- | --- | --- |
| Auth API | `http://localhost:8088` | 本包后端（FastAPI） |
| Context 业务前端 | `http://localhost:3000` | 数据语义管理台，内含登录页与用户控制中心（`/login`、`/user-center/*`） |
| Host 前端 | `http://localhost:3100` | QwenPawData 智能体中心（数据控制台） |
| Host API | `http://localhost:8089` | Host 后端（FastAPI），仅监听 loopback，由 Host 前端 dev proxy 转发 |
| Workspace Provisioner | 后台进程 | 消费开通队列，用 Docker 拉起 Workspace 专属 Neo4j |

相关环境变量（根目录 `.env`）：`QWENPAW_DATA_AUTH_SECRET`、`QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN`、`QWENPAW_DATA_SERVICE_SECRET`、`QWENPAW_DATA_DB_URL`、`VITE_AUTH_URL`、`VITE_AUTH_FRONTEND_URL` 等。

管理员创建／邀请、Workspace 或租户加入申请审批、直接授予角色这三个成员加入入口，在事务提交、响应发出后，后台为用户新获得访问权的 Workspace 尝试一次个人 MG 同步。授予租户管理员时覆盖此前无权访问的所有活跃 Workspace；重复邀请和未增加 Workspace 访问范围的角色升级不重复同步。

同步通过 `QWENPAW_DATA_AUTH_URL` 的 `/api/auth/temporary-access` 获取目标用户的令牌，再调用 `QWENPAW_DATA_CM_BASE_URL` 的 `/api/v1/mg/userspace/sync`；两个地址沿用 Host 配置，CM 默认 `http://127.0.0.1:8765`，同时需要配置 `QWENPAW_DATA_SERVICE_SECRET`。配置在 Auth 启动时读取。

获取令牌超时为 10 秒，MG 同步读取超时为 2400 秒（连接超时 10 秒）。同步失败只记录日志，不撤销已提交的成员权限，也不自动重试；进程退出可能中断该次尝试。本机制不覆盖租户／Workspace 基础设施开通或历史成员补同步，也不保证个人图在首次页面请求前已初始化完成。

## 角色模型

| 角色 | 作用域 | 说明 |
| --- | --- | --- |
| `platform_admin` | 平台 | 平台运营者：租户开通/暂停、基础设施开通、Raw Cypher 特批。不属于任何租户 |
| `tenant_admin` | 租户 | 租户管理员：管理本租户全部 Workspace、用户与角色 |
| `ws_admin` | Workspace | Workspace 管理员：管理所辖 Workspace 内的用户与图数据 |
| `member` | Workspace | 普通成员：读 MG、读写自己的 TG/KG，业务页面多为只读 |

所有账号密码需满足强度校验：至少 8 位、包含大写/小写/数字/特殊字符中至少 3 类、不得是常见弱口令。

## 权限边界

### 功能权限矩阵（resource × role）

唯一事实源是 `src/qwenpaw_data_auth/permissions.py`，前端镜像位于 `packages/qwenpaw-data-context/frontend/src/auth/permissions.ts`（两者必须同步修改）。`platform_admin` 不在此矩阵内——它不属于任何租户，**无法访问任何租户的业务数据与图数据**，仅能调用 `/api/platform/*` 控制面接口。

| 资源 | 含义 | tenant_admin | ws_admin | member |
| --- | --- | :---: | :---: | :---: |
| `mg:read` | 读全局元数据图 MG | ✓ | ✓ | ✓ |
| `mg:write` | 写 MG | ✓ | ✓ | ✗ |
| `mg_copy:self` | 拷贝 MG 到自己的空间 | ✓ | ✓ | ✓ |
| `mg_userspace:sync` | 拉取全局 MG 到自己的 userspace 镜像 | ✓ | ✓ | ✓ |
| `mg_userspace:append` | 在自己的 userspace MG 追加节点/关系（APPEND-only） | ✓ | ✓ | ✓ |
| `tg_kg:self` | 读写自己的 TG/KG | ✓ | ✓ | ✓ |
| `tg_kg:others_read` | 只读浏览其他成员的 TG/KG | ✓ | ✓ | ✗ |
| `workspace:manage` | Workspace 管理 | ✓ | ✓ | ✗ |
| `user:manage` | 用户与角色管理 | ✓ | ✓ | ✗ |
| `datasource:read` | 读数据源/数据集/指标等 | ✓ | ✓ | ✓ |
| `datasource:write` | 写数据源及语义配置 | ✓ | ✓ | ✗ |
| `skill:publish` | 发布技能 | ✓ | ✓ | ✗ |
| `skill:install` | 安装技能 | ✓ | ✓ | ✓ |
| `snapshot:create` | 创建快照 | ✓ | ✓ | ✓ |
| `snapshot:rollback_mg` | 回滚 MG 快照 | ✓ | ✓ | ✗ |
| `snapshot:rollback_self` | 回滚自己空间的快照 | ✓ | ✓ | ✓ |
| `raw_cypher` | 原生 Cypher（需额外特批，见下） | ✓ | ✓ | ✗ |
| `semantic_weaving` | 语义编织 | ✓ | ✓ | ✗ |
| `excel_import` | Excel 导入 | ✓ | ✓ | ✗ |
| `model_config` | 模型配置（LLM/Embedding，含 API Key；改 Embedding 触发重建） | ✓ | ✓ | ✓ |
| `ops:logs` | 运维日志 | ✓ | ✗ | ✗ |

注：`raw_cypher` 即便角色允许，还需同时满足 ① 平台管理员预先签发与查询绑定的一次性 grant（`POST /api/platform/raw-cypher-grants`）；② 服务开关 `QWENPAW_DATA_RAW_CYPHER_ENABLED=1`。

### 管理边界（谁能管理谁）

| 能力 | platform_admin | tenant_admin | ws_admin | member |
| --- | --- | --- | --- | --- |
| 租户开通/暂停/恢复 | ✓（全平台） | ✗ | ✗ | ✗ |
| 创建 Workspace | ✗ | ✓（本租户） | ✗ | ✗ |
| 可见 Workspace 范围 | — | 本租户全部活跃 Workspace | 仅自己被绑定的 Workspace | 仅自己被绑定的 Workspace |
| 创建用户 | ✗ | ✓（含 `tenant_admin`） | ✓（仅限所辖 Workspace，**不能创建 `tenant_admin`**） | ✗ |
| 授予/回收角色 | ✗ | ✓（本租户内） | ✓（仅限所辖 Workspace） | ✗ |
| Raw Cypher 特批 grant | ✓（签发/吊销） | ✗ | ✗ | ✗ |

角色授予的硬规则（`role_api.py`）：

- **R1 不得越级**：不能授予高于自己的角色（ws_admin 不能授 tenant_admin）；
- **R3 不得自授**：不能创建/删除自己的角色绑定；
- `tenant_admin` 角色只能绑在租户作用域，其余角色只能绑在 Workspace 作用域；
- ws_admin 绑定角色的目标 Workspace 必须在自己的管辖范围内。

### 数据边界（图数据隔离）

- **Workspace 物理隔离**：每个 Workspace 对应独立 Neo4j 实例，连接按登录会话的 `active_workspace_id` 路由，任何角色都无法跨 Workspace 查询；tenant_admin 查看其他 Workspace 需先 `switch-workspace` 切换会话；
- **行级 scope 谓词**：所有图查询强制携带 `workspace_id` + `owner_id` 过滤（`context_manager/api/graph_access.py` 的 `ScopedGraphAccess`）：
  - MG 节点 `owner_id='__global__'`，Workspace 内共享；
  - TG/KG 节点 `owner_id=用户ID`，member 只能命中自己 + `__global__`；
  - 管理员可全 Workspace 只读浏览，或通过 `owner_id` 参数收窄到单个成员（member 传他人 id 直接 403）；浏览他人图谱时为强制只读；
- **platform_admin 与数据面完全隔离**：平台会话 Cookie 作用域限定 `/api/platform`，无法调用租户业务 API；对图数据的唯一触点是签发查询绑定的 Raw Cypher grant（限频 10 分钟 5 次，可吊销，全程审计）。

---

## 各角色使用说明

### 1. 平台管理员（platform_admin）

#### 1.1 获取 Bootstrap Token

平台首次初始化需要 Bootstrap Token，它就是环境变量 `QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN` 的值：

- 运行过 `scripts/start_all.sh` 后，会自动生成并写入仓库根目录 `.env`，直接查看即可：

  ```bash
  grep QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN .env
  ```

- 生产部署时应自行生成并配置：`openssl rand -hex 32`

Token 仅用于创建**唯一一个**初始平台管理员；创建成功后 bootstrap 接口永久关闭（返回 `Bootstrap closed`）。

#### 1.2 初始化平台管理员

方式一（推荐）：打开 `http://localhost:3000/platform/login`，当平台尚未初始化时，登录页会自动切换为初始化表单，填入 Bootstrap Token + 用户名 + 密码即可。

方式二（API）：

```bash
curl -X POST http://localhost:8088/api/platform/bootstrap \
  -H "Content-Type: application/json" \
  -H "X-QwenPaw-Data-Bootstrap-Token: <你的 Bootstrap Token>" \
  -d '{"username": "platform-root", "password": "<强密码>"}'
```

之后通过 `http://localhost:3000/platform/login` 登录，进入租户管理页 `http://localhost:3000/user-center/tenants`。

#### 1.3 开通租户（Tenant Onboarding）

在平台控制台创建租户，或调用 API（一次性创建 租户 + 默认 Workspace + 首个 tenant_admin）：

```bash
curl -X POST http://localhost:8088/api/platform/tenants \
  -H "Content-Type: application/json" -b <platform cookie> \
  -d '{"name": "Acme", "admin_username": "acme-admin", "admin_password": "<强密码>"}'
```

新租户初始为 `provisioning` 状态，其 tenant_admin 账号为 `pending`，**在 Workspace 的 Neo4j 实例就绪前无法登录**。开发环境流程：

1. `POST /api/platform/tenants/{tenant_id}/provision`（或控制台按钮）把默认 Workspace 加入开通队列；
2. `scripts/common/workspace_provisioner.py`（`start_all.sh` 已后台启动）领取任务，用 Docker 启动该 Workspace 专属 Neo4j 并回写 `endpoint_ref/secret_ref`；
3. 就绪后租户与 tenant_admin 自动转为 `active`。

生产环境由外部 provisioner 完成后调用 `POST /api/platform/tenants/{tenant_id}/resume` 上报实例信息。

### 2. 租户管理员（tenant_admin）

账号由平台管理员开通租户时创建（或由其他 tenant_admin 在用户管理中创建）。

1. 登录：`http://localhost:3000/login`，输入平台管理员提供的用户名/密码；
2. 用户控制中心（侧边栏 `用户控制中心` 菜单组，`http://localhost:3000/user-center/*`）：
   - **Workspace 管理** `/user-center/workspaces`：创建新 Workspace（`POST /api/workspaces`，仅 tenant_admin）。新 Workspace 同样走 provisioner 队列拉起独立 Neo4j；
   - **成员管理** `/user-center/console`（用户 Tab）：创建用户（`POST /api/users`，指定 `role` 与 `workspace_id`）、禁用/启用、重置；在用户行管理角色绑定（`/api/roles/bindings`）。规则：不能授予高于自己的角色、不能修改自己的绑定；`tenant_admin` 角色只能绑定租户级作用域；
3. 业务使用：登录后进入 `http://localhost:3000` 数据语义管理台。图管理中可查看全租户 Workspace 列表并逐层下钻到成员图谱（跨 Workspace 会自动切换会话并刷新）。

### 3. Workspace 管理员（ws_admin）

账号由 tenant_admin（或其他有权限的管理员）在用户管理中创建，`role=ws_admin` 并绑定到具体 Workspace。

- 登录入口与 tenant_admin 相同（`http://localhost:3000/login`）；
- 用户/角色管理仅限自己管辖的 Workspace：可创建 `member`/`ws_admin`，**不能**创建或授予 `tenant_admin`；
- 业务侧拥有本 Workspace 的完整读写能力（MG 维护、数据源、语义编织、Excel 导入等），可只读浏览本 Workspace 所有成员的 TG/KG；
- 无法查看运维日志（`ops:logs`）。

### 4. 普通成员（member）

账号由 tenant_admin / ws_admin 创建并告知初始密码。

1. 登录 `http://localhost:3000/login`，首次建议前往 `http://localhost:3000/user-center/account/change-password` 修改密码（`POST /api/auth/change-password`）；
2. 登录后自动进入业务前端 `http://localhost:3000`：
   - 可读全局 MG（只读）、读写**自己**的 TG/KG（图管理直达"我的图谱"三张图视图）；
   - 数据源、数据集、指标等页面为只读；无语义编织、Excel 导入、模型配置权限；
3. 属于多个 Workspace 时，可在业务前端右上角切换，或调用 `POST /api/auth/switch-workspace`。

---

## 常用 API 速查

| 端点 | 说明 |
| --- | --- |
| `GET /api/auth/status` | 认证开关 / 是否需要平台 bootstrap |
| `POST /api/platform/bootstrap` | 创建唯一平台管理员（需 `X-QwenPaw-Data-Bootstrap-Token` 头） |
| `POST /api/platform/login` · `/logout` · `/me` | 平台管理员会话 |
| `POST /api/platform/tenants` | 开通租户（租户 + 默认 Workspace + tenant_admin） |
| `POST /api/platform/tenants/{id}/provision` · `/resume` · `/suspend` | 租户基础设施开通 / 恢复 / 暂停 |
| `POST /api/auth/login` · `/logout` · `/refresh` · `/me` | 租户用户会话 |
| `POST /api/auth/switch-workspace` | 切换当前激活 Workspace |
| `POST /api/auth/temporary-access` | 服务端代签 access（需 `X-QwenPaw-Data-Service-Secret`，仅内网） |
| `POST /api/auth/change-password` | 修改密码 |
| `GET/POST /api/users` | 用户列表 / 创建（管理员） |
| `GET/POST /api/workspaces` | Workspace 列表 / 创建（创建仅 tenant_admin） |
| `GET/POST/DELETE /api/roles/bindings` | 角色绑定管理（管理员） |
| `POST /api/platform/raw-cypher-grants` | 平台特批一次性 Raw Cypher grant |

## 服务端代签 access（Host cron / IM）

Host 的定时任务与 IM 渠道没有用户 cookie，但仍需按**当前**成员关系拿到一张 access。该端点只接受身份三元组，`role` 由 Auth 现算，调用方无法指定。`ttl` 可省略，省略即取本部署的 `access_token_ttl`；

```bash
curl -X POST http://127.0.0.1:8088/api/auth/temporary-access \
  -H "X-QwenPaw-Data-Service-Secret: <QWENPAW_DATA_SERVICE_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"...","tenant_id":"...","workspace_id":"..."}'
# → {"access_token": "<jwt>"}
```

签出的 token 与登录 access 同形（`typ=access`，含 `sub` / `tenant_id` / `active_ws` / `role` / `iat` / `exp`），不返回 refresh、不 `Set-Cookie`、不改用户的 `active_workspace_id`。

| 码 | 含义 |
| --- | --- |
| 200 | 签发成功 |
| 400 | 三元组缺失，或显式传入的 `ttl` 不在 `1 .. access_token_ttl`（默认 86400，可用 `QWENPAW_DATA_ACCESS_TOKEN_TTL` 覆盖，上限为 refresh ttl 7 天） |
| 403 | 服务密钥错误、用户不属于该租户、租户停用或无该 Workspace 权限 |
| 404 | 用户不存在或已停用 |
| 422 | 请求体字段类型不合法（FastAPI 默认校验） |
| 503 | 签名密钥或服务密钥为空（正常启动时缺省值会自动生成并持久化，见“运行模式”，因此端点默认开启，仅凭服务密钥放行） |

运维要点：

- **`QWENPAW_DATA_SERVICE_SECRET` 必须独立于 `QWENPAW_DATA_AUTH_SECRET`。** 后者是对称签名密钥，一旦作为请求头外泄，持有者可直接伪造任意 token（含 `platform_admin`）；独立密钥可单独轮换，且不会作废在线会话。
- **该端点只允许内网直连 8088。** `deploy/nginx.conf` 已显式 `deny all`，因为它凭服务密钥即可为任意 `user_id` 签票。
- **吊销的生效速度按服务而异。** 登出、改密、角色绑定变更、停用用户都会抬高 `users.token_revoked_before`，但各服务据此拦截的时机不同：Auth 自身端点每请求现查控制面，**立即**生效；Context 靠 5 分钟一轮的 `revocation_sync_loop` 同步，**最坏延迟 5 分钟**；Host 的入口只做 HMAC 签名 + `exp` 校验，不查吊销水位线，**已签发的 access 会一直有效到自然过期**。因此 access ttl（默认 24h，也是 cookie / 代签票的寿命）就是 Host 侧的吊销暴露窗口；需要更快收敛就调低 `QWENPAW_DATA_ACCESS_TOKEN_TTL`。权限（而非身份）仍由下游每请求现算。
- 审计事件为 `service_access_issued` / `service_access_denied`。

## 开发与测试

```bash
# 后端测试（仓库根目录）
uv run --with pytest --with pytest-asyncio pytest packages/qwenpaw-data-auth/tests -q

# 前端（登录页与用户控制中心已合入 Context 前端）
cd packages/qwenpaw-data-context/frontend && npm run dev
```
