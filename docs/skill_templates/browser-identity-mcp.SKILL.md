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
- daemon model:
  a stable daemon listens on `28888`
- worker model:
  a browser-capable MCP worker is lazily started on demand and reclaimed after idle timeout
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

## Required Behavior

- Never guess a real-login identity automatically.
- Never infer the target website account from the GUI profile `Account` label.
- Never start a new session blindly; check occupancy first.
- If the service reports a busy state, surface that clearly to the user.
- If the same identity is already occupied, do not steal it unless the project explicitly supports safe reuse and the user intends that reuse.
- When the task depends on a specific website login, verify that website's actual logged-in account inside the page before doing account-sensitive work.
- Always close or release the session after the task completes unless the user explicitly asks to keep it open.

## Parameter Mapping

Translate the project-specific field into this mental model:

- identity parameter:
  the field that selects the real browser profile/container, such as `profile_name`, `persona`, or `slot`
- state query:
  the API or tool that reports whether the browser service is idle, starting, occupied, or externally busy
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

- default to `playwright_cli` for ordinary MCP browsing tasks
- switch to `selenium_uc` when stealth or anti-detection tolerance matters more than raw speed
- switch to `patchright` when the task needs the richest structured diagnostics, snapshot/ref behavior, or deeper complex-frontend inspection

Important engine-switching boundary:

- changing the GUI default engine affects only future sessions
- an already running session keeps the engine it started with
- `reuse_existing=true` should only be used when the caller wants the same profile and the same engine session
- in `mirror_isolated` mode, starting the same profile with a different engine creates a separate isolated runtime rather than hot-switching the existing session

## Chromium-Specific Notes

- `browser-identity-mcp` is the skill name, not the MCP server id.
- The MCP server id registered in Codex is `browserIdentity`.
- Do not guess `profile_name`. If the user did not name one, ask.
- The daemon endpoint on `28888` is expected to stay stable across tasks.
- A first request to `/mcp` may lazily start the worker; this is normal.
- If `get_server_status` reports `starting`, `keepalive_running`, or `mirroring`, do not force a new browser session.
- If `get_server_status` reports `occupied`, treat that as the live-root path being occupied. Do not steal it.
- If `get_server_status` reports `external_chromium_running`, check whether the service also reports `accepting_new_sessions=true`; in `mirror_isolated` mode that can still be a valid snapshot-backed start path.
- If `external_chromium_running` is reported, the practical meaning is usually that the configured Chromium binary root is already open outside MCP, so governance is blocking startup before the engine is even created.
- If the same profile is already occupied on a live-root session, prefer surfacing that state first. Reuse should be explicit, not implicit.
- If `can_start_profile_session(profile_name)` reports `same_profile_parallel_supported=true` and `start_mode="mirror_isolated"`, that means the service supports same-profile parallel work by extracting isolated runtime clones from a mirror snapshot. Treat that as safe parallelism, not as live profile sharing.
- For multi-tab tasks, do not assume a newly opened tab became the effective action target unless you explicitly activated it or the tool says it was activated.
- When diagnosing broken pages, prefer the MCP debug tools over manual screenshots of DevTools whenever possible.
- The managed runtime now exposes `session_health.recovery_actions` and `resolution_trace`; use them to decide whether to retry directly, refresh candidate search, or recreate the session.
- The managed runtime exposes `browser_get_action_trace` for per-session action timing and `get_mcp_tool_trace` for MCP worker timing. Use these before guessing why an interaction is slow.
- MCP tool traces are persisted as rotated JSONL; the GUI MCP status panel shows the active trace file path.
- The MCP server publishes standard tool annotations. Normal profile/session operations, navigation, tab operations, clicking, typing, key presses, mouse actions, screenshots, diagnostics, and cleanup are treated as trusted low-risk; arbitrary JavaScript remains non-read-only.
- If the client supports trusted/read-only tool execution, use those annotations to avoid unnecessary approval prompts. Do not bypass profile occupancy, keepalive, mirror, or account-verification rules just to reduce approvals.
- `session_health.page_drift` is a first-class signal. When it reports `drifted=true`, prefer `reactivate_expected_tab`, `reopen_expected_url`, or `retry_on_sticky_tab` before assuming the selector itself is bad.
- `playwright_cli` console/network diagnostics are intentionally bounded and include noise categories. A partial diagnosis with `diagnostic_errors` is better than blocking the worker on a noisy site.
- For `playwright_cli`, simple selector click/fill actions prefer a fast DOM eval path and fall back to native CLI commands when needed. Untargeted candidate discovery is intentionally snapshot-backed while explicit selector reads use shorter target-scoped evals. Treat that split as the expected fast path, not as degraded behavior.
- For `playwright_cli`, the runtime sanitizes upstream Chromium launch args so `AutomationControlled` is not injected through a real `--disable-blink-features` switch.
- Visible MCP browser sessions normally honor `mcp.start_minimized=true`, which should leave the browser in the taskbar instead of stealing foreground focus; users can click it open when they want to watch or take over.
- Do not enable `mcp.headless=true` just to reduce desktop interference. Headless mode should only be used when the user explicitly asks for headless/regression/background validation.
- When a `playwright_cli` session closes, the manager should release the named session and clean owned daemon/browser processes; startup also prunes stale temp dirs that are not referenced by live processes. If a browser window remains, treat it as an orphan-process bug and inspect the runtime root/session name.
- When the work is complete, always call `close_profile_session`.
- If the MCP server is unreachable, the likely operational cause is that the GUI or daemon is not currently running, not that the profile disappeared.
- `playwright_cli` is a supported parallel engine. In `mirror_isolated` mode it still obeys the same governance rules, but its runtime may be launched from an extracted snapshot clone instead of the live profile root.

## Example User Wording

If the user wants another task to use this MCP reliably, wording like the following works well:

- `Use the browserIdentity MCP for this task. If I did not specify profile_name, ask me which profile to use first. Check status before starting, and close the session when done.`
- `Use the browserIdentity MCP with profile_name="Profile 4". Check availability first, do the work, and release the session at the end.`

## Ask the User When

- the user did not specify which identity-bearing browser context to use
- multiple possible identities exist
- the service is busy and reuse would have non-obvious consequences
- the project uses a non-obvious identity field and you cannot confidently infer the correct one from local context
