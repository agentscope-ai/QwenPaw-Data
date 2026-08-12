# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `datapaw datasource` now covers the full lifecycle: `get`, `create`
  (with optional pre-save connection test), `update`, `delete`, and `test`,
  all with masked credential output.
- New `datapaw semantic` command group: table-driven CRUD for business
  domains, datasets, columns, dimensions, dataset-dimension bindings,
  metrics, and metric formulas; Excel workbook import; and weave-task
  management (`submit` with `--wait`, `list`, `kill`).
- `SemanticConfigClient` in `datapaw-host-core` for the authenticated
  `/api/semantic-config` REST surface with pagination validation and the
  unified error protocol.
- Deterministic semantic-CLI smoke test (`examples/semantic_smoke_test.py`)
  covering datasource lifecycle, workbook import, semantic CRUD with partial
  updates, batch deletion, and a real weave publish, wired into the CI smoke
  job.

### Fixed

- Semantic-config partial updates no longer erase omitted fields: the
  repository UPDATE statements previously overwrote every column, so a
  partial payload hit NOT NULL constraints (HTTP 500) or silently nulled
  stored values. All seven resource repositories now preserve fields that
  are not part of the request.
- `datapaw semantic weave submit --wait` now recognizes the upper-case
  terminal states reported by DataBridge (`SUCCESS`/`FAILED`/`KILLED`)
  instead of polling until timeout.
- Added the missing `get_dataset_columns` MCP tool and corrected the tool
  names advertised by the `bi-semantic-layer-guide` skill
  (`get_metric`, `get_dimension`, `list_dimensions_of_metric`,
  `get_dataset`), which previously led agents to call non-existent tools;
  a new alignment test keeps the skill guide and the MCP registry in sync
  (#19).

## [0.1.2] - 2026-08-10

### Added

- Dependabot configuration covering the Python (uv) workspace, the frontend
  npm workspace, and GitHub Actions.
- Persistent SQLite job storage with idempotency keys, leases, retries, expiry,
  and restart recovery.
- Request-level resource budgets and stable machine-readable API error codes.
- Docker-first workspace isolation, path containment, and process-group cleanup.
- Release, compatibility, public-history, SBOM, and integration-test tooling.
- Public package metadata, community templates, support policy, and a PyPI
  Trusted Publishing workflow.
- Executable SQLite/PostgreSQL demo data, expanded `datapaw doctor` diagnostics,
  and a deterministic real-CLI/DataBridge/SQL smoke test.
- Native Windows lifecycle and deterministic demo entry points, PowerShell
  workspace execution, cross-process session locking, and a Windows CI gate
  that exercises real initialization and DataBridge startup.

### Changed

- Locked development and CI validation to AgentScope 2.0.6, which the
  published `>=2.0.5,<2.1` range already resolves for end users.
- Services bind to loopback by default and use real scoped bearer-key checks.
- CORS and outbound callbacks require explicit allowlists.
- Import contracts now live in a transport-neutral application layer.
- Frontend routes are split into lazy chunks and hook lint is warning-free.
- The frontend baseline is now Node.js 22.22+, React 19, React Router 8, and
  Vite 7.

### Fixed

- Windows console-safe liveness probes, forced UTF-8 I/O on the native
  Windows CI job, and the ten test failures unmasked by the console fix.
- Synced the `datapaw-context` requirements lockfile with its `pyproject.toml`
  and made it universal.

### Security

- Raised the pypdf minimum to 6.15.0 to address CVE-2026-71852 and
  CVE-2026-71870.
- Removed unsafe development forwarding/process endpoints and internal assets.
- Added upload, query, response, callback, authentication, and rate limits.
- Added dependency, secret, license, CodeQL, and SBOM CI gates.
- Upgraded PostCSS to 8.5.26 to address GHSA-r28c-9q8g-f849 and
  GHSA-fxqj-rqcc-2cmp, upgraded pytest to 9.1.1 to address
  GHSA-6w46-j5rx-g56g, and extended CI auditing to development dependencies.
- Raised the MCP Python SDK minimum to 1.28.1 across all install paths to
  address CVE-2026-52869, CVE-2026-52870, and CVE-2026-59950.
- Raised the python-dotenv, Requests, and Vite minimums to the locked patched
  releases to address CVE-2026-28684, CVE-2026-25645, CVE-2025-58751, and
  CVE-2025-58752.
- Added PyPI Package URLs and PEP 639 SPDX license expressions to Python SBOM
  components so scanners cannot confuse ecosystems or miss modern license
  metadata.
- Upgraded to React Router 8.3.0, the patched release for
  GHSA-qwww-vcr4-c8h2, and removed the temporary SPA-only audit exception.

## [0.1.1] - 2026-08-09

### Changed

- Rewrote the `datapaw-context` package README in English for PyPI.
- Corrected repository URLs in package metadata and documentation to the
  `agentscope-ai/QwenPaw-Data` GitHub organization.

## [0.1.0] - 2026-08-05

### Added

- Initial local-first open-source baseline.

[Unreleased]: https://github.com/agentscope-ai/QwenPaw-Data/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/agentscope-ai/QwenPaw-Data/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/agentscope-ai/QwenPaw-Data/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/agentscope-ai/QwenPaw-Data/releases/tag/v0.1.0
