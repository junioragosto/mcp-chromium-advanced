# Bundled Runtime Placeholders

This directory is reserved for bundled non-Python runtimes that future release
artifacts may ship inside the application package.

Current layout contract:

- `resources/runtime/node/`
  Bundled Node.js runtime root
- `resources/runtime/official_playwright_mcp/`
  Bundled `@playwright/mcp` runtime root

Current product state:

- `official_playwright_mcp` is wired into engine selection as an experimental
  fourth backend
- it is intentionally fail-fast today
- it must not depend on a system-installed Node.js runtime when formally
  enabled in packaged releases
- the current official runtime ownership model is still incompatible with this
  project's live persistent-profile governance path

Do not place machine-local ad hoc binaries here without updating the release
packaging and validation flow.
