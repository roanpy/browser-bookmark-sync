# Test Matrix

This project separates deterministic format tests from browser-dependent manual validation. Browser cloud behavior can change independently of the local file format, so a passing unit suite is not a cloud-sync compatibility guarantee.

## Automated coverage

| Area | Coverage |
| --- | --- |
| Chromium JSON and Safari plist conversion | Portable tree mapping, folders, URLs, and root handling |
| Metadata preservation | Matching Chromium IDs/GUIDs and Safari UUIDs |
| Sync strategies | Direct, automatic selection, cloud-safe consent, and cloud-purge rollback |
| Recovery | Backups, newest-first discovery, restrictive permissions, single-writer lock, interrupted-operation recovery, restore verification, and rollback |
| Stabilization | Delayed drift detection, Edge repair, doctor, and calibrate state |
| CLI wrapper | Explicit directions, aliases, preview, restore, and safety flags |
| Installable CLI and JSON contract | Single-source version, wheel installation, console entry point, exit codes, redacted machine-readable summaries |
| JSON CLI integration | Real subprocess invocation, backup/doctor summaries, pure JSON stdout, and title/URL exclusion from machine-readable output |
| Build | Python compile, Ruff, AppleScript compile, ad-hoc macOS app build, and embedded-file match |
| Release workflow | YAML validation, tagged-version guard, source/wheel publishing, protected environment, and optional credential-gated signing/notarization path |

## Manual validation

| Browser set | Direction coverage | Result |
| --- | --- | --- |
| Chrome, Edge, Safari | Real local store detection and read-only listing | Validated on macOS |
| Edge cloud sync | Local reinjection diagnosis, cloud-safe purge, settle, restore, and post-open verification | Validated on the maintainer's macOS profile; do not treat as a guarantee for every Edge account state |
| Brave, Vivaldi, Opera | All six directed Chromium source/target pairs in isolated profiles | Validated with direct mode, browser reopen, backup, and byte-for-byte restore |
| Arc | Detection review | Not supported: sidebar archive is not the standard Chromium bookmark store |
| Firefox | Format review | Not supported: safe support requires a transactional `places.sqlite` handler |

## Boundaries

- Manual results are behavior-level validation, not a promise that future browser releases preserve the same private formats.
- Brave, Vivaldi, and Opera account cloud-sync behavior is not calibrated.
- Windows and Linux are outside the current support target.
- Reproduce failures with synthetic fixtures and include browser version, macOS version, direction, strategy, and cloud-sync state without including private bookmark data.
