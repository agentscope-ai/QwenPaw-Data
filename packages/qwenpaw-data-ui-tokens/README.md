# @qwenpaw-data/ui-tokens

Shared visual foundations for the Data Console and Context frontends.

- `tokens.json` is the only source of primitive values.
- `src/index.ts` maps those values into the shared Ant/AgentScope theme.
- `src/tokens.css` is generated for CSS Modules and global compatibility aliases.
- Product-specific layout and visualization tokens stay in their owning frontend.

After editing `tokens.json`, run `npm run build` and commit the generated CSS.
Use `npm run check` in CI to reject stale generated output.
