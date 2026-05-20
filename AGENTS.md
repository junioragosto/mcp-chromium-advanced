# Chromium Advanced Agent Notes

This project exposes a GUI-managed MCP service for real Chromium profiles with persistent login state.

## Default Workflow

1. Ensure the GUI-managed MCP service is running before attempting MCP calls.
2. Query server state first:
   - `get_server_status`
   - `can_start_profile_session(profile_name)`
3. Only start a session when the service reports it is available.
4. Always release the session with `close_profile_session(session_id)` when finished.

## Profile Selection Rule

- If the user explicitly provides a `profile_name`, use that profile.
- If the user does not specify a profile, the agent must ask the user which profile to use before starting a session.
- Do not guess a profile automatically for browser actions that use real login state.

## Busy-State Rule

- If GUI-launched Chromium is already running, MCP browser session startup should be treated as unavailable.
- If keepalive is running, MCP browser session startup should be treated as unavailable.
- If another MCP session already occupies the browser service, new sessions should be rejected unless the same session is explicitly being reused.

## WSL Access

- Windows-hosted MCP may be reachable from WSL via the Windows-side host IP rather than WSL `127.0.0.1`.
- Verify the configured host/port before attempting WSL access.

## Reusable Prompt Pattern

Use wording like this in other tasks when you want the agent to use the real-login browser MCP:

`Use the browserIdentity MCP for this task. If I did not specify profile_name, ask me which profile to use first. Before starting, check server/profile status. If occupied, tell me instead of forcing reuse. When done, close the profile session.`

If the profile is already known, use wording like this:

`Use the browserIdentity MCP with profile_name="Profile 4". Check availability first, then do the browser task, then close the session when finished.`
