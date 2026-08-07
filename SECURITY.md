# Security Policy

## Supported Versions

QwenPaw-Data is pre-1.0 software. Only the latest release on the default
branch receives security fixes.

## Deployment Model

QwenPaw-Data is designed for **local-first, single-user deployments**. All
services bind to `127.0.0.1` by default. Before exposing any service beyond
loopback, read the "Security Model and Known Limitations" section in
`README.md` and configure `DATAPAW_API_TOKEN` or scoped `DATAPAW_API_KEYS`,
plus an exact `DATAPAW_CORS_ORIGINS` origin allowlist.

Known, intentionally documented limitations:

- The Host defaults to a per-session Docker workspace. The explicit
  `workspace_type="local"` / `--workspace local` escape hatch executes agent
  shell commands on the host without a sandbox. Docker resource limits,
  egress network policy and non-root execution are not yet applied (roadmap),
  so the container boundary is not a hardened multi-tenant sandbox.
- Scoped API keys provide endpoint authorization but not a multi-user identity
  provider or user lifecycle/RBAC system.
- Authentication-failure and request-rate limiter state is process-local;
  multi-worker or horizontally scaled deployments require a shared limiter.
- Outbound import callbacks are disabled unless their exact origin is listed
  in `DATAPAW_CALLBACK_ALLOWLIST`; DNS answers are pinned per request and
  request/response sizes and total duration are bounded.
## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security problems.

Instead, report vulnerabilities privately via GitHub Security Advisories
("Report a vulnerability" on the repository's Security tab). If that is not
available to you, contact the maintainers listed in the repository profile.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (proof-of-concept if possible).
- Affected version/commit and environment details.

We aim to acknowledge reports within 7 days and to release a fix or
mitigation guidance within 90 days of confirmation. We will credit
reporters in release notes unless you request otherwise.
