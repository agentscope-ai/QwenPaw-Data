# Context Manager Architecture

Context Manager (CM) builds and maintains a semantic graph between the Host and raw data. It organizes data structures, business definitions, and their relationships into retrievable semantic information. CM owns graph construction, maintenance, and retrieval, exposing context and tools through API / MCP. The Host owns user-task interpretation, planning, tool selection, and the final explanation.

## Four-layer architecture

**From top to bottom, the layers are application, business, infrastructure, and contracts. Cross-layer dependencies must point downward; reverse dependencies are forbidden.** The [Mermaid diagram](architecture.mmd) shows the allowed dependency direction on the left and a typical graph-write flow on the right. Returning results to an upper layer does not constitute a reverse code dependency. Module paths are relative to `packages/qwenpaw-data-context/src/context_manager/`.

| Layer | Modules | Responsibilities |
| --- | --- | --- |
| Application | `api` / `mcp` | Perform necessary protocol adaptation: parse requests, bind trusted identities, delegate to business services, and translate responses and errors. Do not implement business logic. |
| Application | `runtime` / `server.py` | Assemble and start services, select Workspaces, and manage shared resources, execution budgets, and shutdown. |
| Business | `cypher` | Validate model-authored native reads for read-only syntax and the current user's scope, execute statements unchanged through Store, and project bounded public results. |
| Business | `metadata` | Publish committed semantic configuration and physical structures to the Metadata Graph; manage personal graph synchronization. |
| Business | `knowledge` | Extract entities, events, and relationships from documents; coordinate document import, deletion, and graph writes. |
| Business | `user` | Build user profiles and long-term memory from conversations; manage extraction progress and manual operations. |
| Business | `embedding` | Coordinate vector rebuilds, recovery, and job progress; use models and Store for computation and writeback. |
| Business | `cron` / `migration` | Coordinate persistent scheduled jobs and deployment schema repair, respectively. |
| Business | `compatibility` | Support existing Trace, Search, and Explorer workflows and legacy protocols. These modules contain business semantics and are not general infrastructure. |
| Infrastructure | `heartbeat` | Refresh an owned row on a fixed interval, serialize one claim until the caller's transaction commits, and fail abandoned in-flight business rows after their heartbeat goes stale. |
| Infrastructure | `executor.py` | Bound work-unit admission and synchronous thread execution; report rejected admission with `BlockingIOError` and a log entry. Runtime chooses and owns executors; business services compose work units. |
| Infrastructure | `process` | Provide shared algorithms and interfaces for chunking, extraction, deduplication, embedding, and entity alignment. |
| Infrastructure | `sql` / `sql/connectors` | Resolve authorized datasource configuration, execute typed read/create/modify statements, and manage result files; connectors own drivers, connections, and transactions. |
| Infrastructure | `transaction` | Execute prepared graph plans atomically, resolve references, and return detached results without choosing business writes. |
| Infrastructure | `store` / `store/neo4j` | Define Draft / Record contracts, graph primitives and atomic database transactions. Backend adapters own concrete protocols and physical constraints. |
| Infrastructure | `auth` / `model` / `db.py` | Own Scope issuance and enforcement, model access and client lifecycle, and the shared CM SQL connection pool, respectively. |
| Contracts | `schema` / `settings` | Define canonical graph structures and relational declarations, and read and validate deployment configuration, respectively. |
| Shared leaf utilities | `utils` | Provide domain-neutral value operations for all four layers, without dependencies on other CM modules or orchestration responsibilities. |

Upper layers may use lower-layer capabilities directly without forwarding wrappers. Collaboration within a layer must have clear ownership and remain acyclic: for example, `process` consumes `store` data contracts, while Store does not depend on Process. `auth` and `model` are infrastructure; `sql/connectors` belongs to SQL infrastructure. They are not contract-layer utilities like stateless `utils`.

The business layer composes algorithms, chooses processing policies, and builds transaction plans; Store executes those plans atomically. Schema defines graph contracts, Auth controls access scope, and Runtime manages resources. Business Services and Repositories own module-specific use cases and persistence logic.

