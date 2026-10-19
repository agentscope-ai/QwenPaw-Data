# Compatibility policy

QwenPaw-Data is pre-1.0 software. Minor releases may contain breaking changes,
but deprecations are announced in `CHANGELOG.md` whenever practical. Patch
releases are intended to remain API compatible within the same minor line.

## Release compatibility

The four Python distributions are released as one coordinated `0.3.x` line.
Consumers should keep `qwenpaw-data-skills`, `qwenpaw-data-context`,
`qwenpaw-data-host-core`, and `qwenpaw-data-cli` on the same minor version.
Consumers that run the engine service must install
`qwenpaw-data-host-core[service]>=0.3,<0.4`.

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
- Dependabot checks routine version updates monthly, grouping minor/patch
  changes with at most one open version PR per ecosystem (npm, uv, and GitHub
  Actions). This is a concurrency limit, not a monthly PR quota.
- Major version upgrades are planned and tested together with related tooling.
  The `allow.update-types` filter applies only to version updates; security
  updates remain eligible independently of that filter and monthly schedule.
  Explicit dependency exceptions in `.github/dependabot.yml` still apply.
