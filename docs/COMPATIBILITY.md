# Compatibility policy

QwenPaw-Data is pre-1.0 software. Minor releases may contain breaking changes,
but deprecations are announced in `CHANGELOG.md` whenever practical. Patch
releases are intended to remain API compatible within the same minor line.

## Release compatibility

The [0.4.0 release](../CHANGELOG.md#040-release-highlights) includes seven Python
distributions. Use matching versions for the components you install:

| Package | Coordinated version | Notes |
| --- | --- | --- |
| `qwenpaw-data-cli` | 0.4.0 | Installs the `qwenpaw-data` command; depends on Host Core |
| `qwenpaw-data-host-core[service]` | 0.4.0 | Host HTTP/SSE service; depends on Skills, Auth, and Tracing |
| `qwenpaw-data-context` | 0.4.0 | DataBridge; use `[duckdb]` when connecting DuckDB datasets |
| `qwenpaw-data-skills` | 0.4.0 | Built-in analytical skills |
| `qwenpaw-data-console` | 0.4.0 | Bundles the standalone Console and DataBridge UI; no Node.js required |
| `qwenpaw-data-auth` | 0.4.0 | Shared identity and authorization for Host and DataBridge |
| `qwenpaw-data-tracing` | 0.4.0 | Shared tracing for Host and DataBridge |

Dataset-scoped SQL and persisted SQL result references were introduced in 0.3.4;
upgrade Host and DataBridge together. Scope enforcement remains opt-in; tools must still
validate columns, periods, regions and economic definitions. These runtime
checks do not guarantee a model will choose the right tool or produce correct
analysis without domain validation.

The optional Console can also be released independently with a
`console-vX.Y.Z` tag. A coordinated `vX.Y.Z` tag publishes the full package set.

## Validated environments

| Component | Supported / validated baseline |
| --- | --- |
| Operating system | Current macOS, Ubuntu LTS, and Windows 11 |
| Windows | Native PowerShell 7 workflow; WSL2 is the recommended fallback |
| Python | 3.11 through 3.13 |
| Node.js | 22.22.0 or newer on the Node 22 LTS line |
| Docker | A current Docker Engine/Desktop with Compose v2 |
| Neo4j | 5.x |
| PostgreSQL | 15 and 16 |
| MySQL | 8.0 |

CI validates the Python workspace, CLI, API smoke test, and frontend build on
native Windows as well as the existing macOS/Linux jobs. Real database and
Docker sandbox integration tests remain Linux-hosted. See
[`WINDOWS.md`](WINDOWS.md) for native prerequisites and the WSL2 fallback.

## Dependency policy

- Direct runtime dependencies use tested lower and upper compatibility bounds.
- `uv.lock` and `package-lock.json` are the reproducible development and CI
  inputs; published Python metadata remains standards-compliant without local
  workspace source overrides.
- Compatibility-range changes require tests, a changelog entry, and refreshed
  dependency/SBOM audits.
- Only the latest release line receives security fixes before 1.0.
- Dependabot checks routine version updates for directly declared dependencies
  monthly, grouping minor/patch changes with at most one open version PR per
  ecosystem (npm, uv, and GitHub Actions). This is a concurrency limit, not a
  monthly PR quota. Indirect dependencies can still change as needed when
  updating a direct dependency; they are not independent routine update targets.
- Major version upgrades are planned and tested together with related tooling.
  The `allow.update-types` filter applies only to version updates; security
  updates remain eligible independently of that filter and monthly schedule.
  Vulnerable indirect dependencies remain eligible for security updates.
  Explicit dependency exceptions in `.github/dependabot.yml` still apply.
