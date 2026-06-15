# Chromium Profile Manager Release

## What Is Included

- Desktop GUI application
- MCP daemon and browser worker runtime
- Built-in `resources/bookmarks_template.html`
- Built-in fingerprint plugin bundle downloaded from the latest `omegaee/my-fingerprint` release at build time
- Skill templates:
  - `skill_templates/browser-identity-mcp.SKILL.md`
  - `skill_templates/browser-identity-mcp-wsl.SKILL.md`
- Example config:
  - `chromium_profiles.example.json`

## What Is Not Included

- Chromium / Chrome browser binaries
- ChromeDriver binaries
- Real user profile data

You must configure your own Chromium/Chrome executable and matching ChromeDriver after startup.

## First Start

1. Start the GUI application.
2. Open the path/config section.
3. Set:
   - Chromium browser path
   - ChromeDriver path
   - split UserData profiles root
4. Save config.

## MCP Setup

The daemon endpoint is typically:

- `http://127.0.0.1:28888/mcp`

If `mcp.api_token` is set in the GUI/config, every MCP request must send:

- `Authorization: Bearer <token>`

## Token Configuration

- `mcp.api_token`
  Required for normal MCP/browser business calls
- `control.api_token`
  Required for GUI/control endpoints such as dashboard, logs, keepalive state, plugin CRUD, and worker control

If `control.api_token` is empty, `/_control/*` endpoints remain disabled.

## Browser / Driver Requirements

- Use your own local Chromium or Chrome installation
- Use a matching ChromeDriver
- Keep the browser major version aligned with the driver major version

## Profile Creation In A New Environment

New profile creation does not depend on mirror snapshots.

The creation path is:

1. Create a dedicated split UserData root such as `UserDataProfile1`
2. Create the Chromium profile directory inside it such as `Profile 1`
3. Seed bookmarks from the built-in `resources/bookmarks_template.html`
4. Let Chromium generate the rest of the profile state on first launch

So on a new machine, as long as:

- the split UserData root is configured
- Chromium path is configured
- ChromeDriver path is configured

the app can create fresh profiles without any preexisting mirror data.

## Engine Notes

- `playwright_cli`
  Default high-throughput MCP engine
- `selenium_uc`
  Best for stealth / anti-bot tolerance / gesture-heavy pages
- `patchright`
  Best for complex frontend diagnosis and structured extraction

## Skills

Copy the needed skill template into your Codex or project skill directory:

- `skill_templates/browser-identity-mcp.SKILL.md`
- `skill_templates/browser-identity-mcp-wsl.SKILL.md`

## Notes

- This release bundles the bookmark template and fingerprint plugin assets for convenience.
- It does not bundle real browsers or drivers because those remain machine-specific.
