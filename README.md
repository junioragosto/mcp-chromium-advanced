# MCP Chromium Advanced

MCP Chromium Advanced is a desktop GUI and MCP service for managing real Chromium browser profiles. It is intended for workflows that need an existing logged-in browser identity rather than a fresh automation-only browser.

[中文文档](./README_zh.md)

## Overview

This project is best understood as a real Chromium identity manager plus an MCP browser service. Instead of creating a fresh disposable automation browser for every task, it is designed to let AI workflows safely reuse existing logged-in browser profiles.

From a first-contact perspective, there are seven key ideas:

1. It solves the "real login state" problem.
   The project lets GUI-managed Chromium profiles be exposed to MCP clients so automation can reuse cookies, local storage, extensions, bookmarks, and site permissions.
2. It is organized into layered runtime control.
   The GUI manages configuration and profiles, the daemon provides a stable MCP endpoint, the worker starts on demand, and a managed browser session kernel normalizes runtime behavior before MCP tools use it.
3. It supports multiple browser execution engines.
   Shared profile and session ownership stay the same, while the execution backend can use Selenium plus `undetected_chromedriver`, Patchright, or `playwright_cli`.
4. It exposes a more stable runtime contract than the raw engines alone.
   The managed session kernel adds structured capability metadata, normalized action errors, and generic DOM-script fallbacks so callers are less exposed to engine-specific gaps.
5. It is designed around safe profile ownership.
   Session checks prevent live-root tasks, threads, or keepalive jobs from silently fighting over the same logged-in browser identity, while mirror-isolated runtime clones provide a controlled parallel path when enabled.
6. It attaches automation to real Chromium profiles.
   The browser is launched with the actual `user-data-dir` and `profile-directory`, then the selected execution engine connects to that persistent profile.
7. It includes keepalive workflows in addition to MCP control.
   The GUI can run scheduled or manual keepalive tasks against real logged-in profiles for sites such as ChatGPT, Gmail, and Google.

Important account boundary: a Chromium `Profile N` is a browser data container, not a universal website account. The GUI `Account` field is an operator-maintained label or note and should be treated as a hint only. Each website still has its own login state inside that browser profile, so account-sensitive automation must verify the actual logged-in account on the target site before continuing.

