# Console feature boundary

`DataConsole` is the standalone conversation console for the new
`datapaw-host-core/api`. It lives in `packages/datapaw-host-core/frontend`
and is not a UI wrapper around the legacy `qwenpaw-plugin` console.

## Structure

- `src/pages/`: route-level screens only (`Console`, `Sessions`, and settings).
- `src/layouts/ConsoleLayout/`: shared `/console` route shell and outlet context.
- `components/`: Console navigation, workspace, and AgentScope Runtime renderers.
- `mocks/`: removable demo fixtures and the AgentScope Runtime mock adapter.
- `api/`: wire types, REST client, and authenticated SSE reader for `/api`.
- `model/`: event projection, navigation policies, and the Console controller hook.

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

`mocks/runtime.ts` remains only as an explicit visual/test fixture. Production
components do not import or create the mock Runtime session/response.
