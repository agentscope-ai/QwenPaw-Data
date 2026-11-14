# Console feature boundary

`DataConsole` is the standalone conversation console for the new
`qwenpaw-data-host-core/api`. It lives in `packages/qwenpaw-data-console/frontend`
and is not a UI wrapper around the legacy `qwenpaw-plugin` console.

## Structure

- `src/pages/`: route-level screens only (`Console`, `Sessions`, and settings).
- `src/layouts/ConsoleLayout/`: shared `/console` route shell and outlet context.
- `components/`: Console navigation, workspace, and AgentScope Runtime renderers;
  styled components use `ComponentName/index.tsx` plus `index.module.less`.
- `index.module.less`: page/component-owned CSS Modules live beside their owner;
  `features/console/index.module.less` contains only cross-component states,
  responsive rules, and shared design primitives.
- `api/`: wire types, REST client, and authenticated SSE reader for `/api`.
- `model/artifacts/`: artifact access, HTML preview, and Markdown serialization.
- `model/chat/`: the Console controller hook and chat/session request helpers.
- `model/plan/`: plan projection and editor policies.
- `model/runtime/`: SSE projection, Runtime message mapping, timeline, and queue policies.
- `model/session/`: Console-owned session list and polling policies.
- `model/settlement/`: settlement card projection and loading hook.

Cron scheduling and catalog filtering live under their owning features in
`src/features/cron/model/` and `src/features/sessions/model/`. Route builders
shared by the Console shell and those pages live in
`src/shared/navigation/sessionNavigation.ts`.

The `/console` route owns the entire Host Core frontend viewport. DataBridge
remains a separate Context Management frontend and is reached through its
configured external URL.

## Protocol rules

- REST is the acknowledgement channel for user writes.
- SSE only carries Agent execution increments.
- Deduplicate using `(chat_id, sequence_number)`.
- Later events replace earlier projections for the same entity.
- A Segment appears only when the server emits the completed `segment` object.
- Input Queue is local frontend state; only an explicit Steer calls the backend.
- Wire `Chat.status` currently uses `canceled`, while the terminal SSE response
  uses `cancelled`; normalize this difference only in the view-model layer.

## Migration state

The `/console` page now loads the requested `session_id` (or the most recently
updated session), initializes Chat projections from Snapshot, and resumes a
running Chat from its `last_sequence_number`. New input is acknowledged through
`POST /console/chat`; the returned Chat and its SSE stream update the same
projection used by the conversation and workspace shell.

Large artifact-list visual checks use the development-only `mock_artifacts`
query parameter implemented by `SessionWorkspace`; production builds do not
create mock Runtime sessions or responses.
