# Daemon Automation Integration

This document describes how to integrate fixed local automation scripts with the
Chromium Advanced daemon without going through MCP tool calls.

## Purpose

Use this path when the caller is a normal local script or service and needs:

- real Chromium profile login state
- the same profile occupancy and lock governance as MCP
- explicit acquire / heartbeat / release lifecycle control
- bounded browser actions instead of arbitrary remote code execution

The integration path is:

```text
fixed script -> daemon HTTP API -> SessionManager -> BrowserEngine -> real Chromium profile
```

## Scope

This integration surface is intended for fixed-script automation, not for
uploading arbitrary executable logic to the daemon.

The daemon accepts:

- profile acquisition
- bounded browser actions
- lease heartbeat refresh
- explicit release
- profile reclaim

It does not accept:

- arbitrary Python execution
- arbitrary shell execution
- arbitrary remote code upload

## Authentication

Every daemon request must send the configured API token:

```http
Authorization: Bearer <token>
```

If the token is missing or wrong, the daemon returns `401`.

## Core Endpoints

### 1. Acquire

`POST /_daemon/automation/acquire`

Example body:

```json
{
  "profile_name": "Profile 4",
  "engine": "selenium_uc",
  "owner_label": "gmail_batch_job",
  "heartbeat_timeout_seconds": 180,
  "runtime_options": {
    "headless": false,
    "start_minimized": true,
    "mute_audio": true,
    "window_size": "1280,720",
    "extra_args": []
  }
}
```

Success response contains:

- `session_id`
- `profile_name`
- `engine_name`
- `runtime_mode`
- `runtime_root`
- `reused`

Typical failures:

- `400`: bad request body
- `404`: profile not found
- `409`: profile already occupied

### 2. Action

`POST /_daemon/automation/action`

Example body:

```json
{
  "session_id": "session-xxxx",
  "owner_label": "gmail_batch_job",
  "action": "navigate",
  "args": {
    "url": "https://mail.google.com/",
    "wait_for_ready": true,
    "timeout_seconds": 30
  }
}
```

Currently supported action names:

- `navigate`
- `get_current_url`
- `get_page_text`
- `get_page_html`
- `list_tabs`
- `open_tab`
- `activate_tab`
- `close_tab`
- `click`
- `type_text`
- `press_key`
- `run_script`
- `get_console_messages`
- `get_network_requests`
- `screenshot`
- `get_summary`
- `get_capabilities`
- `snapshot`

Typical failures:

- `400`: missing `session_id`, missing `action`, unsupported action
- `404`: session not found
- `409`: session no longer usable

### 3. Heartbeat

`POST /_daemon/automation/heartbeat`

Example body:

```json
{
  "session_id": "session-xxxx",
  "profile_name": "Profile 4",
  "engine_name": "selenium_uc",
  "owner_label": "gmail_batch_job",
  "owner_pid": 12345,
  "heartbeat_timeout_seconds": 180,
  "details": {
    "phase": "after_navigation"
  }
}
```

Use heartbeat for long-running scripts so the daemon can distinguish an active
owner from a stale one.

Typical failures:

- `404`: profile occupancy not found
- `400`: malformed request

### 4. Release

`POST /_daemon/automation/release`

Example body:

```json
{
  "session_id": "session-xxxx",
  "profile_name": "Profile 4"
}
```

At least one of `session_id` or `profile_name` is required.

Typical failures:

- `400`: missing identifiers

### 5. Profile Status And Recovery

Useful daemon endpoints around the automation flow:

- `GET /_daemon/status`
- `GET /_daemon/profiles`
- `GET /_daemon/profiles/{profile_name}`
- `POST /_daemon/profiles/{profile_name}/reclaim`
- `POST /_daemon/reap-expired`

Use them to inspect occupancy, recent events, and stale lock recovery state.

## Runtime Options

`runtime_options` is applied as a temporary launch override for the acquired
browser session.

Supported keys in the current implementation:

- `headless`
- `start_minimized`
- `mute_audio`
- `window_size`
- `extra_args`
- `heartbeat_timeout_seconds`

Important rule:

- `headless` should only be enabled when the caller explicitly needs headless.
- For user-observable desktop workflows, prefer `start_minimized=true` instead
  of stealing the foreground window.

## Error Contract

The daemon now exposes stable HTTP semantics for automation callers:

- `400`: caller request problem
- `401`: authentication failure
- `404`: missing session, profile, or occupancy
- `409`: occupancy conflict or incompatible reuse attempt
- `500`: unexpected internal failure

Callers should branch on HTTP status first, then inspect the returned `detail`.

## Validation Scripts

Reference validation scripts in this repository:

- `demo/validate_daemon_automation.py`
- `demo/validate_daemon_automation_failures.py`

Latest verified reports:

- `demo/output/managed_daemon_smoke_20260611_230345.json`
- `demo/output/managed_daemon_failures_20260611_230315.json`

Important validation rule:

- Both scripts default to the same isolated validation profile: `Profile 101`.
- Do not run them in parallel unless you override one script to use a different
  validation profile and split user-data root.

## Minimal Python Example

```python
import httpx

BASE_URL = "http://127.0.0.1:28888"
TOKEN = "replace-with-your-token"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

with httpx.Client(headers=headers, timeout=90.0) as client:
    acquire = client.post(
        f"{BASE_URL}/_daemon/automation/acquire",
        json={
            "profile_name": "Profile 4",
            "engine": "selenium_uc",
            "owner_label": "demo_script",
            "heartbeat_timeout_seconds": 180,
            "runtime_options": {
                "start_minimized": True,
                "mute_audio": True,
                "window_size": "1280,720",
            },
        },
    )
    acquire.raise_for_status()
    session_id = acquire.json()["session_id"]

    try:
        nav = client.post(
            f"{BASE_URL}/_daemon/automation/action",
            json={
                "session_id": session_id,
                "owner_label": "demo_script",
                "action": "navigate",
                "args": {"url": "https://example.com/", "wait_for_ready": True},
            },
        )
        nav.raise_for_status()
        print(nav.json())
    finally:
        release = client.post(
            f"{BASE_URL}/_daemon/automation/release",
            json={"session_id": session_id, "profile_name": "Profile 4"},
        )
        release.raise_for_status()
```

## Recommended Caller Policy

For production callers, use this order:

1. Read `/_daemon/status`
2. Read `/_daemon/profiles/{profile_name}`
3. Acquire one profile
4. Run bounded actions
5. Send heartbeat if the script is long-running
6. Release explicitly
7. Use reclaim only for stale-owner recovery, not as a normal close path
