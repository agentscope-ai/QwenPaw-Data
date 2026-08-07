# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

- Services bind to loopback by default and use real scoped bearer-key checks.
- CORS and outbound callbacks require explicit allowlists.
- Import contracts now live in a transport-neutral application layer.
- Frontend routes are split into lazy chunks and hook lint is warning-free.
- The frontend baseline is now Node.js 22.22+, React 19, React Router 8, and
  Vite 7.

### Security

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

## [0.1.0] - 2026-08-05

### Added

- Initial local-first open-source baseline.

[Unreleased]: https://github.com/QwenLM/QwenPaw-Data/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/QwenLM/QwenPaw-Data/releases/tag/v0.1.0
