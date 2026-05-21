# MCP Chromium Advanced

MCP Chromium Advanced is a desktop GUI and MCP service for managing real Chromium browser profiles. It is intended for workflows that need an existing logged-in browser identity rather than a fresh automation-only browser.

[中文文档](./README_zh.md)

## Overview

This project is best understood as a real Chromium identity manager plus an MCP browser service. Instead of creating a fresh disposable automation browser for every task, it is designed to let AI workflows safely reuse existing logged-in browser profiles.

From a first-contact perspective, there are six key ideas:

1. It solves the "real login state" problem.
   The project lets GUI-managed Chromium profiles be exposed to MCP clients so automation can reuse cookies, local storage, extensions, bookmarks, and site permissions.
2. It is organized into three layers.
   The GUI manages configuration and profiles, the daemon provides a stable MCP endpoint, and the worker starts on demand to control a real browser session.
3. It supports multiple browser execution engines.
   Shared profile and session ownership stay the same, while the execution backend can use either Selenium plus `undetected_chromedriver` or Patchright.
4. It is designed around safe profile ownership.
   Session checks prevent multiple tasks, threads, or keepalive jobs from silently fighting over the same logged-in browser identity.
5. It attaches automation to real Chromium profiles.
   The browser is launched with the actual `user-data-dir` and `profile-directory`, then the selected execution engine connects to that persistent profile.
6. It includes keepalive workflows in addition to MCP control.
   The GUI can run scheduled or manual keepalive tasks against real logged-in profiles for sites such as ChatGPT, Gmail, and Google.

The public user entry point is:

```bash
python run_gui.py
```

## Screenshot

![Application Screenshot](docs/imgs/ScreenShot.png)

## How it works

The browser automation layer supports:

