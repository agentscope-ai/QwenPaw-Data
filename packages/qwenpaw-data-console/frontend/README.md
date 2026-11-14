# QwenPawData Host Core Frontend

Standalone React frontend for `qwenpaw-data-host-core`.

The application owns the Session / Chat / Segment / Plan / Artifact / Trace
console and talks to the Host Core `/api` routes. Context Management and
DataBridge remain in `packages/qwenpaw-data-context/frontend`.

## Local development

Start the Host Core API:

```bash
export QWENPAW_DATA_HOME="$HOME/.qwenpaw-data"
export QWENPAW_DATA_DB_URL="sqlite+aiosqlite:///${QWENPAW_DATA_HOME}/host.sqlite3"
uvicorn qwenpaw_data.host.core.api.app:app --reload --port 8000
```

When `QWENPAW_DATA_AUTH_SECRET` is set, also start the Auth API on port `8088` and
sign in through the DataBridge frontend before opening the Host console.

Start this frontend:

```bash
cd packages/qwenpaw-data-console/frontend
npm install
npm run dev
```

Open `http://localhost:3100/console`.

Environment variables:

- `VITE_API_BASE_URL`: optional absolute Host Core API URL.
- `QWENPAW_DATA_HOST_API_BASE_URL`: Vite development proxy target; defaults to
  `http://localhost:8000`.
- `VITE_AUTH_API_URL` / `VITE_AUTH_URL`: Auth API URL. Development requests
  to `/api/auth/me`, `/api/auth/refresh`, and `/api/auth/switch-workspace` use
  the local proxy; production builds use this absolute URL when configured.
- `VITE_AUTH_FRONTEND_URL`: login frontend used for authentication redirects.
- `VITE_CONTEXT_FRONTEND_URL`: DataBridge frontend entry; defaults to
  `http://localhost:3000`.

## Theme

The color palette, typography, radii, and control sizing are defined once in
`src/theme.ts` and passed through `@agentscope-ai/design` `ConfigProvider`.
Page CSS consumes the emitted semantic CSS variables and should not patch
Ant Design component colors directly.
