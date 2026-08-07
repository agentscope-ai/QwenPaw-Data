# Compatibility policy

QwenPaw-Data is pre-1.0 software. Minor releases may contain breaking changes,
but deprecations are announced in `CHANGELOG.md` whenever practical. Patch
releases are intended to remain API compatible within the same minor line.

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