**The application layer is limited to assembly and lifecycle management; it must not implement concrete business logic.** API / MCP entry points provide only the protocol adaptation needed to invoke business services. Runtime binds authorized resources and execution policies. Business workflows, batching, retry decisions, business state transitions, and result aggregation remain in their business modules. Keep application code small, direct, and easy to follow. Removing a business module's dependency on Runtime must not move its business logic into Runtime or introduce extra forwarding layers or generic orchestration frameworks.

**Infrastructure is the shared foundation for all business modules and must expose sufficiently abstract, general-purpose capabilities.** Its contracts and implementations must be independent of individual business workflows. Do not add one-off operations, business-specific branches, or compensating logic to patch an individual workflow. Fix defects at the owning boundary; introduce shared capabilities for demonstrated reusable requirements, while keeping abstractions minimal.

### Change boundaries

Contract and infrastructure layers should normally remain stable when product or business requirements change. Prefer composing existing capabilities. When a concrete defect or missing capability belongs to a lower layer, inspect consumers and comparable implementations before choosing an abstraction. Keep implementation, contracts, errors, and resource ownership simple and consistent. Caution does not justify repeated validation, speculative fallback chains, or extra wrappers.

Fix incorrect representations or behavior at their owning boundary instead of adding compensating parsers or prompt patches downstream. When replacing an implementation, check compatibility requirements, migrate actual consumers, and remove superseded code, aliases, wrappers, and redundant tests in the same change. Keep cleanup within the directly affected scope.

Business modules use shared capabilities through explicit services, resources, or callbacks; they must not retain the entire `ContextRuntime`. Infrastructure must not depend on business or application modules. `schema` and `settings` must not depend on upper layers. Lazy imports, `TYPE_CHECKING`, and global lookups do not exempt code from these dependency rules.

## Identity, Workspaces, and resource ownership

- **Tenant** is the tenant boundary. `qwenpaw-data-auth` authenticates requests and resolves current membership into a trusted `AuthorizationContext`. The target Workspace must belong to that tenant and be accessible to the caller.
- **Workspace** is the graph-backend routing boundary. Each Workspace maps to a configured graph backend route; it does not require a dedicated physical server. `ContextRuntime` resolves targets through the Resolver, and the Pool creates, reuses, and closes `WorkspaceRuntime` instances by `(tenant_id, workspace_id)`.
- **User** is the personal-data ownership boundary within a Workspace graph. Users share the graph backend, with personal data isolated by `workspace_id` and `owner_id = user_id`. The requesting identity and the data owner are distinct concepts.

`GraphScopeAuthority` issues `ScopeBinding` from trusted identities. Store validates the issuer, access mode, tenant, and Workspace, and enforces owner, datasource, and zone restrictions. Permission checks use the current `active_workspace_role`: ordinary members read and write their own graphs; Workspace administrators may read across owners within their authorized scope, while personal writes still target themselves. Shared Metadata uses `owner_id = "__global__"`. Members read it through an explicit shared Scope, writes require Workspace administration, and maintenance across owners uses dedicated authorization.

`ContextRuntime` owns process-wide shared SQL and execution resources, and manages task-manager startup and shutdown. The Workspace Pool owns each Workspace's Store, Glossary, and related resources. Modules own their internal service assembly and business workflows. See [Runtime](../src/context_manager/runtime/README.md) for routing, lifecycle, and model configuration boundaries, and [Auth](../src/context_manager/auth/README.md) for Scope rules.

## Settings boundaries

`settings` is the contract-layer entry point for deployment configuration. It reads, parses, validates, and groups configuration values; it does not create Stores, model clients, schedulers, or executors.

| Belongs in Settings | Remains with the owning module |
| --- | --- |
| Backend connections, deployment secrets, `QWENPAW_DATA_HOME` | User Provider credentials and model choices; dynamic Workspace and datasource registrations |
| Timeouts, concurrency, execution budgets, cache capacity, retention limits | Request queries, filters, pagination, operation parameters, and business or algorithm policies |
| Deployment switches and initial scheduling defaults | Persisted schedules, business directory layouts, running jobs, clients, and cached contents |

Distinguish configuration defaults from hard limits; resource owners enforce the limits. Each new setting needs a concrete operational purpose and documented default, unit, range, scope, and activation timing. A parameter does not belong in Settings merely because it is adjustable or affects cost. `clear_settings()` only clears the configuration cache; it does not rebuild existing resources or provide runtime hot reload. See [Settings](../src/context_manager/settings/README.md) for detailed examples.

