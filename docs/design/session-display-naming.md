# Session display naming

QwenPaw-Data exposes two complementary session identifiers to clients:

- `title` is a semantic, user-editable label. When a session has no explicit
  title, the engine derives it from the normalized first user input, capped at
  60 characters. A derived title never replaces a non-empty title.
- `display_code` is a stable six-character uppercase hexadecimal fingerprint
  derived from the immutable session ID. It disambiguates repeated titles and
  is not editable.

Clients should render these together, for example `March GAAP review #7A3F2C`,
while rename operations edit only `title`. Search should match both fields.
`display_code` is a presentation aid, not an authorization token or globally
unique database key; API calls must continue to use `id`.

The code is computed at the API boundary, so existing sessions gain one without
a storage migration. Session creation paths may derive the same title
optimistically for immediate display, but the engine's first-chat rule is the
authoritative fallback for Console, PawApp delegation, cron, and API clients.
