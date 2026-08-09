<div align="center">
  <h1>Bookmark Sync</h1>
  <p><strong>Safe, local-first bookmark mirroring across major macOS browsers.</strong></p>

  English · [简体中文](README.zh-CN.md)

  [![Tests](https://github.com/roanpy/browser-bookmark-sync/actions/workflows/test.yml/badge.svg)](https://github.com/roanpy/browser-bookmark-sync/actions/workflows/test.yml)
  [![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](#requirements)
  [![Platform macOS](https://img.shields.io/badge/platform-macOS-lightgrey.svg)](#requirements)
  [![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
</div>

Bookmark Sync is a command-line tool and small macOS app for explicitly mirroring bookmarks between Chrome, Edge, Safari, Brave, Vivaldi, and Opera. Bookmark data stays on the machine; the project has no account system or bookmark server.

## Why This Project Exists

I work across multiple browsers and different environments, but browser bookmarks are usually only synchronized reliably inside one vendor's ecosystem. Keeping the same bookmark tree across Chrome, Edge, Safari, and other browsers becomes especially difficult when native cloud sync is enabled: a local correction may appear to succeed, then stale cloud entries can be re-injected when the browser opens again.

I looked for existing open-source tools before writing this project. They solved important parts of cross-browser synchronization, but I did not find one that combined explicit source-to-target mirroring, local backups and rollback, Safari support, and a practical repair path for cloud reinjection. Bookmark Sync grew out of that gap.

> **The core problem:** multiple browsers are useful in real life, but their bookmark stores and cloud-sync behavior are not interoperable enough to keep one trusted tree aligned safely.

This project does not try to replace native browser cloud sync or become another hosted bookmark service. It provides a local-first bridge for migration, controlled mirroring, backup, verification, and browser-specific repair.

> [!WARNING]
> This is a mirror tool, not a merge service. A real sync replaces the mapped bookmark trees in each selected target. Preview the direction first and keep the automatic backups.

## The problems it solves

- **Edge cloud reinjection:** a local bookmark-file replacement can appear correct, then Edge Sync restores stale cloud entries after the browser opens. The `auto` strategy detects this drift, records it per target, and switches affected Chrome/Edge profiles to a browser-API purge, cloud-settle, and restore workflow.
- **Different browser formats:** Chromium browsers use JSON bookmark trees while Safari uses a property list with different roots and metadata. A portable tree maps bookmark bar, menu, folders, URLs, and supported synced roots without copying one browser's raw file into another.
- **Identity churn:** unchanged Chromium IDs/GUIDs and Safari UUIDs are reused when nodes match, reducing needless delete/recreate behavior and cloud-sync noise.
- **Unsafe one-shot scripts:** every target is backed up by default, writes are atomic, results are reloaded and compared, and restore creates its own rollback backup.
- **Browser timing differences:** stabilization observations, `doctor`, and `calibrate` provide browser-specific verification instead of assuming one fixed delay works everywhere.

## Key advantages

| Capability | What it provides |
| --- | --- |
| Explicit direction | Choose any detected supported browser as source and one or more targets. |
| Two sync strategies | Fast direct writes for normal/local profiles; cloud-safe remediation only for confirmed Chrome/Edge reinjection cases. |
| Data-loss guards | Target backup, `0600` permissions, atomic replacement, post-write verification, and verified restore with rollback. |
| Local-first privacy | No telemetry, API key, hosted service, or bookmark upload. Runtime state stores hashes and timing observations, not URLs. |
| Agent-friendly interface | Installable deterministic CLI with stable JSON output plus optional Codex/Hermes Skill instructions. The Skill is an adapter; the CLI remains the product core. |
| Extensible registry | Browser detection and format handlers are registered separately, so another Chromium browser can reuse the existing handler. |

## Browser support

| Browser | Direct sync | Cloud-safe remediation | Validation status |
| --- | --- | --- | --- |
| Chrome | Yes | Yes | Real local profile and isolated logic tests |
| Edge | Yes | Yes | Real local/cloud calibration and reinjection remediation |
| Safari | Yes | Direct verification only | Real local profile |
| Brave | Yes | Not calibrated | Isolated profile, all Chromium cross-directions |
| Vivaldi | Yes | Not calibrated | Isolated profile, all Chromium cross-directions |
| Opera | Yes | Not calibrated | Isolated profile, all Chromium cross-directions |
| Arc | No | No | Uses its own sidebar archive rather than the standard Chromium bookmark store |
| Firefox | No | No | Requires a transactional `places.sqlite` implementation |

Use Brave, Vivaldi, and Opera direct mode with their bookmark cloud sync disabled until their cloud behavior is calibrated.

## Quick start

### Requirements

- macOS
- Python 3.11 or newer
- Full Disk Access for the terminal or app if macOS denies access to Safari bookmarks

```bash
git clone https://github.com/roanpy/browser-bookmark-sync.git
cd browser-bookmark-sync
./sync-bookmarks --list
```

Install the callable CLI without keeping a repository checkout:

```bash
python3 -m pip install --user .
bookmark-sync --list
```

For isolated user installs, use `pipx install .` instead. The package has no runtime dependencies and remains macOS-only.

Preview Chrome to Edge and Safari without writing:

```bash
./sync-bookmarks --from chrome --to edge safari --mode preview
```

Agent and CI integrations can consume JSON on stdout; detailed human diagnostics stay on stderr:

```bash
bookmark-sync --from chrome --to edge safari --mode preview --json
```

The JSON schema is versioned and reports the operation, exit code, selected stores, strategies, backup paths, result counts, and verification summaries. It never includes bookmark titles or URLs.

Run a strict sync. Close affected browsers first, or explicitly let the tool close them:

```bash
./sync-bookmarks --from chrome --to edge safari --auto-close
```

If `auto` selects cloud-safe remediation, explicitly authorize the temporary target-cloud purge:

```bash
./sync-bookmarks --from chrome --to edge --auto-close --allow-cloud-purge
```

Restore a backup; the current target is backed up again before restoration:

```bash
./sync-bookmarks --restore-backup ~/Downloads/bookmark-sync-backups/BACKUP_FILE \
  --restore-target edge --auto-close
```

Backups default to `~/Downloads/bookmark-sync-backups`. Runtime state defaults to `~/Library/Application Support/Bookmark Sync/state.json`. Override them with `BOOKMARK_SYNC_BACKUP_DIR` and `BOOKMARK_SYNC_DATA_DIR`; `BOOKMARK_SYNC_HOME` is available for isolated tests.

## macOS app

Build an ad-hoc signed local app:

```bash
./build-macos-app
open "dist/Bookmark Sync.app"
```

The app confirms source, targets, browser closing, backups, and possible cloud purge before running. Public binary distribution still requires a Developer ID signature and Apple notarization; this repository publishes source, not a notarized binary.

The tag-driven signed release workflow is documented in [RELEASING.md](RELEASING.md). It uses GitHub Actions secrets for Apple credentials and never stores certificates, passwords, or tokens in the repository.

## Safety and privacy

- Cloud purge always requires `--allow-cloud-purge` and cannot be combined with `--no-backup`.
- If cloud purge fails, the tool closes the target browser and makes a best-effort local rollback from the pre-purge backup.
- Source bookmarks are reloaded after affected browsers close, avoiding a stale pre-shutdown snapshot.
- Atomic-write temporary files are removed even when replacement fails.
- Backups contain complete bookmark data. Do not publish them or attach them to issues.
- Runtime state contains store IDs, SHA-256 signatures, timing observations, and strategy history, but no bookmark titles or URLs.

The temporary Chrome/Edge extension uses the documented Chromium `bookmarks` API and exists only for the cloud-safe run. See the official [Chrome bookmarks API](https://developer.chrome.com/docs/extensions/reference/api/bookmarks) and [Microsoft Edge extension API support](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/api-support).

## Validation

```bash
python3 -m unittest discover -s tests
python3 -m py_compile bookmark_sync.py sync_bookmarks.py sync-bookmarks
python3 -m pip wheel --no-deps . --wheel-dir /tmp/bookmark-sync-wheel
ruff check .
```

The automated suite covers conversion, stable metadata reuse, strategy selection, cloud-purge consent and rollback, backup/restore, stabilization, wrapper commands, and the real JSON CLI subprocess contract. Brave, Vivaldi, and Opera were also manually tested in isolated profiles in all six source/target directions, including browser reopen and byte-for-byte backup restoration.

See [CHANGELOG.md](CHANGELOG.md) for the version history. The source tree is prepared for `v0.1.1`; the latest published release remains `v0.1.0` until the signed tag workflow completes.

Contributors should read [CONTRIBUTING.md](CONTRIBUTING.md) and [TEST_MATRIX.md](TEST_MATRIX.md) before submitting browser or recovery changes.

## Scope

- Bookmark mirroring only; history, passwords, tabs, extensions, and browser settings are never modified.
- This tool does not merge concurrent edits or replace native browser cloud sync.
- Safari's private on-disk format is handled conservatively and may require updates after macOS changes.
- Chrome/Edge cloud-safe mode is intentionally destructive to the selected target bookmark tree before restoring the chosen source; preview and backup are the safety boundary.

Security reports should follow [SECURITY.md](SECURITY.md). MIT licensed. Browser names and trademarks belong to their respective owners; this project is not affiliated with them.
