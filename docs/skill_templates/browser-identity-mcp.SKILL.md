---
name: browser-identity-mcp
description: Use when a task needs to control a browser through an MCP service that exposes real browser profile identities, personas, browser slots, or user spaces with persistent site login state. Apply this skill across projects whenever Codex must choose or confirm an identity-bearing browser context, check service occupancy before starting work, avoid conflicting sessions, verify target-site login state when needed, and release the session when finished.
---

# Browser Identity MCP

## Overview

Treat browser identity as a scarce shared resource. In some projects the identity parameter may be named `profile_name`, but in others it may be `persona`, `slot`, `browser_id`, or something similar.

A browser profile is a persistent browser data container. It can store cookies, local storage, extensions, bookmarks, permissions, and login state for many unrelated websites. It is not the same thing as a universal website account.

For the Chromium Profile Manager service, the GUI profile `Account` field is only a human-maintained profile label or note. It must not be treated as proof of the currently logged-in account on GitHub, YouTube, ChatGPT, Gmail, Google, or any other target site. The only close relationship is with Google-family workflows when the label was intentionally maintained as the Google account hint, and even then the target site must still be verified when account correctness matters.

Always optimize for safety over convenience: if the user did not specify which identity to use, ask before taking control of a real logged-in browser context.

For the Chromium Profile Manager service used on this machine:

- MCP server id used by Codex:
  `browserIdentity`
- MCP endpoint:
  `http://127.0.0.1:28888/mcp`
- daemon auth:
  if `mcp.api_token` is configured, every business request must send `Authorization: Bearer <token>` with no localhost bypass
- daemon control auth:
  GUI/control endpoints use `control.api_token`, which may differ from the business token
  if `control.api_token` is absent, `/_control/*` endpoints stay disabled instead of falling back to `mcp.api_token`
- daemon model:
  a stable daemon listens on `28888`
- worker model:
  a browser-capable MCP worker is policy-controlled: `lazy`, `sticky`, or `always_on`
- identity parameter:
  `profile_name`

## Workflow

1. Identify the browser identity parameter used by the current project or MCP service.
2. If the user did not specify the identity value, ask which identity to use before starting a session.
3. Query service state before session startup.
4. If the service is busy, report who or what is occupying it and do not force a new session unless the project explicitly supports safe reuse.
5. Start the session only after the service reports it is available.
6. Perform the browser work.
7. Release the session when finished.

For multi-step tasks, keep one session open for the whole task. Do not repeatedly start and close the same profile session for each individual page action unless the task explicitly requires isolation.

## Session Red Lines

- Default rule: one user task uses one browserIdentity session.
- After `start_profile_session(...)`, keep reusing the returned `session_id` until the whole task is finished.
- Do not wrap each browser step in its own `start_profile_session(...)` / `close_profile_session(...)` pair.
- Do not close and recreate the session just because a new page, new site area, or new result page is needed.
- When a new page is needed inside the same task, prefer `browser_open_tab(...)`, `browser_activate_tab(...)`, and normal navigation within the existing session.
- Only recreate a session when the current session is confirmed unrecoverable, or when the user explicitly asks for isolation.
- If a step fails, first inspect `session_health`, `recovery_actions`, `resolution_trace`, and page diagnostics before deciding to recreate the session.
- Repeated acquire/release within one task is considered incorrect usage because it causes browser churn, weakens session stability, and makes real-profile debugging noisy.

## Required Behavior

- Never guess a real-login identity automatically.
- Never infer the target website account from the GUI profile `Account` label.
- Never start a new session blindly; check occupancy first.
- If the service reports a busy state, surface that clearly to the user.
- If the same identity is already occupied, do not steal it unless the project explicitly supports safe reuse and the user intends that reuse.
- When the task depends on a specific website login, verify that website's actual logged-in account inside the page before doing account-sensitive work.
- Always close or release the session after the task completes unless the user explicitly asks to keep it open.
- For one task with many browser steps, prefer one `start_profile_session(...)`, many page actions, then one `close_profile_session(...)`.
- If direct daemon HTTP verification is needed, include the configured bearer token on every request instead of assuming localhost is trusted.
- Do not assume the same token can call control endpoints. `/_control/*` operations require the control token when the daemon is configured with one.