## Shared infrastructure ownership

Each category of configuration, declarations, and data contracts has one owning module. Business code reuses these objects directly and owns its processing policies.

| Location | Core classes / objects | Shared responsibilities |
| --- | --- | --- |
| [settings](../src/context_manager/settings/__init__.py) | `Settings`, the `*Settings` classes | Parse deployment environment variables by responsibility; `get_settings()` caches process configuration. User model choices still come from the database. |
| [schema/model](../src/context_manager/schema/model.py) | `PropertySpec`, `KeySpec`, `NodeSpec`, `EdgeSpec`, `GraphSchema` | Define property types, field provenance, key rules, nodes, edges, validation, and export contracts. |
| [schema/graph](../src/context_manager/schema/graph.py) | Declarations such as `METRIC`, `GRAPH_SCHEMA` | Declare the actual graph structure centrally. Business code, Store, API, and index generation share the same canonical objects. |
| [schema/relational](../src/context_manager/schema/relational.py) | SQLAlchemy `Table`, `metadata`, `external_tables` | Declare CM-owned SQL tables and required columns in external tables. Owned tables participate in schema creation; external tables support dependency checks. |
| [db](../src/context_manager/db.py) | `CMDatabase` | Own the shared CM SQL connection pool, driver adaptation, startup table creation, missing-column checks, and shutdown. Business Repositories borrow connections. |
| [store/models](../src/context_manager/store/models.py) | `NodeDraft` / `EdgeDraft`, `NodeRecord` / `EdgeRecord` / `SubgraphRecord` | Define shared algorithm/storage data contracts. Drafts describe facts awaiting processing; Records represent snapshots of persisted results. |
| [store](../src/context_manager/store/base.py) | `GraphStore` | Define graph primitives and the atomic database transaction boundary. |
| [transaction](../src/context_manager/transaction/README.md) | `Transaction`, `StoreOperation`, `TransactionPlan`, `TransactionResult` | Resolve references, execute prepared operations, and validate results before committing one atomic transaction. |
| [store/neo4j](../src/context_manager/store/neo4j/physical_schema.py) | `Neo4jGraphStore`, `Neo4jRuntime`, `Neo4jSchemaCatalog` | Implement graph contracts and transactions; derive label-local key uniqueness constraints, range and full-text indexes from logical declarations. Vector search scores scoped candidates directly; the default catalog declares dimensions without ANN indexes. |
| [auth](../src/context_manager/auth/scope_authority.py) | `GraphScopeAuthority`, `ScopeBinding` | Issue and validate access scopes, enforced by services and Store at execution boundaries. |
| [runtime](../src/context_manager/runtime/README.md) | `ContextRuntime`, `WorkspaceResolver`, `WorkspaceRuntimePool`, `WorkspaceRuntime` | Separate process and Workspace ownership; coordinate startup, resource selection, caching, and shutdown in dependency order. |

Graph declarations are used directly: `GRAPH_SCHEMA.node("Metric") is METRIC`. `PropertySpec` defines types, editability, display policies, and field provenance. `KeySpec` defines key generation. `Glossary` manages mutable Workspace terms separately from the fixed graph structure.

Data objects have distinct roles: **Spec defines structure, Draft carries candidate facts, Record carries a read/write snapshot, Plan describes storage operations, and Result reports execution outcomes**. A Draft's `ref` is request-local; a Record's `key` is its persistent identity. Schema owns Specs. Store owns Drafts and Records; Transaction owns operation Plans and Results. Process and business services consume these shared contracts.

Relational declarations and resources have separate owners: `relational.py` declares tables, `CMDatabase` manages the pool, and business Repositories own reads and writes. Startup creates missing CM tables and checks required columns; external modules initialize their own tables before dependency checks. Missing columns cause errors; schema migrations run separately. Shared SQL and Neo4j commit independently.

## Implementation boundaries

Runtime's ownership of shared SQL, schedulers, embedding jobs, and history ingestion is a resource and lifecycle responsibility; the corresponding business workflows belong to their modules. `semantic_config` / `profiling` retain their own SQL sessions. Compatibility Trace/Search retains some independent configuration, and `TraceRepository` handles the legacy Trace graph structure. Do not describe every business path as already unified behind GraphStore.
