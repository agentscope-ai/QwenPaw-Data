# Console frontend

Shared React chat application for the official Host HTTP/SSE contract.
Product branding is supplied by `ConsoleConfig` at runtime, through
`/console-config.js`; no domain-specific rebuild is required.

From the repository root, run `python scripts/build_console.py` to build
both packaged UIs. Run `npm test --prefix packages/qwenpaw-data-console/frontend`
for stream projection and API contract checks.

For frontend development, `npm run dev` proxies local APIs to a Console
running on port 18768. The dev server listens on 18769.

Optional `QWENPAW_DATA_CONSOLE_SMOKE_PATH` points to a real Host replay JSON
containing `chat` and `events`. The contract test checks chunked SSE replay.
