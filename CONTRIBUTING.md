# Contributing

## Before opening a change

- Keep the scope focused on local bookmark mirroring and recovery.
- Use synthetic bookmark titles and URLs in tests and screenshots.
- Never commit browser profile files, backups, runtime state, local paths, account names, or real bookmark data.
- Do not run cloud-purge or browser-closing flows in CI or an unattended pull request.

## Local checks

Run the same checks used by CI from macOS:

```bash
python3 -m unittest discover -s tests
python3 -m py_compile bookmark_sync.py sync-bookmarks
ruff check .
./build-macos-app
```

Changes to browser format handlers should include a fixture-based test. Changes to sync strategy or recovery should cover consent, backup, verification, and rollback behavior. Manual browser tests must use isolated profiles unless the test explicitly documents a reversible real-profile operation.

## Pull requests

Describe the affected browser direction, sync strategy, cloud-sync state, backup behavior, and validation performed. Remove private paths, URLs, logs, and screenshots before attaching them. The project mirrors selected bookmark trees; it does not merge concurrent edits or upload bookmark data.
