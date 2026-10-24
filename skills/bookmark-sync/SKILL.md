---
name: bookmark-sync
description: Safely inspect, preview, mirror, restore, diagnose, or calibrate local Chrome, Edge, Safari, Brave, Vivaldi, and Opera bookmarks on macOS. Use for explicit cross-browser bookmark directions, backup selection, and Chrome/Edge cloud-reinjection repair.
---

# Bookmark Sync

Use `bookmark-sync` when installed for routine operations. For `cloud-safe`, locate the repository from `BOOKMARK_SYNC_ROOT` or the current workspace and run its `./sync-bookmarks` from the repository root; an installed CLI may be stale and lack current safeguards. Never embed a username or absolute home path in reusable commands.

## Safety

- Inspect with `--list` and preview an unfamiliar direction with `--mode preview`.
- Specify real Agent runs with explicit `--from` and `--to` values.
- Use the full store ID from `--list --json`, such as `chrome:Profile 1`, when a browser has multiple profiles.
- Keep backups enabled. They contain complete bookmark data and must not be published.
- If a write reports an unfinished operation, use `--recover` after authorization to close the target browser. Never use `--discard-recovery` without explicit user approval after inspecting the target and backup.
- Use `--auto-close` only after the user authorizes closing affected browsers.
- Cloud-safe is an explicit, destructive attempt to temporarily clear the selected Chrome/Edge target cloud bookmark tree. Use `--sync-strategy cloud-safe --allow-cloud-purge` only after explicit authorization.
- Cloud-safe requires Developer mode enabled once on the target profile: open `edge://extensions` (or `chrome://extensions`) and toggle "Developer mode" / "开发人员模式". Modern Chrome/Edge ignore `--load-extension` on established profiles otherwise, and the CLI now fails fast with this instruction instead of timing out. Never edit `Secure Preferences` directly; its HMAC protection treats tampering as corruption.
- For an explicitly authorized automated Edge cloud-reinjection repair, prefer the `cloud-safe` CLI command below. It backs up the target, uses a temporary Chromium bookmarks API extension to clear it, waits for cloud settling, then recreates the full source tree in source order through the same API so sync position metadata uploads correctly. File-level writes cannot set sync positions, so the bookmarks bar order only survives cloud merge when the rebuild happens through the browser API. It can time out and roll back; never claim it cleared Microsoft's cloud solely because the local tree reached zero.
- If the same target's previous `cloud-safe` attempt timed out, do not blindly repeat it. Report the prior failure and use only a materially different, user-approved recovery path.
- Microsoft's official account-level Reset sync is a separate Settings workflow, not exposed as a supported command/API in this project. Do not call the CLI equivalent to that reset, use undocumented endpoints, or open Settings automatically. If the user prohibits UI and the CLI attempt fails, stop and report the limitation. See [Microsoft's cloud reset procedure](https://learn.microsoft.com/en-us/deployedge/edge-learnmore-reset-data-in-cloud).
- When the CLI succeeds, verify the complete portable tree after the configured post-open check. Other synced devices can still reintroduce changes; do not promise permanent cloud convergence.
- Use `--sync-strategy auto` unless the user explicitly requests another strategy.
- `auto` directly mirrors with a backup, reopens supported sync-enabled Chromium targets for the full post-open observation window, then closes the browser and repairs detected drift with another backup.
- `auto` is a local repair, not a cloud purge. Later browser opens or other synced devices can re-inject bookmarks; do not promise permanent prevention.
- Verify the complete portable bookmark tree, including folder hierarchy, titles, URLs, and node order; matching counts alone are insufficient. Wait for the full post-open observation window before judging cloud reinjection, and distinguish post-open alignment from a repair verified only with the browser closed.
- Treat Brave, Vivaldi, and Opera cloud-synced profiles as uncalibrated; direct mode is supported when bookmark cloud sync is disabled.
- Arc and Firefox are unsupported.

## Commands

```bash
bookmark-sync --list --json
bookmark-sync --list-backups --json
bookmark-sync --from chrome --to edge safari --mode preview --json
bookmark-sync --from "chrome:Profile 1" --to "edge:Work" --mode preview --json
bookmark-sync --from chrome --to edge safari --auto-close
bookmark-sync --from chrome --to edge --sync-strategy cloud-safe --auto-close --allow-cloud-purge
./sync-bookmarks --from chrome:Default --to edge:Default --sync-strategy cloud-safe --auto-close --allow-cloud-purge --post-open-check edge --json
bookmark-sync --restore-backup /path/to/backup --restore-target edge --auto-close
bookmark-sync --recover --auto-close
bookmark-sync --doctor all --json
bookmark-sync --calibrate edge
```

Report backup paths, result counts, strategy, and verification after writes. Restore creates a rollback backup first. JSON stdout never includes bookmark titles or URLs; human diagnostics go to stderr.
