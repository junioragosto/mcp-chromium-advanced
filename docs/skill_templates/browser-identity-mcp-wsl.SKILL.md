---
name: browser-identity-mcp-wsl
description: Use when a task needs to access a Windows-hosted browser MCP service from WSL. Apply this skill when Codex must discover the reachable Windows-side MCP address from WSL, verify connectivity, use the WSL-reachable host instead of assuming localhost, and still follow safe identity selection and occupancy checks before starting browser work.
---

# Browser Identity MCP WSL

## Overview

When a browser MCP service runs on Windows and the caller runs inside WSL, do not assume `127.0.0.1` works. WSL often needs the Windows host IP instead.

Use this skill only for the WSL-to-Windows access layer. The normal identity and occupancy rules from the main browser identity MCP workflow still apply.

## Workflow

1. Confirm that the browser MCP service is actually running on Windows.
2. Determine the Windows-side host IP visible from WSL.
3. Test connectivity from WSL to the Windows MCP endpoint.
4. Use the reachable WSL-side URL for MCP calls.
5. Before starting a browser session, still check occupancy and confirm the identity parameter with the user if it is not specified.
6. For multi-tab or debug-heavy tasks, use the same explicit tab activation and structured debug tools as the normal browser identity MCP workflow.

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
- After connectivity is confirmed, prefer MCP debug tools such as `browser_get_console_messages`, `browser_get_page_errors`, `browser_get_network_requests`, and `browser_diagnose_page` over screenshot-only diagnosis.
