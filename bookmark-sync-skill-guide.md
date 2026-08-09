# Bookmark Sync Skill Guide

Canonical automation guide for the repository. User-facing installation and safety information lives in `README.md`.

## Entry points

- Engine: `bookmark_sync.py`
- Short CLI: `sync-bookmarks`
- macOS app source: `bookmark-sync-app.applescript`
- App builder: `build-macos-app`
- State: `~/Library/Application Support/Bookmark Sync/state.json`
- Backups: `~/Downloads/bookmark-sync-backups`

## Supported browsers

- Chrome, Edge, Safari: direct sync.
- Chrome and Edge: cloud reinjection detection and cloud-safe remediation.
- Brave, Vivaldi, Opera: tested direct sync when bookmark cloud sync is disabled.
- Arc and Firefox: unsupported because they require separate format handlers.

## Agent rules

- Inspect with `./sync-bookmarks --list` or `--mode preview` before uncertain writes.
- Real runs should pass an explicit source and targets.
- Backups are enabled by default and cover only targets in the current run.
- Do not pass `--no-backup` unless the user explicitly requests a direct-only run; cloud purge always requires a backup.
- Browsers must be closed. Pass `--auto-close` only with user authorization.
- Cloud-safe temporarily clears target cloud bookmarks. Pass `--allow-cloud-purge` only after explicit user authorization.
- If cloud purge fails, report whether the pre-purge local backup was restored and include its path.
- Prefer `--mode strict` for real syncs.
- Use `--restore-backup` with `--restore-target` to restore; restoration creates a rollback backup first.
- Brave, Vivaldi, and Opera account-synced profiles are not calibrated and should not be targeted as cloud-synced stores.

## Commands

```bash
# Inspect
./sync-bookmarks --list

# Preview
./sync-bookmarks --from chrome --to edge safari --mode preview

# Strict direct/auto sync with authorized browser closing
./sync-bookmarks --from chrome --to edge safari --auto-close

# Allow known Chrome/Edge cloud remediation
./sync-bookmarks --from chrome --to edge --auto-close --allow-cloud-purge

# Restore a target from backup
./sync-bookmarks --restore-backup /path/to/backup --restore-target edge --auto-close

# Health and calibration
python3 bookmark_sync.py --doctor all
python3 bookmark_sync.py --calibrate edge
```

## Verification

- A real sync must report a backup path, result count, and stabilization result.
- A restore must report a rollback backup and restored result count.
- Run `python3 -m unittest discover -s tests` after code changes.