Important extraction boundary: this project deliberately stays generic and open. It does not ship site-specific DOM adapters for Gmail, YouTube Studio, GitHub, or other targets. The managed runtime now does safer script serialization plus generic DOM/text fallbacks, but complex dynamic applications can still yield weaker structured extraction under `playwright_cli` than under `patchright`. When the task depends on high-fidelity structured reads from a difficult frontend, prefer `patchright`.

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
- Playwright CLI: [https://github.com/microsoft/playwright-cli](https://github.com/microsoft/playwright-cli)

The project starts Chromium with a real `user-data-dir` and `profile-directory`, then attaches the selected browser engine to that profile. This allows the worker to reuse real cookies, sessions, local storage, extensions, and other persistent browser state.

If you use a fingerprint plugin, the project can also load `my-fingerprint`:

- my-fingerprint releases: [https://github.com/omegaee/my-fingerprint/releases](https://github.com/omegaee/my-fingerprint/releases)

On top of that browser layer, the MCP service adds:

- profile/session occupancy checks
- shared profile occupancy registry and event stream
- session start and release APIs
- profile reclaim and recovery primitives for stale locks or expired script leases
- a stable daemon endpoint with a policy-controlled worker (`lazy`, `sticky`, or `always_on`)
- GUI-based lifecycle control and logs

## Runtime control and security

The daemon now separates business access from management access.

- `api_token`
  Used by normal MCP clients and daemon automation callers for browser work.
- `admin_token`
  Required for management endpoints such as worker lifecycle control and force reclaim.

Security boundary:

- `admin_token` is intentionally independent from `api_token`.
- If `admin_token` is absent, management endpoints stay disabled instead of silently accepting the business token.
- GUI and daemon bootstrap now generate distinct values for both tokens when persistence needs to seed them.

Current endpoint boundary:

- Business surface:
  - `/mcp`
  - `/_daemon/status`
  - `/_daemon/profiles`
  - `/_daemon/profiles/{profile_name}`
  - `/_daemon/automation/*`
- Admin-only surface:
  - `/_daemon/worker/start`
  - `/_daemon/worker/stop`
  - `/_daemon/profiles/{profile_name}/reclaim`
  - `/_daemon/reap-expired`

The worker runtime policy is configurable:

- `lazy`
  Reclaims the worker soon after idle timeout.
- `sticky`
  Default. Keeps the worker alive longer for real business traffic and reduces frequent restart churn.
- `always_on`
  Never reclaims the worker due to idle timeout.

## Main capabilities

- Manage multiple Chromium profiles from one GUI
- Expose real browser identities to MCP clients
- Prevent conflicting sessions across threads or tasks
- Switch the default browser engine in the GUI configuration
- Expose structured runtime capabilities instead of only raw engine names
- Normalize action failures into stable error codes for callers
- Start the browser worker only when needed
- Release resources automatically after idle timeout
- Run keepalive jobs against real logged-in profiles
- Coordinate multi-tab browser work with explicit tab listing, opening, activation, and closing tools
- Support formal coordinate and gesture interactions on capable engines via `browser_mouse_move_xy`, `browser_mouse_click_xy`, `browser_mouse_drag_xy`, and `browser_mouse_gesture_path`
- Collect structured console, page error, and network diagnostics instead of relying on screenshots alone
- Fall back to generic DOM-based snapshot, candidate enumeration, wait, and target diagnostics when a runtime lacks native support

## Engine selection

The project now treats browser engines as execution strategies under one shared profile/session governance model.

There are two ways to choose an engine:

- GUI default engine
  Stored in `app.browser_engine`. This is the fallback engine used when MCP callers do not pass an explicit engine.
- Per-request explicit engine
  MCP callers may pass `engine` to `can_start_profile_session(...)` and `start_profile_session(...)`.

Recommended practical policy:

- `playwright_cli`
  Default choice for normal MCP work. Fast startup, lower interaction overhead, and the best fit for the new per-profile live concurrency model.
- `selenium_uc`
  Preferred for stealth-sensitive sites or workflows where avoiding automation detection matters more than raw throughput.
- `patchright`
  Preferred for complex frontend diagnosis, richer structured debugging, and the strongest snapshot/tab-aware inspection behavior.

Important switching rule:

- Changing the GUI default engine affects only future sessions.
- Existing sessions keep the engine they were started with.
- `reuse_existing=true` only reuses a compatible session for the same profile and the same engine.
- Starting a new session with a different engine never hot-switches an existing session in place. Existing sessions keep their original engine.

## Managed automation scripts

The project now has a second formal consumer path besides MCP: managed local scripts.

The intended model is:

```text
fixed script -> AutomationRunner -> SessionManager -> BrowserEngine -> real Chromium profile
```

This matters because fixed Python automation should not bypass profile governance by launching its own browser directly against a real `user-data-dir`.

Use `AutomationRunner` when you want a reusable local script that:

- claims a profile through the same central lock and occupancy rules as MCP
- records `automation` occupancy in the shared registry
- is blocked if the same profile is already occupied by MCP, GUI manual launch, or keepalive
- releases the profile through the same centralized close path

Reference example:

- `demo/managed_uc_gmail_titles_demo.py`

That demo proves a fixed `selenium_uc` script can:

- acquire `Profile 1` through the manager
- open Gmail with real login state
- extract the first three visible mail titles
- block a second same-profile process while the run is still active

Production direction now implemented in the codebase:

- shared occupancy entries can carry `owner_pid`, lease expiry, heartbeat metadata, and reclaimability
- stale non-MCP occupancies can be reaped automatically when the owning process disappears or a lease expires
- the daemon exposes profile status, recent occupancy events, and explicit reclaim endpoints
- the GUI shows profile occupancy state and provides manual reclaim for recovery workflows
- managed automation session acquisition can override runtime launch behavior such as `headless`, `incognito`, `start_minimized`, `mute_audio`, `window_size`, and `extra_args` through a temporary runtime config layer

The daemon now also exposes a managed automation HTTP flow for fixed scripts that should use the same governance model without speaking MCP directly:

```text
fixed script -> daemon HTTP API -> SessionManager -> BrowserEngine -> real Chromium profile
```

Current daemon automation endpoints:

- `POST /_daemon/automation/acquire`
- `POST /_daemon/automation/action`
- `POST /_daemon/automation/heartbeat`
- `POST /_daemon/automation/release`

Intended usage:

1. Acquire one profile session with runtime options.
2. Execute bounded actions through `automation/action`.
3. Refresh the lease if the script is long-running.
4. Release the session explicitly on normal completion.

Reference scripts:

- `demo/managed_daemon_smoke.py`
  Minimal daemon-side lifecycle smoke using `navigate`, `get_current_url`, and `get_page_text`.
- `demo/managed_daemon_gmail_titles_demo.py`
  Example of a site-task script driven through the daemon HTTP automation API.
- `demo/validate_daemon_automation.py`
  Full isolated validation harness that starts a temporary daemon, runs the smoke flow, and shuts it down again.
- `demo/validate_daemon_automation_failures.py`
  Isolated failure-semantics validation for token auth, bad payloads, conflict acquisition, stale session actions, and missing-profile reclaim.
- `docs/DAEMON_AUTOMATION_INTEGRATION.md`
  Dedicated integration guide for fixed scripts that use the daemon automation API directly.

Latest verified result:

- `demo/output/managed_daemon_smoke_20260611_224625.json`
- `demo/output/managed_daemon_smoke_20260611_225059.json`
- `demo/output/managed_daemon_smoke_20260611_230345.json`
- `demo/output/managed_daemon_failures_20260611_230315.json`

Validation note:

- `validate_daemon_automation.py` and `validate_daemon_automation_failures.py` both use the same isolated validation profile by default: `Profile 101`.
- Do not run those two validation scripts in parallel unless you override one of them to use a different validation profile and split user-data root.

Current boundary:

- the managed automation HTTP surface is now functional and validated for acquisition, action execution, heartbeat, and release
- this surface is intended for fixed-script execution, not arbitrary remote code upload
- profile busy-state is now evaluated per profile rather than copying one global daemon state onto every profile row
- orphan Chromium subprocess cleanup is better than earlier mirror-era behavior, but long-running real desktop environments still need continued hardening around residual utility processes

## Requirements

- Python 3.10+
- A local Chromium-compatible browser
- A matching ChromeDriver build
- A desktop environment

For AI-assisted setup after cloning, use the dedicated runbook:

- [AI Installation Runbook](./docs/AI_INSTALLATION_RUNBOOK.md)

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
  Legacy shared-root path kept for migration compatibility
- `paths.user_data_profiles_root`
  Split root that stores one dedicated UserData root per profile, for example `UserDataProfile1/Profile 1`
- `paths.mirror_user_data_root`
  Backup snapshot path. Runtime no longer depends on extracted mirror clones for normal MCP startup
- `paths.bookmarks_template_path`
  Optional bookmark template used when initializing profiles
- `paths.fingerprint_zip_path`
  Optional path related to `my-fingerprint`
- `app.language`
  UI language code such as `en`, `ja`, or `zh`
- `app.browser_engine`
  Default browser execution backend, currently `selenium_uc`, `patchright`, or `playwright_cli`
- `app.concurrency_mode`
  Session governance mode. `per_profile_live` is the current default and allows different profiles to run concurrently while keeping the same profile exclusive
- `launch.*`
  Browser launch defaults used by the built-in Python launcher, such as `new_window`, `start_maximized`, `load_fingerprint_extension`, `check_url`, and `extra_args`
- `mcp.host`, `mcp.port`, `mcp.worker_port`, `mcp.path`
  Network settings for the daemon and worker

### Per-profile live concurrency

The runtime now uses one dedicated UserData root per logical profile.

- `per_profile_live`
  Current default. Different profiles can run concurrently, but the same profile remains exclusive across GUI launch, keepalive, and MCP.
- `block`
  Optional conservative mode if you want historical single-session gating.

Important rules:

- Keepalive and mirror refresh are no longer global live-root operations; keepalive locks one profile at a time.
- Mirror snapshots are now backup artifacts, not the primary normal-session startup path.
- Same-profile parallelism is intentionally blocked.

### Manual launch and close behavior

The GUI `Launch` button is now a runtime toggle for the selected profile:

- If that profile is not currently running, the button launches a visible Chromium window for that profile.
- If Chromium for that profile is already running, the same button changes to `Close` and terminates only that profile's matching Chromium processes.
- If the user closes the Chromium window manually, the GUI re-detects the real process state and automatically changes the button back to `Launch` once the profile has fully exited.
- If the window is gone but a background Chromium process still remains, the GUI continues to show `Close` so operators can reclaim the leftover process explicitly.

This behavior is profile-scoped. It does not terminate other profiles.

## MCP service

When enabled in the GUI, the daemon exposes a stable HTTP endpoint such as:

```text
http://127.0.0.1:28888/mcp
```

The daemon stays available between tasks. The browser worker is started only when a request needs it, and it is reclaimed after the configured idle timeout.

Operational notes:

- If `mcp.api_token` is configured, every daemon request must send `Authorization: Bearer <token>`. There is no localhost bypass.
- If `mcp.admin_token` is not configured, admin-only daemon endpoints remain disabled instead of falling back to `mcp.api_token`.
- GUI status polling also uses the same bearer token, so the GUI and external MCP clients follow one authentication rule.
- The daemon is intended to stay stable while the worker is short-lived and lazily started.
- A worker reclaimed because of `idle_timeout` is a normal managed lifecycle event, not a crash.
- If the configured Chromium binary root already has live browser processes, session startup is intentionally blocked with states such as `external_chromium_running`.
- That busy-state rule is now enforced per profile root, including `playwright_cli`.
- MCP tools publish standard tool annotations so clients can distinguish trusted local/browser operations from arbitrary script execution.
- These annotations reduce unnecessary approval prompts in clients that honor MCP hints, but they do not bypass the client approval policy or this project's profile/busy-state governance.
- Normal profile/session operations, navigation, tab operations, clicking, typing, key presses, mouse actions, screenshots, diagnostics, and cleanup are treated as trusted low-risk MCP operations for local real-profile workflows.
- `run_script` and `run_script_batch` are intentionally high-trust, non-read-only actions because they execute arbitrary JavaScript inside a real logged-in browser context.

Typical MCP flow:

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. perform browser actions
7. `close_profile_session(session_id)`

For one multi-step task, keep a single session alive across the whole task.
Do not repeatedly `start_profile_session -> do one action -> close_profile_session`
for every small step unless the task explicitly needs isolation between steps.

Engine-aware callers may also pass an explicit engine when starting a session. If omitted, the configured GUI default engine is used.

For example:

```powershell
$token = "<your-api-token>"
Invoke-RestMethod -Uri 'http://127.0.0.1:28888/_daemon/status' -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 5 | ConvertTo-Json -Depth 8
```

If you configure an MCP client manually, the same token must be attached on every
request. Example Codex config shape:

```toml
[mcp_servers.browserIdentity]
url = "http://127.0.0.1:28888/mcp"

[mcp_servers.browserIdentity.http_headers]
Authorization = "Bearer <token>"
```

```text
can_start_profile_session(profile_name="Profile 4", engine="selenium_uc")
start_profile_session(profile_name="Profile 4", engine="playwright_cli")
```

### Multi-tab tools

The worker now exposes formal multi-tab operations so agents do not have to rely on hidden browser focus changes:

- `browser_list_tabs`
- `browser_open_tab`
- `browser_activate_tab`
- `browser_close_tab`
- Do not mix these session-bound tools with another MCP browser server such as generic `mcp:playwright/*`. A `browserIdentity` `session_id` belongs only to this service.

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
- `browser_get_action_trace`
- `get_mcp_tool_trace`

`browser_diagnose_page` is the highest-signal first stop when an agent gets blocked. It bundles the current interaction context together with recent console errors, page exceptions, failed requests, and recent bad HTTP responses. Heavy `playwright_cli` diagnostics are bounded by short CLI timeouts and truncated raw output so a noisy site should return a partial diagnosis instead of blocking the MCP worker for minutes.

`browser_get_action_trace` reports recent managed browser actions for one session, including slow actions, failures, fallback usage, and average duration. `get_mcp_tool_trace` reports MCP worker-level tool timings so real production calls can be inspected without digging through Codex internal logs. The MCP trace is also written to a JSONL file shown in the GUI MCP status panel and is rotated automatically to avoid unbounded log growth.

## Engine Notes

### Shared behavior

- Profile creation, deletion, syncing, bookmarks, and session ownership are shared across all engines
- GUI and MCP session flow stay the same regardless of engine
- Real `user-data-dir` plus `profile-directory` remain the source of truth

### Selenium plus undetected-chromedriver

- Currently the most mature path in the project
- Also powers the existing keepalive workflows
- Uses the shared `launch.*` defaults for direct profile launch
- Best current stealth-oriented option in the project
- Best current option when a page needs gesture unlock, slider drag, pattern input, or coordinate-level fallback
- Supports the full low-level gesture family, including multi-point `browser_mouse_gesture_path`
- Still the code-level fallback default if no configured engine is present

### Patchright

- Already supports real persistent profile sessions through the MCP/session layer
- Uses a smaller validated startup argument set than Selenium for compatibility
- Intended for sites where a Playwright-compatible execution model is more reliable
- Provides the strongest tab model and the richest structured debug telemetry in the current project
- Collects DevTools-style diagnostics through per-tab CDP sessions, so agents can read console output, uncaught exceptions, and network failures without opening browser DevTools manually
- Supports the formal coordinate and gesture tool family, including multi-point `browser_mouse_gesture_path`
- Keepalive is not routed through Patchright yet in this stage

### Playwright CLI

- Current preferred engine for normal MCP task execution on this machine when the GUI default is set to `playwright_cli`
- Once a session is started here, all follow-up tab/click/type/snapshot/diagnostic actions should stay on this MCP service. Cross-routing the same task into another browser MCP is an agent integration bug, not a supported pattern.
- Best fit for lower-overhead task execution in the new per-profile live runtime
- Native stealth is weaker than `selenium_uc`
- Native inspection fidelity is weaker than `patchright`, but the managed runtime lifts it with fallbacks, diagnostics, and structured recovery metadata
- Added as a third parallel engine under the same `SessionManager -> BrowserEngine factory` path
- Uses `playwright-cli open --persistent` only for startup, then reuses the named session for later commands
- Reuses the real `user-data-dir` together with Chromium `--profile-directory=Profile N`, so logged-in state can be preserved
- Supports the validated first-stage surface: session start, navigation, multi-tab basics, script execution, type/click/key actions, screenshot, console, requests, and coarse page diagnostics
- Managed runtime fallbacks lift the raw CLI session with generic `snapshot`, candidate enumeration, waiting, target verification, and snapshot-ref style targeting where possible
- Does not currently implement the formal gesture/XY tool family, including `browser_mouse_gesture_path`. If the task depends on drag, slider movement, pattern unlock, or coordinate-level fallback, switch to `selenium_uc` or `patchright`.
- Uses a fast DOM eval path for simple selector `click` and `fill` operations, then falls back to native `playwright-cli` commands if the DOM path is not safe or fails
- `run_script` now prefers a safer serialization wrapper, and generic page text reads can fall back to bounded DOM chunking or page-text extraction when direct structured extraction comes back empty
- Even with that generic fallback, complex dynamic frontends can still produce noisy or low-fidelity structured output. If the task depends on precise structured extraction rather than resilient fallback, prefer `patchright`
- Classifies console and network noise into categories such as third-party, asset, media, security policy, CORS, and auth, so diagnostics can separate useful signal from common site noise
- Sanitizes the upstream `playwright-cli` Chromium launch args so `AutomationControlled` is not injected through `--disable-blink-features`
- Honors `mcp.start_minimized=true` by default, so visible MCP browser sessions start minimized in the taskbar instead of stealing desktop focus while still allowing the user to click in and take over when needed
- Keeps `mcp.headless=false` by default; headless mode is only for explicit user-requested regression or background validation, not the normal MCP browsing path
- Supports `runtime_options.incognito=true` for isolated validation when the caller wants the same governed profile path but does not want to inherit the normal regular-window session state
- On session close, the runtime attempts to terminate owned `playwright-cli` daemon and Chromium processes and then cleans isolated runtime directories; startup also prunes stale empty or old `chromium-advanced-playwright-cli-*` temp directories that are not referenced by live processes
- Shared-root runtime is now treated as a migration-only legacy layout. Normal operation should use `paths.user_data_profiles_root`
- Keepalive is not routed through `playwright_cli` in this stage
- Windows packaged GUI, daemon, and worker executables have been validated against the managed runtime path

### Selenium plus undetected-chromedriver debug notes

- Selenium sessions now expose the same high-level tab and debug tools where Chromium logging supports them
- Console and network diagnostics are gathered from browser and performance logs, so they are best-effort compared with Patchright
- Structured accessibility snapshots and snapshot-ref targeting still remain Patchright-only
- Use `selenium_uc` when stealth, anti-detection tolerance, or challenge-heavy browsing matters more than throughput
- Current challenge validation against `skrbtso.top` confirmed that `selenium_uc` can pass recurring recaptcha/browser-verification gates and still reach both result pages and detail pages
- Managed post-action context and `browser_diagnose_page` now expose an `anti_bot` block so callers can distinguish likely real challenge pages from normal pages that merely mention providers such as Cloudflare

## Cross-platform notes

This is a Python project and the source code is being kept platform-aware.

- Windows is the primary tested platform
- macOS and Linux are supported at source level when valid browser and driver paths are provided
- Windows packaging is currently the most complete desktop packaging path

## Skill templates

The repository includes reusable agent skill templates in:

- `docs/skill_templates/`

These files are examples for Codex or other AI workflows that need to consume this MCP service consistently. They are templates, not an auto-loaded runtime directory.

The skill guidance should explicitly tell agents that:

- `playwright_cli` is the normal default for ordinary MCP work
- `patchright` should be selected when structured extraction or deep frontend diagnostics matter more than throughput
- `selenium_uc` should be selected when stealth, anti-bot tolerance, recurring challenge pages, gesture flows, or coordinate fallback matter more than speed
- `runtime_options.incognito=true` is available when a flow should be validated without inheriting the current regular-window session state

## Keepalive Plugins

Keepalive sites use a plugin-style runtime. Built-in site logic exists for `chatgpt`, `google`, `gmail`, and `github`; custom Python plugins can add new site IDs such as `youtube` or `youtube_studio` without rebuilding the app. The desktop GUI now includes a dedicated Keepalive Plugins tab for browsing built-in plugin source, creating external plugins, and editing trusted local plugin code. Additional trusted plugin directories can still be configured in the GUI keepalive settings or in `keepalive.plugin_dirs`.

- [Keepalive Plugin Guide](./docs/KEEPALIVE_PLUGIN_GUIDE.md)

## Operational notes

- The GUI `Account` column is only an operator label. It is not a guaranteed website account identity.
- Keepalive cleans cache-like data for the finished profile automatically after each profile run. It removes re-creatable cache, lock, and log artifacts rather than wiping the whole profile.
- A keepalive batch is still one global keepalive task, but runtime locking is profile-scoped. In normal `per_profile_live` mode, other profiles can still be used by MCP as long as they are not the same locked profile.

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
