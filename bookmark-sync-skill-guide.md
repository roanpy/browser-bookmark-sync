# Bookmark Sync Skill Guide

Canonical automation guide for the repository. User-facing installation and safety information lives in `README.md`.

## Entry points

- Engine: `bookmark_sync.py`
- Short CLI: `sync-bookmarks`
- Installed CLI: `bookmark-sync`
- macOS app source: `bookmark-sync-app.applescript`
- App builder: `build-macos-app`
- State: `~/Library/Application Support/Bookmark Sync/state.json`
- Backups: `~/Downloads/bookmark-sync-backups`
- Installable Codex Skill: `skills/bookmark-sync/SKILL.md`

## Supported browsers

- Chrome, Edge, Safari: direct sync.
- Chrome and Edge: automatic post-open reinjection detection and closed-browser repair; cloud-safe purge is explicit and destructive.
- Brave, Vivaldi, Opera: tested direct sync when bookmark cloud sync is disabled.
- Arc and Firefox: unsupported because they require separate format handlers.

## Agent rules

- Inspect with `./sync-bookmarks --list` or `--mode preview` before uncertain writes.
- Real runs should pass an explicit source and targets.
- Use full store IDs from `--list --json` for non-default or multiple browser profiles.
- Backups are enabled by default and cover only targets in the current run.
- Do not pass `--no-backup` unless the user explicitly requests a direct-only run; cloud purge always requires a backup.
- Browsers must be closed. Pass `--auto-close` only with user authorization.
- `auto` uses backed-up direct mirroring, then reopens sync-enabled Chromium targets and repairs detected drift with the browser closed.
- Cloud-safe temporarily clears target bookmarks. Use `--sync-strategy cloud-safe --allow-cloud-purge` only after explicit user authorization.
- If cloud purge fails, report whether the pre-purge local backup was restored and include its path.
- Resolve unfinished operations with `--recover` before new writes; use `--discard-recovery` only with explicit user approval after inspection.
- Prefer `--mode strict` for real syncs.
- Use `--restore-backup` with `--restore-target` to restore; restoration creates a rollback backup first.
- Brave, Vivaldi, and Opera account-synced profiles are not calibrated and should not be targeted as cloud-synced stores.

## Commands

```bash
# Inspect
./sync-bookmarks --list
bookmark-sync --list --json
bookmark-sync --list-backups --json

# Preview
./sync-bookmarks --from chrome --to edge safari --mode preview

# Strict direct/auto sync with authorized browser closing
./sync-bookmarks --from chrome --to edge safari --auto-close

# Explicitly allow a destructive Chrome/Edge cloud purge
./sync-bookmarks --from chrome --to edge --sync-strategy cloud-safe --auto-close --allow-cloud-purge

# Restore a target from backup
./sync-bookmarks --restore-backup /path/to/backup --restore-target edge --auto-close
bookmark-sync --recover --auto-close

# Health and calibration
bookmark-sync --doctor all --json
bookmark-sync --calibrate edge

# JSON result for automation
bookmark-sync --from chrome --to edge safari --mode preview --json
bookmark-sync --from "chrome:Profile 1" --to "edge:Work" --mode preview --json
```

After installation with `python3 -m pip install --user .` or `pipx install .`, use `bookmark-sync` from any directory. Add `--json` when another Agent, script, or CI job needs a machine-readable result; human diagnostics are written to stderr.

Install the Codex Skill from a checkout with:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills/bookmark-sync"
cp skills/bookmark-sync/SKILL.md "${CODEX_HOME:-$HOME/.codex}/skills/bookmark-sync/SKILL.md"
```

## Verification

- A real sync must report a backup path, result count, and stabilization result.
- A restore must report a rollback backup and restored result count.
- Run `python3 -m unittest discover -s tests` after code changes.
