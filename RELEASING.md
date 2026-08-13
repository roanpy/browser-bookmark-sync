# Releasing

Bookmark Sync releases are tag-driven. Without Apple credentials, the release workflow publishes the source archive and Python wheel. With a complete Apple credential set, it additionally signs the macOS app with a Developer ID certificate, submits it to Apple notarization, staples the ticket, generates checksums, and publishes the app zip.

## Signed App Credentials

To publish only source and wheel assets, no Apple credentials are required. To attach a signed and notarized macOS app, create a protected GitHub Environment named `release`, restrict it to `v*` tags, and configure these environment secrets before pushing a release tag. A sole maintainer does not need a self-approval rule; teams should add independent reviewers.

- `MACOS_CERTIFICATE_BASE64`: base64-encoded Developer ID Application `.p12` certificate.
- `MACOS_CERTIFICATE_PASSWORD`: password for the `.p12` file.
- `APPLE_DEVELOPER_ID`: exact Developer ID Application signing identity.
- `APPLE_ID`: Apple ID used for notarization.
- `APPLE_TEAM_ID`: Apple Developer Team ID.
- `APPLE_APP_PASSWORD`: app-specific password for `notarytool`.

Do not commit the certificate, passwords, API tokens, or exported keychain. The workflow fails if only part of the Apple credential set is configured; it otherwise falls back to the safe source-and-wheel release mode.

Protect `v*` tags from deletion and non-fast-forward updates so published releases cannot be rewritten. The workflow checks out the tagged source without persisting GitHub credentials and receives signing credentials only inside the restricted `release` environment.
It also rejects tags whose commit is not part of `main` history.

## Release Steps

1. Confirm `main` is clean and `bookmark_sync_version.py`, README files, and `CHANGELOG.md` are aligned.
2. Run the local validation commands from `CONTRIBUTING.md`.
3. Create and push an annotated tag:

```bash
git switch main
git pull --ff-only
git tag -a v0.2.1 -m "Release v0.2.1"
git push origin v0.2.1
```

The `release.yml` workflow then publishes the source archive, wheel, and `SHA256SUMS`. If the complete Apple credential set is configured, it also publishes a notarized `Bookmark Sync.app` zip; it never publishes an unsigned app.
