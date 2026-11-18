---
status: accepted
date: 2026-11-18
---

# Keep shared behavior on the internal baseline and adapt OSS at its boundaries

Upgrade the [internal baseline](../../CONTEXT.md) first. Keep one implementation of sessions, settlement confirmation and deduplication, scheduled execution, skill execution, and authorization. OSS adapts how an installation presents or connects those capabilities; it does not put a second business API in front of them.

| Boundary | What OSS adapts | What stays shared |
| --- | --- | --- |
| Console and DataBridge UI | In local mode show the workspace and settings, not user center, password, logout or workspace-switch actions. Preserve account controls in multi-user mode. | Auth determines the mode and identity. Sessions, settlement items and cron jobs still have owners. A UI switch is not an access check. |
| Host to DataBridge | For settlement REST calls in local mode, use the configured machine credential only for a loopback HTTP(S) destination. Do not fall back to it for remote destinations or failed multi-user delegation. | Host schedules confirmation; DataBridge authorizes, deduplicates and writes the item. A successful scheduling response is not evidence of a write. |
| Local storage upgrades | Recognize historical layouts under stable IDs. Discover without writing; any future SQLite import requires a backup, a dry run and explicit operator action. | The current Host owns its schema and ownership rules. Startup must not silently convert old owners or keys, or mistake a new database for a completed migration. |

We rejected a full OSS facade: if it reimplemented settlement confirmation, every change to deduplication or permissions would need a second port. We also rejected scattering edition checks through business logic. Use the narrowest existing extension point in the owning package; if none exists, propose one there. For example, OSS may leave an optional integration disabled without changing how the shared integration works when enabled. This is a boundary for future changes, not a claim that today's tree is a drop-in overlay.

A baseline change that alters authorization, data ownership or stored formats requires review, even if the code merges cleanly. Follow the [baseline update checklist](../RELEASING.md#updating-the-internal-baseline) and the [legacy SQLite procedure](../../packages/qwenpaw-data-cli/README.md#find-a-pre-upgrade-local-sqlite-store).
