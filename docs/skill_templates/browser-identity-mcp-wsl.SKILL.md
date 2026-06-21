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
- `official_playwright_mcp`

Recommended engine-selection policy remains the same from WSL:

- default to `patchright` for ordinary MCP task execution and most real workflows
- use `selenium_uc` for stealth-sensitive pages, recurring challenge/verification pages, or when gesture/coordinate fallback matters more than raw speed
- use `playwright_cli` as a lightweight compatibility or diagnostic path, not as the normal default
- treat `official_playwright_mcp` as an experimental backend name only; do not choose it for routine live-profile work from WSL unless the runtime explicitly documents that the bundled official backend was enabled for that build

Important engine capability examples from WSL remain the same:

- `patchright`
  best default for structured extraction, complex frontend interaction, richer diagnostics, and the most complete mainstream action surface
- `selenium_uc`
  prefer this when the target is stealth-sensitive, shows automation friction, repeatedly triggers challenge/verification pages, or needs gesture unlock, drag, slider movement, or coordinate-level mouse fallback
- `playwright_cli`
  prefer this for lightweight compatibility flows and bounded diagnostics
- `official_playwright_mcp`
  currently a reserved backend slot for future bundled official-runtime integration; at this stage it should be expected to fail fast rather than open a normal live persistent-profile session
- `gesture_actions`
  treat `browser_mouse_move_xy`, `browser_mouse_click_xy`, `browser_mouse_drag_xy`, and `browser_mouse_gesture_path` as engine-scoped capabilities, not as guaranteed fallback tools on every runtime
- prefer the high-level gesture path first:
  - `browser_detect_gesture_grid(...)`
  - `browser_unlock_gesture_pattern(...)`
- only drop to raw coordinate/continuous-path calls when gesture-grid detection fails or the control is genuinely freeform

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
- `handle_dialog(...)`
- `file_upload(...)`

Prefer these before arbitrary `run_script(...)` when the interaction is a normal browser task.

Official-style compatibility aliases are also available from WSL:

- `browser_tabs`
- `browser_take_screenshot`
- `browser_close`
- `browser_handle_dialog`
- `browser_file_upload`
- `browser_resize`
- `browser_network_request`

If the upstream prompt looks written for an official `playwright-mcp`-style tool surface, prefer these aliases instead of mixing MCP families.

`browser_tabs` should also be treated as a real action tool from WSL:

- `action="list"`
- `action="new"` with optional `url`
- `action="select"` with `index`
- `action="close"` with `index`

For network diagnostics from WSL, prefer the pair:

- `browser_get_network_requests` for list-oriented inspection
- `browser_network_request` for official-style single-request detail lookup by 1-based index

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

That structured page model is now broader as well. It can additionally expose:

- likely primary actions
- search and filter style controls
- navigation-oriented controls
- collection signals for list/table/thread-heavy pages
- collection summaries such as comment threads, message lists, repository lists, and generic result lists
- toolbar controls and status surfaces that can guide the next follow-up action
- lightweight role density and interactive label previews

For target-local debugging from WSL, prefer `browser_diagnose_target(...)` when the task is about one control or local panel. Its `structured_region` block now includes:

- `region_kind`
- `interactive_controls`
- `visible_controls`
- `overlay_controls`
- `dialog_controls`
- `interactive_density`
- `primary_actions`
- `search_like_controls`
- `status_controls`
- `role_counts`

Managed verification surfaces are also more uniform from WSL:

- `browser_verify_text(...)`, `browser_verify_dialog(...)`, and `browser_verify_element(...)` normalize `verified` and `matched`
- `browser_verify_target_value(...)` and `browser_verify_target_visible(...)` also normalize `verified`, `matched`, `target`, and `by`
- `browser_describe_target(...)` and `browser_list_candidates(...)` expose a lightweight `target_summary`
- `browser_list_candidates(...)` candidates now also expose `match_reason` and `ranking_reason`, so retry logic from WSL should inspect ranking intent before broadening selectors
- `run_script_batch(...)` now also returns `ok_count`, `error_count`, `all_ok`, and `first_error` in addition to per-item results

On the default `patchright` path, successful high-frequency actions also leave behind a richer `post_action_context` more often than before. Use that as the first continuation surface before escalating to a heavier extra call. In normal cases it can already include:

- a bounded `snapshot`
- derived `structured_page`
- lightweight `interaction_hints`
- recent action/session-health context

On ordinary successful fast-path actions, that continuation surface is also intentionally lighter than a full diagnosis pass. Heavy anti-bot probing is deferred on normal success paths, so the caller still gets useful continuation context without paying a hidden full-page diagnostics cost after every click or type action.

On the default `patchright` path, candidate ordering is also more semantic now. Popup items, search/filter controls, and likely primary actions receive stronger ranking signals, so complex frontend follow-up steps should need fewer exploratory retries from WSL as well.

That follow-up ranking also now reuses recent managed context:

- recent `structured_page` and `interaction_hints` are cached inside the managed session
- the active interaction region such as `overlay` or `dialog` can bias the next candidate search
- collection-heavy pages can bias follow-up steps toward the active collection kind such as `comment_threads`, `message_list`, `repository_list`, or `result_list`
- toolbar/filter/search/status labels extracted from the previous step can boost the next ranking pass before the runtime falls back to broad full-page probing

Runtime isolation note from WSL:

- `runtime_options.incognito=true`
  is currently available on the managed daemon automation path when the caller wants the same governed profile selection but a fresh isolated browser session state for the target flow
- `runtime_options.resource_only=true`
  is currently available on the managed daemon automation path when the caller only needs exclusive access to the governed profile files and not a live browser session
- do not assume the current `browserIdentity` MCP `start_profile_session(...)` tool directly accepts `runtime_options` from WSL; use daemon automation when incognito is required today

How to switch engines explicitly:

- `can_start_profile_session(profile_name="Profile 4", engine="patchright")`
- `start_profile_session(profile_name="Profile 4", engine="selenium_uc")`
- for difficult dynamic pages from WSL as well, prefer `browser_get_interaction_context(...)`, `browser_list_candidates(...)`, and other structured surfaces before depending on raw `run_script(...)` readback
- if `run_script(...)` returns `script_result_state="stringified"`, treat that as a serialization boundary, not as a normal structured success

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

Do not assume the Windows-side `%USERPROFILE%\.codex\config.toml` is automatically reused by a WSL Codex environment. Treat them as separate client configurations unless you have explicitly unified them.

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
- `runtime_options.incognito=true` on the managed daemon automation path remains subject to the same profile occupancy and session-governance rules; it is an isolation mode, not a concurrency bypass.
- `runtime_options.resource_only=true` on the managed daemon automation path remains subject to the same profile occupancy and session-governance rules; it is a resource lease for external tools, not a way to bypass profile locking.
- If the target page needs gesture/pattern unlock, drag, slider movement, continuous path input, or coordinate-level mouse fallback, do not assume the default engine is enough. Start the session with `engine="selenium_uc"` or `engine="patchright"` explicitly.
- `browser_mouse_gesture_path(session_id, points=[...])` is the preferred gesture interface from WSL as well. Prefer `patchright` first for frontend-heavy pages and `selenium_uc` when stealth pressure is higher.
- Use `get_session_capabilities(session_id)` when the task may need gesture actions. The capability surface distinguishes generic coordinate support from formal `gesture_actions` support.
- If the Windows side reports `external_chromium_running`, do not assume the entire service is blocked. Check whether the target profile itself is the one that is occupied.
