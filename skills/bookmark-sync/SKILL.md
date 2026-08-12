---
name: bookmark-sync
description: Safely inspect, preview, mirror, restore, diagnose, or calibrate local Chrome, Edge, Safari, Brave, Vivaldi, and Opera bookmarks on macOS. Use for explicit cross-browser bookmark directions, backup selection, and Chrome/Edge cloud-reinjection repair.
---

# Bookmark Sync

Use `bookmark-sync` when installed. Otherwise locate the repository from `BOOKMARK_SYNC_ROOT` or the current workspace and run `./sync-bookmarks` there. Never embed a username or absolute home path in reusable commands.

## Safety

- Inspect with `--list` and preview an unfamiliar direction with `--mode preview`.
- Specify real Agent runs with explicit `--from` and `--to` values.
- Keep backups enabled. They contain complete bookmark data and must not be published.
- If a write reports an unfinished operation, use `--recover` after authorization to close the target browser. Never use `--discard-recovery` without explicit user approval after inspecting the target and backup.
- Use `--auto-close` only after the user authorizes closing affected browsers.
- Use `--allow-cloud-purge` only after explicit authorization to temporarily clear the selected Chrome/Edge target cloud bookmark tree.
- Use `--sync-strategy auto` unless the user explicitly requests another strategy.
- Treat Brave, Vivaldi, and Opera cloud-synced profiles as uncalibrated; direct mode is supported when bookmark cloud sync is disabled.
- Arc and Firefox are unsupported.

## Commands

```bash
bookmark-sync --list --json
bookmark-sync --list-backups --json
bookmark-sync --from chrome --to edge safari --mode preview --json
bookmark-sync --from chrome --to edge safari --auto-close
bookmark-sync --from chrome --to edge --auto-close --allow-cloud-purge
bookmark-sync --restore-backup /path/to/backup --restore-target edge --auto-close
bookmark-sync --recover --auto-close
bookmark-sync --doctor all --json
bookmark-sync --calibrate edge
```

Report backup paths, result counts, strategy, and verification after writes. Restore creates a rollback backup first. JSON stdout never includes bookmark titles or URLs; human diagnostics go to stderr.
