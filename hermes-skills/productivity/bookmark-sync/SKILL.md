---
name: bookmark-sync
description: Detect and sync local Chrome, Edge, Safari, Brave, Vivaldi, and Opera bookmark stores with automatic backups.
version: 1.6.0
author: roanpy
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [Bookmarks, Browser, Chrome, Edge, Safari, Brave, Vivaldi, Opera, Sync, macOS]
---

# Bookmark Sync

After installation, use `bookmark-sync` from any directory. From a checkout, `./sync-bookmarks` remains available. Use `python3 bookmark_sync.py` for advanced doctor/calibrate options.

## Rules

- Inspect with `./sync-bookmarks --list` or `--mode preview` when direction is uncertain.
- Real syncs must specify source and targets explicitly.
- Keep automatic backups enabled unless the user explicitly requests a direct-only run; cloud purge cannot run without a backup.
- Pass `--auto-close` only when the user authorizes closing affected browsers.
- Pass `--allow-cloud-purge` only when the user explicitly authorizes Chrome/Edge cloud-safe remediation.
- If cloud purge fails, report the rollback result and pre-purge backup path.
- If a write reports an unfinished operation, recover it before any new write. Never discard recovery without explicit user approval.
- Brave, Vivaldi, and Opera are supported only with bookmark cloud sync disabled.
- Arc and Firefox are not supported.
- Report backup paths and verification results after writes.

## Commands

```bash
./sync-bookmarks --list
bookmark-sync --list --json
bookmark-sync --list-backups --json
./sync-bookmarks --from chrome --to edge safari --mode preview
./sync-bookmarks --from chrome --to edge safari --auto-close
./sync-bookmarks --from chrome --to edge --auto-close --allow-cloud-purge
./sync-bookmarks --restore-backup /path/to/backup --restore-target edge --auto-close
bookmark-sync --recover --auto-close
bookmark-sync --doctor all --json
python3 bookmark_sync.py --calibrate edge
bookmark-sync --from chrome --to edge safari --mode preview --json
```

## Verification

- Sync output must include a target backup, bookmark result count, and stabilization result.
- Restore output must include a rollback backup and result count.
- Backups default to `~/Downloads/bookmark-sync-backups`.
- Use `--list-backups` to select a restore candidate by target and creation time instead of guessing from filenames.
- JSON mode writes only the versioned result object to stdout; human diagnostics remain on stderr. The JSON object never includes bookmark titles or URLs.