## Parameter Mapping

Translate the project-specific field into this mental model:

- identity parameter:
  the field that selects the real browser profile/container, such as `profile_name`, `persona`, or `slot`
- state query:
  the API or tool that reports whether the browser service is idle, starting, keepalive-running, mirroring, active for other profiles, or externally busy
- session start:
  the API or tool that claims the identity and returns a session handle
- session release:
  the API or tool that closes the claimed session and frees the identity

## Browser Profile vs Site Account

Do not collapse these concepts:

- Browser profile:
  the Chromium data container selected by `profile_name`, such as `Profile 1`
- GUI account label:
  an optional operator-maintained note shown in the desktop GUI; useful as a hint, not authority
- Site account:
  the account that the current page is actually logged into for a specific website

Account-sensitive tasks must verify the site account using site evidence. Examples:

- GitHub:
  read `meta[name="user-login"]`, the account menu, or another GitHub-owned login indicator
- Google or YouTube:
  inspect the Google account menu, page identity metadata, or other Google-owned account indicators
- ChatGPT or other apps:
  inspect the app's own account menu, settings page, or authenticated API/page metadata

If the requested target account and the verified site account do not match, stop and report the mismatch instead of continuing with the wrong account. If the site is signed out, report that the chosen browser profile does not currently have usable login state for that site.

## Typical Tool Order

Use the project's equivalent of this sequence:

1. `get_server_status`
2. `get_profile_status`
3. `can_start_*` or equivalent preflight check
4. `start_*session`
5. browser actions
6. `close_*session`

For the Chromium Profile Manager MCP, prefer this exact order:

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. browser actions
7. `close_profile_session(session_id)`

For multi-tab work inside one session, prefer this extension:

1. `browser_list_tabs`
2. `browser_open_tab(...)` when needed
3. `browser_activate_tab(...)` before interacting with a different tab
4. page actions on the active tab
5. `browser_close_tab(...)` if the tab is no longer needed

Tool-family boundary:

1. once a session was created through `browserIdentity`, keep all subsequent browser actions inside the same `browserIdentity` tool family
2. do not switch mid-task to another MCP server such as generic `mcp:playwright/*`, `browser-use`, or unrelated browser tools just because they expose similarly named tab APIs
3. `session_id` returned by `browserIdentity` is only valid for this MCP service; it is not transferable to another MCP server
4. if the task needs richer capabilities than the current engine exposes, restart the task with a different `engine` on `browserIdentity` instead of mixing tool families

For long tasks or agent workflows that naturally pause between actions:

1. start one session
2. reuse the same `session_id` across all steps
3. only close when the whole task or subtask is complete

Do not treat `start_profile_session` and `close_profile_session` as per-action wrappers.

If the task needs multiple pages, tabs, or navigations, stay inside the same session and expand the work there. Do not interpret "open another page" as permission to release and reacquire the same profile.

Bad pattern:

1. `start_profile_session(...)`
2. do one click or one page read
3. `close_profile_session(...)`
4. `start_profile_session(...)` again for the next step

Correct pattern:

1. `start_profile_session(...)`
2. perform all page actions for the task
3. use `browser_open_tab(...)` or navigation when another page is needed
4. only call `close_profile_session(...)` once the whole task is complete

If an action fails and the page looks dynamic or broken, prefer structured diagnostics before guessing:

1. `browser_get_interaction_context`
2. `browser_get_console_messages`
3. `browser_get_page_errors`
4. `browser_get_network_requests`
5. `browser_diagnose_page`
6. `browser_get_action_trace` when repeated actions feel slow or flaky
7. `get_mcp_tool_trace` when the worker itself appears slow
8. inspect `session_health.recovery_hint`, `session_health.recovery_actions`, and `failure_classification`
9. when a target-oriented read fails or looks ambiguous, inspect `resolution_trace` before retrying with different selectors
10. if `failure_classification == "page_drift"`, recover the expected tab/page first instead of immediately changing selectors or recreating the session

