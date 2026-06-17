---
name: browser-identity-mcp-wsl
description: Use when a task needs to access a Windows-hosted browser MCP service from WSL. Apply this skill when Codex must discover the reachable Windows-side MCP address from WSL, verify connectivity, use the WSL-reachable host instead of assuming localhost, and still follow safe identity selection and occupancy checks before starting browser work.
---

# Browser Identity MCP WSL

## Overview

When a browser MCP service runs on Windows and the caller runs inside WSL, do not assume `127.0.0.1` works. WSL often needs the Windows host IP instead.

Use this skill only for the WSL-to-Windows access layer. The normal identity, occupancy, and target-site account verification rules from the main browser identity MCP workflow still apply.

For the Chromium Profile Manager service on this machine:

- MCP server id used by Codex:
  `browserIdentity`
- default Windows endpoint:
  `http://127.0.0.1:28888/mcp`
- daemon auth:
  if `mcp.api_token` is configured, every daemon request must send `Authorization: Bearer <token>` with no localhost bypass
- daemon control auth:
  GUI/control endpoints use `control.api_token`, which may differ from the business token
  if `control.api_token` is absent, `/_control/*` endpoints stay disabled instead of falling back to `mcp.api_token`
- important configuration boundary:
  this skill alone does not register an MCP server; the WSL-side Codex config must also define `mcp_servers.browserIdentity`

## Workflow

1. Confirm that the browser MCP service is actually running on Windows.
2. Determine the Windows-side host IP visible from WSL.
3. Confirm that the WSL-side Codex config actually registers `browserIdentity` as an MCP server. If not, you only have skill guidance, not a live MCP connection.
4. Test connectivity from WSL to the Windows MCP endpoint.
5. Use the reachable WSL-side URL for MCP calls.
6. Before starting a browser session, still check occupancy and confirm the browser profile identity parameter with the user if it is not specified.
7. For multi-tab or debug-heavy tasks, use the same explicit tab activation and structured debug tools as the normal browser identity MCP workflow.

Tool-family boundary still applies from WSL:

1. once a session was created through `browserIdentity`, keep all subsequent browser actions inside the same `browserIdentity` MCP service
2. do not open the session with `browserIdentity` and then send tab or click actions to generic `mcp:playwright/*` tools
3. if richer capabilities are needed, restart with another `engine` on `browserIdentity` instead of mixing MCP families

For the Chromium Profile Manager service on this machine, the supported engine values remain:

- `selenium_uc`
- `patchright`
- `playwright_cli`

Recommended engine-selection policy remains the same from WSL:

- default to `patchright` for ordinary MCP task execution and most real workflows
- use `selenium_uc` for stealth-sensitive pages, recurring challenge/verification pages, or when gesture/coordinate fallback matters more than raw speed
- use `playwright_cli` as a lightweight compatibility or diagnostic path, not as the normal default

Important engine capability examples from WSL remain the same:

- `patchright`
  best default for structured extraction, complex frontend interaction, richer diagnostics, and the most complete mainstream action surface
- `selenium_uc`
  prefer this when the target is stealth-sensitive, shows automation friction, repeatedly triggers challenge/verification pages, or needs gesture unlock, drag, slider movement, or coordinate-level mouse fallback
- `playwright_cli`
  prefer this for lightweight compatibility flows and bounded diagnostics
- `gesture_actions`
  treat `browser_mouse_move_xy`, `browser_mouse_click_xy`, `browser_mouse_drag_xy`, and `browser_mouse_gesture_path` as engine-scoped capabilities, not as guaranteed fallback tools on every runtime

Recently strengthened high-level actions from WSL are the same:

- `wait_for_text(...)`
- `wait_for_text_gone(...)`
- `wait_for_text_change(...)`
- `wait_for_page_stable(...)`
- `watch_page_state(...)`
- `watch_target_state(...)`
- `wait_for_timeout(...)`
- `hover(...)`
- `select_option(...)`
- `navigate_back(...)`
- `navigate_forward(...)`
- `drag_target(...)`

Prefer these before arbitrary `run_script(...)` when the interaction is a normal browser task.

For dynamic pages from WSL as well:

- use `wait_for_page_stable(...)` before re-reading page state when the target is still re-rendering
- use `wait_for_text_change(...)` when polling for task/status changes
- use `watch_page_state(...)` when one call should cover baseline capture, state change, and stabilization
- use `watch_target_state(...)` when the task is about one dynamic control or target-local region rather than the entire page
- if `run_script(...)` returns `result=null`, treat that as a diagnostic state; the runtime now adds `script_result_state="null"` and a hint

Recent managed-result normalization is the same from WSL:

- `open_tab(...)`, `activate_tab(...)`, `close_tab(...)`
  now expose stable fields such as `opened`, `activated`, `closed`, `active_tab_id`, `closed_tab_id`, and `tab_count`
- `wait_for(...)`, `wait_for_timeout(...)`
  now expose normalized `condition`, `by`, `waited`, and `timeout_ms`
- `type_target_and_verify(...)`
  now keeps `target`, `requested_target`, `by`, `value`, and `verified` aligned

`browser_diagnose_page(...)` should be treated as a generic structured-page surface from WSL as well. Its `structured_page` block now summarizes interactive controls, form controls, custom elements, region density such as dialog/menu/listbox/tab, and a current interaction-region hint.

Runtime isolation option from WSL:

- `runtime_options.incognito=true`
  use this when the caller wants the same governed profile selection but a fresh isolated browser session state for the target flow

How to switch engines explicitly:

- `can_start_profile_session(profile_name="Profile 4", engine="patchright")`
- `start_profile_session(profile_name="Profile 4", engine="selenium_uc")`

