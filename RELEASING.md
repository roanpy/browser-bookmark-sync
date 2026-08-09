# Releasing

Bookmark Sync releases are tag-driven. The release workflow builds the wheel, signs the macOS app with a Developer ID certificate, submits it to Apple notarization, staples the ticket, generates checksums, and publishes a GitHub Release.

## Required GitHub Actions Secrets

Create a protected GitHub Environment named `release` and require reviewer approval. Configure these environment secrets before pushing a release tag:

- `MACOS_CERTIFICATE_BASE64`: base64-encoded Developer ID Application `.p12` certificate.
- `MACOS_CERTIFICATE_PASSWORD`: password for the `.p12` file.
- `APPLE_DEVELOPER_ID`: exact Developer ID Application signing identity.
- `APPLE_ID`: Apple ID used for notarization.
- `APPLE_TEAM_ID`: Apple Developer Team ID.
- `APPLE_APP_PASSWORD`: app-specific password for `notarytool`.

Do not commit the certificate, passwords, API tokens, or exported keychain. The workflow fails before building a release if any required secret is missing.

Protect `v*` tags so only maintainers can start the signing workflow. The workflow checks out the tagged source and receives signing credentials only after the protected environment is approved.

## Release Steps

1. Confirm `main` is clean and the version in `pyproject.toml`, README files, and `CHANGELOG.md` is aligned.
2. Run the local validation commands from `CONTRIBUTING.md`.
3. Create and push an annotated tag:

```bash
git switch main
git pull --ff-only
git tag -a v0.1.1 -m "Release v0.1.1"
git push origin v0.1.1
```

The `release.yml` workflow then publishes the wheel, notarized `Bookmark Sync.app` zip, and `SHA256SUMS` to the GitHub Release. If Apple credentials are not configured, the workflow fails safely rather than publishing an unsigned app.
