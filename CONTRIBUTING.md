# Contributing to QwenPaw-Data

Thanks for your interest in contributing! This document describes how to set
up a development environment, verify your changes, and submit them.

## Development Setup

Requirements: Windows 11, macOS, or Linux; Python >= 3.11; [uv](https://docs.astral.sh/uv/);
Node.js >= 22.22 (for the DataBridge frontend), Docker (for local Neo4j).

```bash
git clone https://github.com/agentscope-ai/QwenPaw-Data.git
cd QwenPaw-Data
cp .env.example .env          # then fill in NEO4J_PASSWORD etc.
uv sync --all-packages        # installs all workspace packages + dev deps
```

The repository is a uv workspace with four packages under `packages/`:
`qwenpaw-data-context` (DataBridge), `qwenpaw-data-host-core`, `qwenpaw-data-cli`, and
`qwenpaw-data-skills`.

## Verifying Changes

Every change must pass the standard verification pipeline before review:

```bash
bash scripts/verify.sh              # compileall + pytest + API smoke test
bash scripts/verify.sh --frontend   # additionally run frontend lint + build
```

On Windows, run the same constituent checks directly from PowerShell:

```powershell
uv run python -m compileall -q packages scripts
uv run pytest -q
npm --prefix packages/qwenpaw-data-context/frontend run lint
npm --prefix packages/qwenpaw-data-context/frontend run build
```

Please add or update tests alongside behavior changes. Package tests live in
`packages/<name>/tests/`; cross-package tests live in `tests/`.

## Coding Guidelines

- Match the style of the surrounding code; keep changes minimal and focused.
- `async` endpoints must not perform blocking I/O directly — use
  `asyncio.to_thread` or async drivers.
- Never bind services to non-loopback addresses by default, and never add
  endpoints that forward user-supplied URLs without allowlisting (see
  `context_manager/net_guard.py`).
- Do not commit credentials; use `.env` (git-ignored) for local secrets.

## Commit and PR Conventions

- One logical change per commit, using Conventional Commit prefixes
  (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`).
- Sign off your commits (DCO): `git commit -s`. By signing off you certify
  the [Developer Certificate of Origin](https://developercertificate.org/).
- PRs should describe what changed, why, and how it was verified.

## Reporting Issues

- Bugs and feature requests: GitHub Issues.
- Security vulnerabilities: **do not** open a public issue — see
  [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under
the Apache License 2.0 (see [LICENSE](LICENSE)).
