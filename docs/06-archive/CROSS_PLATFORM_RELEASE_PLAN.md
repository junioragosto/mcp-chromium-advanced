# Cross-Platform Release Plan

## Target

Ship a first formal release baseline as `0.1.0` that can be built on GitHub Actions for:

- Windows
- macOS
- Linux

This release line focuses on the application/runtime layer, not bundled browser assets.

## Scope for 0.1.0

Included:

- GUI
- MCP daemon
- MCP worker
- Python package metadata
- Node setup inside workflow
- `playwright-cli` installation inside workflow
- release artifact packaging
- versioned release metadata

Deferred:

- bundling Chromium binaries
- bundling ChromeDriver
- automatic per-platform browser asset resolution
- a fully self-contained browser runtime

## Release Contract

Each release artifact must:

- carry version `0.1.0`
- be buildable from GitHub Actions
- produce a downloadable artifact on each target OS
- document that Chromium and ChromeDriver must be configured locally after startup

## Packaging Strategy

### Windows

- reuse the existing PyInstaller pipeline
- package the produced `dist/` output into a zip artifact

### macOS

- produce a portable source/runtime bundle
- include project code, resources, and entrypoint files
- do not bundle browser assets yet

### Linux

- produce a portable source/runtime bundle as `tar.gz`
- include project code, resources, and entrypoint files
- do not bundle browser assets yet

## Build Entry Points

- local:
  `python scripts/build_release.py --artifact-name <artifact-name>`
- CI:
  `.github/workflows/release-candidate.yml`
  `.github/workflows/release-publish.yml`

## Future Upgrade Path

After `0.1.0`, the next release track should add:

1. browser runtime manifest
2. Chromium/ChromeDriver asset resolution
3. per-platform asset caching
4. fully self-contained release bundles