Do not assume the service is single-engine. This MCP exposes multiple browser backends behind one profile/session interface, and the caller is allowed to choose the engine per new session.

Changing the GUI default engine still affects only future sessions. Existing sessions keep their original engine, and same-profile multi-engine starts are blocked rather than mutating a live session in place.

For `playwright_cli`, the Windows runtime sanitizes upstream Chromium launch args so `AutomationControlled` is not injected through a real `--disable-blink-features` switch. Visible MCP sessions normally honor `mcp.start_minimized=true` so they stay in the taskbar instead of stealing focus while still allowing the user to click in and take over. Do not enable `mcp.headless=true` just to reduce desktop interference; use headless only when the user explicitly asks for headless/regression/background validation.

## Typical Host Discovery

Inside WSL, the Windows host is often the default gateway:

```bash
ip route | awk '/default/ {print $3; exit}'
```

If the Windows MCP service listens on port `28888`, the effective WSL URL often looks like:

```text
http://<windows-host-ip>:28888/mcp
```

Typical WSL Codex config shape:

```toml
[mcp_servers.browserIdentity]
url = "http://<windows-host-ip>:28888/mcp"

[mcp_servers.browserIdentity.http_headers]
Authorization = "Bearer <token>"
```

WSL callers should also keep one browser session open across a multi-step task
instead of repeatedly acquiring and closing the same profile for each small
action. Reuse the same `session_id` until the task or subtask is complete.

On Linux/WSL, this is typically stored in:

```text
~/.codex/config.toml
```

Do not assume the Windows-side `C:\Users\Administrator\.codex\config.toml` is automatically reused by a WSL Codex environment. Treat them as separate client configurations unless you have explicitly unified them.

## Connectivity Check

From WSL, use a short timeout test before doing real MCP work:

```bash
curl --max-time 5 -I \
  http://<windows-host-ip>:28888/mcp \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Authorization: Bearer <token>'
```

HTTP error responses such as `400` or `405` can still mean the service is reachable; the key distinction is whether the TCP connection succeeds.

## Required Behavior

- Do not assume WSL `127.0.0.1` reaches the Windows service.
- Do not assume installing this skill also creates the MCP server entry in WSL Codex.
- If WSL Codex has no `mcp_servers.browserIdentity` entry, treat that environment as skill-only guidance, not a real MCP integration.
- Verify that the Windows service is listening on a WSL-reachable host such as `0.0.0.0` or a specific LAN address.
- If WSL cannot reach the service, check Windows firewall rules and listening host configuration before debugging MCP semantics.
- If auth is enabled on Windows, include the bearer token on every WSL-side connectivity check and MCP request.
- Do not assume the MCP token can call control routes. `/_control/*` operations require the control token when the daemon is configured with one.
- After connectivity is confirmed, follow the same identity confirmation and occupancy rules as the normal browser identity MCP workflow.
- After connectivity is confirmed, do not infer a target website account from the GUI profile account label; verify the actual site login inside the target website when account correctness matters.
- After connectivity is confirmed, prefer MCP debug tools such as `browser_get_console_messages`, `browser_get_page_errors`, `browser_get_network_requests`, `browser_diagnose_page`, `browser_get_action_trace`, and `get_mcp_tool_trace` over screenshot-only diagnosis.
- After connectivity is confirmed, prefer `session_health.recovery_actions`, `session_health.page_drift`, and `resolution_trace` over ad-hoc retries when a dynamic page fails under WSL.
- The Windows MCP server publishes standard tool annotations, treating normal profile/session operations, navigation, tab operations, browser actions, screenshots, diagnostics, and cleanup as trusted low-risk.
- `run_script` and `run_script_batch` remain high-trust, non-read-only surfaces because they execute arbitrary JavaScript inside a real logged-in browser context. Use those hints when the WSL-side client supports trusted/read-only execution, but do not bypass identity or occupancy checks.
- Treat partial `playwright_cli` diagnostics with `diagnostic_errors` as useful signal. The runtime intentionally bounds heavy console/network calls, classifies common noise, and avoids long MCP worker stalls.
- `playwright_cli` simple selector click/fill actions may use the fast DOM eval path before native CLI fallback. This is expected and should be treated as the high-performance path.
- `playwright_cli` now has safer generic script/text fallback behavior, but difficult dynamic frontends can still produce weaker structured extraction than `patchright`. Prefer engine switching over site-specific assumptions.
- Managed post-action context and `browser_diagnose_page` now include an `anti_bot` block. Treat `anti_bot.detected=true` with strong markers or structured signals as a likely real challenge page.
- Do not treat every page that merely mentions Cloudflare as a challenge automatically. Normal search/detail pages can mention Cloudflare while still being valid result pages.
- `runtime_options.incognito=true` remains subject to the same profile occupancy and session-governance rules; it is an isolation mode, not a concurrency bypass.
- If the target page needs gesture/pattern unlock, drag, slider movement, continuous path input, or coordinate-level mouse fallback, do not assume the default engine is enough. Start the session with `engine="selenium_uc"` or `engine="patchright"` explicitly.
- `browser_mouse_gesture_path(session_id, points=[...])` is the preferred gesture interface from WSL as well. Prefer `patchright` first for frontend-heavy pages and `selenium_uc` when stealth pressure is higher.
- Use `get_session_capabilities(session_id)` when the task may need gesture actions. The capability surface distinguishes generic coordinate support from formal `gesture_actions` support.
- If the Windows side reports `external_chromium_running`, do not assume the entire service is blocked. Check whether the target profile itself is the one that is occupied.