If `can_start_profile_session(profile_name)` reports `allowed=false` but also reports the same identity as reusable, treat that as "do not start a second session automatically." Only use `reuse_existing=true` when the current project explicitly supports it and the user intends that reuse.

If the project needs a specific browser backend, pass the optional `engine` parameter explicitly when checking or starting a session, for example `engine="selenium_uc"` or `engine="patchright"`. If omitted, the GUI-configured default engine will be used.

For the Chromium Profile Manager service on this machine, the supported engine values are:

- `selenium_uc`
- `patchright`
- `playwright_cli`

Recommended engine-selection policy for this project:

- default to `patchright` for ordinary MCP browsing tasks and most real production flows
- switch to `selenium_uc` when stealth, anti-detection tolerance, recurring challenge pages, or gesture/coordinate fallback matter more than raw speed
- use `playwright_cli` as a lightweight compatibility or diagnostic path, not as the default high-capability path

Important engine capability examples:

- `patchright`
  best default for structured extraction, complex frontend interaction, richer diagnostics, more complete high-level action coverage, and mainstream MCP work
- `selenium_uc`
  prefer this when the target is stealth-sensitive, shows automation friction, repeatedly triggers challenge/verification pages, or needs coordinate-level mouse actions such as drag, gesture unlock, slider/pattern input, or vision-style XY fallback
- `playwright_cli`
  prefer this for lightweight compatibility flows, bounded diagnostics, or lower-overhead tasks when the stronger `patchright` path is not required
- `gesture_actions`
  treat `browser_mouse_move_xy`, `browser_mouse_click_xy`, `browser_mouse_drag_xy`, and `browser_mouse_gesture_path` as a formal capability boundary rather than a generic fallback every engine should support

Recently strengthened high-level actions:

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

Prefer these higher-level actions before falling back to arbitrary `run_script(...)` when the task is a mainstream browser interaction problem.

For dynamic pages:

- use `wait_for_page_stable(...)` before re-reading text or candidates when the page is visibly re-rendering
- use `wait_for_text_change(...)` when monitoring state changes or long-running task output
- use `watch_page_state(...)` when the task is naturally "watch this until it changes and settles"
- use `watch_target_state(...)` when the task is about one dynamic control, popup choice, local status block, or target-local region
- if `run_script(...)` returns `result=null`, treat that as a first-class diagnostic state rather than a clean success; the runtime now adds `script_result_state="null"` and a hint

Recent managed-result normalization also means callers should expect stronger cross-engine consistency for:

- `open_tab(...)`, `activate_tab(...)`, `close_tab(...)`
  expect stable fields such as `opened`, `activated`, `closed`, `active_tab_id`, `closed_tab_id`, and `tab_count`
- `wait_for(...)`, `wait_for_timeout(...)`
  expect normalized `condition`, `by`, `waited`, and `timeout_ms`
- `type_target_and_verify(...)`
  expect aligned `target`, `requested_target`, `by`, `value`, and `verified`

`browser_diagnose_page(...)` should also be treated as a more general structured-page primitive now, not only a text dump. Its `structured_page` block can summarize interactive controls, form controls, custom elements, region density such as dialog/menu/listbox/tab, and a current interaction-region hint.

In current builds that structured page model is broader than the initial version. It can also surface:

- likely primary actions
- search and filter style controls
- navigation-oriented controls
- collection signals for list/table/thread-heavy pages
- lightweight role density and interactive label previews

For target-local debugging, prefer `browser_diagnose_target(...)` when the task is really about one control or one local region. Its `structured_region` block now includes:

- `region_kind`
- `interactive_controls`
- `primary_actions`
- `search_like_controls`
- `status_controls`
- `role_counts`

Managed verification surfaces are also more uniform now:

