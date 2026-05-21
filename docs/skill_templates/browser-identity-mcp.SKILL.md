---
name: browser-identity-mcp
description: Use when a task needs to control a browser through an MCP service that exposes real logged-in identities such as profiles, accounts, personas, browser slots, or user spaces. Apply this skill across projects whenever Codex must choose or confirm an identity-bearing browser context, check service occupancy before starting work, avoid conflicting sessions, and release the session when finished.
---

# Browser Identity MCP

## Overview

Treat browser identity as a scarce shared resource. In some projects the identity parameter may be named `profile_name`, but in others it may be `account`, `persona`, `slot`, `browser_id`, or something similar.

Always optimize for safety over convenience: if the user did not specify which identity to use, ask before taking control of a real logged-in browser context.

For the Chromium Profile Manager service used on this machine:

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
- Never start a new session blindly; check occupancy first.
- If the service reports a busy state, surface that clearly to the user.
- If the same identity is already occupied, do not steal it unless the project explicitly supports safe reuse and the user intends that reuse.
- Always close or release the session after the task completes unless the user explicitly asks to keep it open.

## Parameter Mapping

Translate the project-specific field into this mental model:

- identity parameter:
  the field that selects the real browser identity, such as `profile_name`, `account`, `persona`, or `slot`
- state query:
  the API or tool that reports whether the browser service is idle, starting, occupied, or externally busy
- session start:
  the API or tool that claims the identity and returns a session handle
- session release:
  the API or tool that closes the claimed session and frees the identity

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

If `can_start_profile_session(profile_name)` reports `allowed=false` but also reports the same identity as reusable, treat that as "do not start a second session automatically." Only use `reuse_existing=true` when the current project explicitly supports it and the user intends that reuse.

If the project needs a specific browser backend, pass the optional `engine` parameter explicitly when checking or starting a session, for example `engine="selenium_uc"` or `engine="patchright"`. If omitted, the GUI-configured default engine will be used.

## Chromium-Specific Notes

- Do not guess `profile_name`. If the user did not name one, ask.
- The daemon endpoint on `28888` is expected to stay stable across tasks.
- A first request to `/mcp` may lazily start the worker; this is normal.
- If `get_server_status` reports `occupied`, `starting`, `keepalive_running`, or `external_chromium_running`, do not force a new browser session.
- If the same profile is already occupied, prefer surfacing that state first. Reuse should be explicit, not implicit.
- For multi-tab tasks, do not assume a newly opened tab became the effective action target unless you explicitly activated it or the tool says it was activated.
- When diagnosing broken pages, prefer the MCP debug tools over manual screenshots of DevTools whenever possible.
- When the work is complete, always call `close_profile_session`.
- If the MCP server is unreachable, the likely operational cause is that the GUI or daemon is not currently running, not that the profile disappeared.

## Example User Wording

If the user wants another task to use this MCP reliably, wording like the following works well:

- `Use the browserIdentity MCP for this task. If I did not specify profile_name, ask me which profile to use first. Check status before starting, and close the session when done.`
- `Use the browserIdentity MCP with profile_name="Profile 4". Check availability first, do the work, and release the session at the end.`

## Ask the User When

- the user did not specify which identity-bearing browser context to use
- multiple possible identities exist
- the service is busy and reuse would have non-obvious consequences
- the project uses a non-obvious identity field and you cannot confidently infer the correct one from local context
