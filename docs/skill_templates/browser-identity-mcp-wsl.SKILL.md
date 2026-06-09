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

## Workflow

1. Confirm that the browser MCP service is actually running on Windows.
2. Determine the Windows-side host IP visible from WSL.
3. Test connectivity from WSL to the Windows MCP endpoint.
4. Use the reachable WSL-side URL for MCP calls.
5. Before starting a browser session, still check occupancy and confirm the browser profile identity parameter with the user if it is not specified.
6. For multi-tab or debug-heavy tasks, use the same explicit tab activation and structured debug tools as the normal browser identity MCP workflow.

For the Chromium Profile Manager service on this machine, the supported engine values remain:

- `selenium_uc`
- `patchright`
- `playwright_cli`

Recommended engine-selection policy remains the same from WSL:

- default to `playwright_cli` for ordinary MCP task execution
- use `selenium_uc` for stealth-sensitive or higher anti-detection workflows
- use `patchright` for richer structured diagnostics and complex frontend inspection

Changing the GUI default engine still affects only future sessions. Existing sessions keep their original engine, and same-profile multi-engine starts in `mirror_isolated` mode create separate isolated runtimes rather than mutating a live session in place.

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

## Connectivity Check

From WSL, use a short timeout test before doing real MCP work:

```bash
curl --max-time 5 -I http://<windows-host-ip>:28888/mcp -H 'Accept: application/json, text/event-stream'
```

HTTP error responses such as `400` or `405` can still mean the service is reachable; the key distinction is whether the TCP connection succeeds.

## Required Behavior

- Do not assume WSL `127.0.0.1` reaches the Windows service.
- Verify that the Windows service is listening on a WSL-reachable host such as `0.0.0.0` or a specific LAN address.
- If WSL cannot reach the service, check Windows firewall rules and listening host configuration before debugging MCP semantics.
- After connectivity is confirmed, follow the same identity confirmation and occupancy rules as the normal browser identity MCP workflow.
- After connectivity is confirmed, do not infer a target website account from the GUI profile account label; verify the actual site login inside the target website when account correctness matters.
- After connectivity is confirmed, prefer MCP debug tools such as `browser_get_console_messages`, `browser_get_page_errors`, `browser_get_network_requests`, `browser_diagnose_page`, `browser_get_action_trace`, and `get_mcp_tool_trace` over screenshot-only diagnosis.
- After connectivity is confirmed, prefer `session_health.recovery_actions`, `session_health.page_drift`, and `resolution_trace` over ad-hoc retries when a dynamic page fails under WSL.
- Treat partial `playwright_cli` diagnostics with `diagnostic_errors` as useful signal. The runtime intentionally bounds heavy console/network calls, classifies common noise, and avoids long MCP worker stalls.
- `playwright_cli` simple selector click/fill actions may use the fast DOM eval path before native CLI fallback. This is expected and should be treated as the high-performance path.
- If the Windows side reports `external_chromium_running`, do not assume it is always a hard block. In `mirror_isolated` mode, first inspect whether `accepting_new_sessions=true` and whether the preflight allows a snapshot-backed start.