- `browser_verify_text(...)`, `browser_verify_dialog(...)`, and `browser_verify_element(...)` normalize `verified` and `matched`
- `browser_describe_target(...)` and `browser_list_candidates(...)` expose a lightweight `target_summary`

Runtime isolation option:

- `runtime_options.incognito=true`
  use this when the caller wants to keep the same governed profile selection but validate a flow without inheriting the normal regular-window site session state

How to switch engines explicitly:

- rely on the GUI-configured default engine for normal work, which should now be `patchright`
- set `engine="selenium_uc"` when a page needs gesture/drag/XY mouse actions
- set `engine="patchright"` explicitly when the caller wants to pin the strongest structured path
- set `engine="playwright_cli"` only when a lightweight compatibility path is intentionally desired

Example:

- `can_start_profile_session(profile_name="Profile 4", engine="selenium_uc")`
- `start_profile_session(profile_name="Profile 4", engine="selenium_uc")`

Do not assume the service is single-engine. This MCP exposes multiple browser backends behind one profile/session interface, and the caller is allowed to choose the engine per new session.

Important engine-switching boundary:

- changing the GUI default engine affects only future sessions
- an already running session keeps the engine it started with
- `reuse_existing=true` should only be used when the caller wants the same profile and the same engine session
- starting the same profile with a different engine still does not hot-switch the existing session in place

## Chromium-Specific Notes

