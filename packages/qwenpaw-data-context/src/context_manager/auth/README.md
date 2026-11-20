# CM Auth

CM Auth 接收 Auth 已经验证的身份，判断本次操作可以访问哪份图，并签发内部使用的 Scope。

- **Auth / `AuthorizationContext`**：Auth 确认请求者是谁、属于哪些 Workspace、具有什么角色；`AuthorizationContext` 携带这些可信结果。
- **`ScopeBinding`**：内部流通的通行证，明确本次操作能读写哪份图、哪个 owner、哪个 datasource。`user_id` 是请求者，`owner_ids` / `write_owner_id` 是被访问图的归属，二者不一定相同。
- **`GraphScopeAuthority`**：根据可信身份和本次操作要求，判断是否允许，再签发 Scope；也负责范围收窄和 Store 入口的 Scope 验证。

## 文件与关键类型

| 文件 | 类型 | 职责 |
| --- | --- | --- |
| `authorization.py` | `AuthorizationContext` 请求绑定、Workspace 身份归属校验 | 管理可信身份上下文，确认其属于当前 tenant / Workspace。 |
| `scope.py` | `ScopeBinding`、`AccessMode`、`WriteOwner` | 定义授权结果及其字段约束，只校验数据一致性。 |
| `scope_authority.py` | `GraphScopeAuthority` | 集中图访问范围、读写权限和 owner 选择规则，负责签发、收窄和准入。 |
| `repository.py` | `OwnerRepository` | 借用共享数据库读取 owner 用户名、有效用户与 Workspace 的角色绑定，供展示和后台任务重新授权使用；调用方决定所需角色及实例状态。 |

## 授权流程

1. HTTP middleware 通过 `qwenpaw-data-auth` 验证 token，并确认当前用户、tenant、Workspace 和角色，绑定可信 `AuthorizationContext`。
2. API 通过 Runtime 申请读写 Scope；Runtime 选择 Workspace，使用目标 Store 的 Authority 判断权限。
3. 业务将 Scope 传给 Store，Store 校验签发者与物理目标，再应用 owner、zone 和 datasource 条件。

```python
read_scope = runtime.issue_graph_read_scope(authorization, zone="metadata")
store = runtime.graph_store(read_scope)
```

嵌入宿主需提供等价的可信认证结果。

## 授权规则

- 图权限依据 `active_workspace_role`（当前 Workspace 的有效角色）；`role` 只是租户内最高角色的展示/审计信息，不能把其他 Workspace 的管理权限带入当前 Workspace。
- Scope 由当前 Store 的 `GraphScopeAuthority` 签发；伪造实例或其他 Authority 签发的 Scope 会被拒绝。
- Scope 收窄必须保持同一 tenant、workspace、授权主体 ID 和 owner 集合；已限定 datasource/zone 时不能换值或去掉限制。
- `WriteOwner.USER` 精确限定当前用户；`WriteOwner.WORKSPACE` 仅允许 Workspace / tenant 管理员维护权威图。重建通过 `issue_admin_scope()` 按已有节点的 owner 签发管理权限，保留发起管理员的用户身份。
- KG、UG 和个人 MG 使用同一用户归属边界；UG 引用解析读取用户自己的 MG/KG。
- 全局权威图统一使用 `GLOBAL_OWNER_ID = "__global__"`；`WriteOwner.WORKSPACE` 签发该 owner 的写入权限，`workspace_id` 保留当前工作区范围。个人图的 owner 为用户 ID。
- 权威 MG 的读取通过 `issue_read_scope(owner_id=GLOBAL_OWNER_ID)` 或 `metadata_source_scope()` 显式签发，由 Authority 限定到 `metadata`。未限定 zone 时补上 MG 限制；已有其他 zone 限制时拒绝，不改换访问范围。API 只传入请求目标和已有范围，不自行覆盖 zone。
- `narrow_write_scope()` 为多 datasource 业务计划签发原 write Scope 的子范围，不改变写入 owner。
- `issue_admin_scope()` 接受当前 Workspace 的 `ws_admin`、`tenant_admin` 或配置的管理角色（默认 `context_admin`）。可限定 owner、datasource、zone；数据写入仍要求明确的 write owner，未指定 owner 的 admin Scope 不能直接写数据。
- 用户发起 rebuild 使用该管理员的可信身份；后台恢复使用 Runtime 为已解析 tenant/Workspace 装配的系统身份。两者复用 `issue_admin_scope()`，不增加业务专用 Scope 类型或签发入口；系统身份不接受外部请求构造，也不从 Undo 数据取得授权。
- Store 还会将 Scope 与 `GraphTarget` 的 tenant/workspace 比较，防止有效 Scope 被跨图重放。
- Scope 只携带授权结果，不携带 Neo4j driver、Schema 对象或业务服务。

## 内部状态

`internal_read_scope()` 允许内部服务读取同一用户的 `_internal` 状态，保留 tenant、workspace、owner 和 datasource 边界，且只签发 READ。feedback 用它读取 MGSyncState；公开入口仍拒绝内部类型。