- Selenium: [https://www.selenium.dev/](https://www.selenium.dev/)
- undetected-chromedriver: [https://github.com/ultrafunkamsterdam/undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)
- Patchright: [https://github.com/Kaliiiiiiiiii-Vinyzu/patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)

The project starts Chromium with a real `user-data-dir` and `profile-directory`, then attaches the selected browser engine to that profile. This allows the worker to reuse real cookies, sessions, local storage, extensions, and other persistent browser state.

If you use a fingerprint plugin, the project can also load `my-fingerprint`:

- my-fingerprint releases: [https://github.com/omegaee/my-fingerprint/releases](https://github.com/omegaee/my-fingerprint/releases)

On top of that browser layer, the MCP service adds:

- profile/session occupancy checks
- session start and release APIs
- a stable daemon endpoint with a lazy-start worker
- GUI-based lifecycle control and logs

## Main capabilities

- Manage multiple Chromium profiles from one GUI
- Expose real browser identities to MCP clients
- Prevent conflicting sessions across threads or tasks
- Switch the default browser engine in the GUI configuration
- Start the browser worker only when needed
- Release resources automatically after idle timeout
- Run keepalive jobs against real logged-in profiles
- Coordinate multi-tab browser work with explicit tab listing, opening, activation, and closing tools
- Collect structured console, page error, and network diagnostics instead of relying on screenshots alone

## Requirements

- Python 3.10+
- A local Chromium-compatible browser
- A matching ChromeDriver build
- A desktop environment

Install dependencies:

```bash
pip install -r requirements.txt
```

## Browser and driver setup

You need three local resources before using the tool:

1. A Chromium or Chrome binary
2. A matching `chromedriver`
3. A persistent user data directory

Common choices:

- ungoogled-chromium
- Chromium
- Google Chrome

The ChromeDriver version should match the browser major version as closely as possible.

### Strong recommendation

This project strongly recommends `ungoogled-chromium`.

- It is more stable for long-lived local automation setups
- It does not auto-update aggressively, which helps avoid unexpected ChromeDriver breakage
- Versions below `136` are recommended because newer releases may have compatibility issues with `ungoogled-chromium` in some environments

Official download pages:

- ungoogled-chromium binaries: [https://ungoogled-software.github.io/ungoogled-chromium-binaries/](https://ungoogled-software.github.io/ungoogled-chromium-binaries/)
- Chrome for Testing / ChromeDriver: [https://googlechromelabs.github.io/chrome-for-testing/](https://googlechromelabs.github.io/chrome-for-testing/)

Driver matching rule:

- Always match the Chromium major version with the ChromeDriver major version
- If the default download page does not show the exact build you need, adjust the version number in the driver download URL until it matches your installed Chromium version
- Before wiring paths into the GUI, verify the browser version and driver version are aligned

## Configuration

At first launch, the app creates a config file in the platform config directory:

- Windows: `%APPDATA%/ChromiumProfileManager/workstates/chromium_profiles.json`
- macOS: `~/Library/Application Support/ChromiumProfileManager/workstates/chromium_profiles.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/ChromiumProfileManager/workstates/chromium_profiles.json`

A sanitized template is included in the repository:

- `chromium_profiles.example.json`
- `resources/bookmarks_template.html`

On first run, the app copies the bundled bookmark template into the local workspace default path if no template exists yet.

Important fields:

- `paths.chromium_dir`
  Path to the browser executable, or a directory containing it
- `paths.chromedriver_path`
  Path to `chromedriver`, or a directory containing it
- `paths.user_data_root`
  Root directory that stores all persistent browser profiles
- `paths.bookmarks_template_path`
  Optional bookmark template used when initializing profiles
- `paths.fingerprint_zip_path`
  Optional path related to `my-fingerprint`
- `app.language`
  UI language code such as `en`, `ja`, or `zh`
- `app.browser_engine`
  Default browser execution backend, currently `selenium_uc` or `patchright`
- `launch.*`
  Browser launch defaults used by the built-in Python launcher, such as `new_window`, `start_maximized`, `load_fingerprint_extension`, `check_url`, and `extra_args`
- `mcp.host`, `mcp.port`, `mcp.worker_port`, `mcp.path`
  Network settings for the daemon and worker

## MCP service

When enabled in the GUI, the daemon exposes a stable HTTP endpoint such as:

```text
http://127.0.0.1:28888/mcp
```

The daemon stays available between tasks. The browser worker is started only when a request needs it, and it is reclaimed after the configured idle timeout.

Typical MCP flow:

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. perform browser actions
7. `close_profile_session(session_id)`

Engine-aware callers may also pass an explicit engine when starting a session. If omitted, the configured GUI default engine is used.

### Multi-tab tools

The worker now exposes formal multi-tab operations so agents do not have to rely on hidden browser focus changes:

- `browser_list_tabs`
- `browser_open_tab`
- `browser_activate_tab`
- `browser_close_tab`

The practical workflow is:

1. open or discover the tab
2. activate the target tab explicitly
3. perform page actions on that active tab
4. switch again when needed

For tab-aware read and debug calls, tools such as `navigate`, `get_current_url`, `get_page_text`, `get_page_html`, `browser_snapshot`, `browser_list_candidates`, `inspect_elements`, `run_script`, and `screenshot` also accept an optional `tab_id`.

### Debug and observability tools

The worker also exposes structured debugging helpers that are meant to replace manual F12 screenshots in many cases:

- `browser_get_console_messages`
- `browser_get_page_errors`
- `browser_get_network_requests`
- `browser_clear_debug_buffers`
- `browser_diagnose_page`

`browser_diagnose_page` is the highest-signal first stop when an agent gets blocked. It bundles the current interaction context together with recent console errors, page exceptions, failed requests, and recent bad HTTP responses.

## Engine Notes

### Shared behavior

- Profile creation, deletion, syncing, bookmarks, and session ownership are shared across all engines
- GUI and MCP session flow stay the same regardless of engine
- Real `user-data-dir` plus `profile-directory` remain the source of truth

### Selenium plus undetected-chromedriver

- Currently the most mature path in the project
- Also powers the existing keepalive workflows
- Uses the shared `launch.*` defaults for direct profile launch

### Patchright

- Already supports real persistent profile sessions through the MCP/session layer
- Uses a smaller validated startup argument set than Selenium for compatibility
- Intended for sites where a Playwright-compatible execution model is more reliable
- Provides the strongest tab model and the richest structured debug telemetry in the current project
- Collects DevTools-style diagnostics through per-tab CDP sessions, so agents can read console output, uncaught exceptions, and network failures without opening browser DevTools manually
- Keepalive is not routed through Patchright yet in this stage

### Selenium plus undetected-chromedriver debug notes

- Selenium sessions now expose the same high-level tab and debug tools where Chromium logging supports them
- Console and network diagnostics are gathered from browser and performance logs, so they are best-effort compared with Patchright
- Structured accessibility snapshots and snapshot-ref targeting still remain Patchright-only

## Cross-platform notes

This is a Python project and the source code is being kept platform-aware.

- Windows is the primary tested platform
- macOS and Linux are supported at source level when valid browser and driver paths are provided
- Windows packaging is currently the most complete desktop packaging path

## Skill templates

The repository includes reusable agent skill templates in:

- `docs/skill_templates/`

These files are examples for Codex or other AI workflows that need to consume this MCP service consistently. They are templates, not an auto-loaded runtime directory.

Typical usage:

1. Copy the appropriate `.SKILL.md` file into your global or project-specific skill directory
2. Adjust host, port, and profile naming rules if your environment differs
3. In other AI tasks, instruct the agent to use that skill when interacting with this MCP service

## Privacy and security

- Do not commit your real `chromium_profiles.json`
- Do not commit real profile data, cookies, session state, or personal account labels
- Do not expose the MCP endpoint to untrusted networks
- Agents should never guess a real profile identity; they should ask or use an explicit `profile_name`

Bookmark templates themselves are not treated as sensitive by default and can be kept in the repository if they are generic.

## Repository structure

- `run_gui.py`
  Public entry point
- `chromium_advanced/chromium_manage_gui.py`
  Desktop GUI
- `chromium_advanced/mcp_daemon.py`
  Stable daemon service
- `chromium_advanced/mcp_server.py`
  Browser worker implementation
- `docs/ARCHITECTURE_GUIDE.md`
  Additional implementation notes
- `docs/skill_templates/`
  Reusable agent skill templates for Codex or other AI workflows, including Windows and WSL examples
- `resources/bookmarks_template.html`
  Bookmark template bundled with the project

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).