- `browser-identity-mcp` is the skill name, not the MCP server id.
- The MCP server id registered in Codex is `browserIdentity`.
- Do not guess `profile_name`. If the user did not name one, ask.
- The daemon endpoint on `28888` is expected to stay stable across tasks.
- A first request to `/mcp` may lazily start the worker; this is normal.
- If `get_server_status` reports `starting`, `keepalive_running`, or `mirroring`, do not force a new browser session.
- Do not expect `occupied` as a canonical top-level server state. The current service reports states such as `idle`, `starting`, `keepalive_running`, `mirroring`, `active_sessions`, `isolated_runtime_active`, and `external_chromium_running`.
- If `get_server_status` reports `external_chromium_running`, treat it as a profile-scoped signal, not an automatic full-service outage.
- If `external_chromium_running` is reported, the practical meaning is usually that the configured Chromium binary root is already open outside MCP, so governance is blocking startup before the engine is even created.
- If the same profile is already occupied on a live-root session, prefer surfacing that state first. Reuse should be explicit, not implicit.
- If `can_start_profile_session(profile_name)` reports `allowed=false` for the same profile, do not try to force parallel reuse. Same-profile concurrency is intentionally blocked.
- For multi-tab tasks, do not assume a newly opened tab became the effective action target unless you explicitly activated it or the tool says it was activated.
- After `start_profile_session`, do not route subsequent tab or click actions to generic `mcp:playwright/*` tools. Use this service's own `browser_*` tools for tabs, clicks, typing, snapshot, diagnosis, and close.
- If an agent message says the session is open but also says it "cannot click because there is no tree node interface", that is almost certainly a tool-routing mistake rather than a real runtime capability loss. The correct fix is to keep using the `browserIdentity` session tools or restart with `engine=\"patchright\"` when snapshot/ref-style targeting is required.
- When diagnosing broken pages, prefer the MCP debug tools over manual screenshots of DevTools whenever possible.
- The managed runtime now exposes `session_health.recovery_actions` and `resolution_trace`; use them to decide whether to retry directly, refresh candidate search, or recreate the session.
- The managed runtime exposes `browser_get_action_trace` for per-session action timing and `get_mcp_tool_trace` for MCP worker timing. Use these before guessing why an interaction is slow.
- MCP tool traces are persisted as rotated JSONL; the GUI MCP status panel shows the active trace file path.
- The MCP server publishes standard tool annotations. Normal profile/session operations, navigation, tab operations, clicking, typing, key presses, mouse actions, screenshots, diagnostics, and cleanup are treated as trusted low-risk.
- `run_script` and `run_script_batch` are deliberately high-trust, non-read-only actions because they execute arbitrary JavaScript inside a real logged-in browser context.
- If the client supports trusted/read-only tool execution, use those annotations to avoid unnecessary approval prompts. Do not bypass profile occupancy, keepalive, mirror, or account-verification rules just to reduce approvals.
- `session_health.page_drift` is a first-class signal. When it reports `drifted=true`, prefer `reactivate_expected_tab`, `reopen_expected_url`, or `retry_on_sticky_tab` before assuming the selector itself is bad.
- `playwright_cli` console/network diagnostics are intentionally bounded and include noise categories. A partial diagnosis with `diagnostic_errors` is better than blocking the worker on a noisy site.
- For `playwright_cli`, simple selector click/fill actions prefer a fast DOM eval path and fall back to native CLI commands when needed. Untargeted candidate discovery is intentionally snapshot-backed while explicit selector reads use shorter target-scoped evals. Treat that split as the expected fast path, not as degraded behavior.
- For `playwright_cli`, `run_script` now prefers a safer serialization wrapper and generic text reads can fall back to bounded DOM chunking/page text when direct structured extraction comes back empty.
- For `playwright_cli`, that fallback improves robustness but not semantic quality. On complex dynamic frontends, structured extraction can still be noisier and weaker than `patchright`; choose the engine accordingly instead of expecting site-specific adapters in the core runtime.
- For `playwright_cli`, the runtime sanitizes upstream Chromium launch args so `AutomationControlled` is not injected through a real `--disable-blink-features` switch.
- For `playwright_cli`, gesture-style pages are a known engine-selection boundary. If the task requires `browser_mouse_move_xy`, `browser_mouse_click_xy`, `browser_mouse_drag_xy`, `browser_mouse_gesture_path`, pattern unlock, slider drag, or coordinate-based fallback, prefer starting the session with `engine="selenium_uc"` or `engine="patchright"` instead of assuming the default engine is sufficient.
- Use `get_session_capabilities(session_id)` when the task may need gesture actions. The capability surface now distinguishes generic coordinate support from formal `gesture_actions` support.
- `browser_mouse_gesture_path(session_id, points=[...])` is the preferred interface for gesture locks, slider tracks, and continuous path input. Use `patchright` first when the target page is frontend-heavy or needs stronger debug context; use `selenium_uc` when stealth is more important than diagnostics.
- managed post-action context and `browser_diagnose_page` now include an `anti_bot` block. Treat `anti_bot.detected=true` with strong markers or structured signals as a likely real challenge page; do not treat every page mentioning Cloudflare as a challenge automatically.
- Visible MCP browser sessions normally honor `mcp.start_minimized=true`, which should leave the browser in the taskbar instead of stealing foreground focus; users can click it open when they want to watch or take over.
- Do not enable `mcp.headless=true` just to reduce desktop interference. Headless mode should only be used when the user explicitly asks for headless/regression/background validation.
- `runtime_options.incognito=true` is supported when the task needs isolated validation without normal session carry-over, but it still uses the same profile governance and occupancy rules.
- When a `playwright_cli` session closes, the manager should release the named session and clean owned daemon/browser processes; startup also prunes stale temp dirs that are not referenced by live processes. If a browser window remains, treat it as an orphan-process bug and inspect the runtime root/session name.
- When the work is complete, always call `close_profile_session`.
- If the MCP server is unreachable, the likely operational cause is that the GUI or daemon is not currently running, not that the profile disappeared.
- `patchright` is now the default engine for the per-profile live runtime. It still obeys the same profile-occupancy rules as the other engines.

## Example User Wording

If the user wants another task to use this MCP reliably, wording like the following works well:

- `Use the browserIdentity MCP for this task. If I did not specify profile_name, ask me which profile to use first. Check status before starting, and close the session when done.`
- `Use the browserIdentity MCP with profile_name="Profile 4". Check availability first, do the work, and release the session at the end.`

## Ask the User When

- the user did not specify which identity-bearing browser context to use
- multiple possible identities exist
- the service is busy and reuse would have non-obvious consequences
- the project uses a non-obvious identity field and you cannot confidently infer the correct one from local context
