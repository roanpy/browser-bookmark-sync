# Changelog

## [0.1.1] - 2026-08-09

- Clarify the project's motivation and the cross-browser/cloud-reinjection problem on the homepage.
- Add an installable `bookmark-sync` CLI entry point for scripts, agents, and CI.
- Add `--json` output with exit codes, strategies, backups, results, and verification summaries on stdout; detailed logs remain on stderr.
- Add subprocess coverage for the real JSON CLI contract.
- Add tag-driven source/wheel publishing with optional Developer ID signing and Apple notarization for macOS releases.

## [0.1.0] - 2026-08-09

Initial public source release.

- Mirror Chrome, Edge, Safari, Brave, Vivaldi, and Opera bookmark stores on macOS.
- Add direct and cloud-safe Chrome/Edge strategies with explicit purge consent.
- Add target backups, atomic writes, stabilization verification, doctor/calibrate state, and verified restore with rollback.
- Add a portable tree that preserves matching browser node identifiers where possible.
- Add a standalone macOS app builder and reusable Codex/Hermes Skill guidance.
- Validate the Chromium handler in isolated Brave, Vivaldi, and Opera profiles across all six directed browser pairs.
