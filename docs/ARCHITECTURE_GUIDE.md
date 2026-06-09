# MCP Chromium Advanced Architecture Guide

## Goal

MCP Chromium Advanced is designed for browser automation workflows that must reuse a real logged-in Chromium identity instead of creating a fresh disposable browser every time.

The key idea is:

- one `Profile N` represents one persistent browser identity
- the GUI manages those identities and shared configuration
- the MCP layer claims, uses, and releases those identities safely for AI tasks

## Core Principles

- Reuse real Chromium profile state: cookies, local storage, extensions, bookmarks, and site permissions
- Keep identity selection explicit: callers should provide `profile_name` instead of relying on hidden defaults
- Prevent collisions: only one live-root owner should control a profile session at a time, while snapshot-backed isolated clones may run in parallel when mirror concurrency is enabled
- Share one source of truth: GUI and MCP services read the same config and profile metadata
- Start heavy browser automation lazily: keep the daemon stable, start the worker only when needed

## Runtime Layers

### GUI Layer

Main file:

- `chromium_advanced/chromium_manage_gui.py`

Responsibilities:

- edit browser paths and runtime settings
- manage profile entries
- start profiles manually
- run keepalive jobs
- show status, logs, and MCP state

### Shared Core Layer

Main file:

- `chromium_advanced/chromium_profile_lib.py`

Responsibilities:

- default config generation and normalization
- workspace and path resolution
- profile directory management
- bookmark template initialization
- Chromium launch logic
- keepalive helpers and shared browser automation utilities
- mirror snapshot generation and metadata persistence after keepalive

This module is the shared core used by both the GUI and the MCP side.

### Browser Engine Layer

Main directory:

- `chromium_advanced/browser_engines/`
- `chromium_advanced/browser_session_kernel.py`

Responsibilities:

- define a shared browser session interface for MCP operations
- provide `selenium_uc`, `patchright`, and `playwright_cli` runtime implementations
- keep profile/session ownership outside the engine layer
- allow the GUI and MCP worker to select an execution backend without changing profile creation logic
- keep keepalive separate for now so engine migration can happen incrementally
- expose explicit tab lifecycle operations instead of relying on hidden browser focus
- expose structured debug telemetry such as console messages, page errors, and network request summaries
- expose runtime capability metadata separately from raw engine names
- keep a managed action kernel between MCP tools and raw runtime sessions
- preserve actionable snapshot refs even when the underlying runtime does not have native ref semantics
- support deep DOM and open shadow-root traversal in the managed fallback path

Engine-specific note:

- `patchright` collects debug telemetry through per-tab CDP sessions, which makes console output, uncaught exceptions, and failed requests much closer to what a human would inspect in DevTools
- `selenium_uc` collects similar signals from Chromium browser/performance logs when available, so the API surface is shared but the fidelity is lower
- `playwright_cli` is treated as a fast runtime with lower native inspection fidelity, then lifted by managed fallbacks for generic DOM inspection, waiting, and snapshot-ref style targeting

### MCP Service Layer

Main files:

- `chromium_advanced/mcp_daemon.py`
- `chromium_advanced/mcp_server.py`
- `chromium_advanced/session_manager.py`

Responsibilities:

- expose profile-aware browser control over MCP
- report service and profile occupancy
- claim and release sessions
- prevent unsafe concurrent use
- allow controlled snapshot-backed concurrency when `app.concurrency_mode=mirror_isolated`
- keep the daemon stable while allowing the worker to start on demand
- route session creation through the selected browser engine
- wrap raw runtime sessions through a managed session kernel before exposing them to MCP tools
- keep busy-state governance ahead of engine startup so live-root access cannot be bypassed by switching runtimes
- distinguish managed worker reclaim from unexpected worker exit in daemon status reporting
- start visible MCP-owned browser sessions minimized by default when `mcp.start_minimized=true`, avoiding foreground focus theft while preserving a taskbar window for manual observation or user takeover
- keep `mcp.headless=false` as the normal MCP browsing default; `mcp.headless=true` is reserved for explicit user-requested regression or background validation
- materialize extracted runtime clones under `paths.mirror_user_data_root` when starting from a validated mirror snapshot

## Managed Action Kernel

Main file:

- `chromium_advanced/browser_session_kernel.py`

Responsibilities:

- normalize runtime capability output into a structured capability contract
- normalize action failures into stable product-level error codes
- attach consistent action metadata regardless of runtime
- provide generic DOM-script fallbacks for runtimes that do not natively implement some higher-level tools
- preserve the external MCP tool surface while reducing engine-specific `NotImplementedError` leakage
- cache fallback candidates as executable handles, not only plain CSS selectors, so later target actions can still resolve on complex pages
- use deep selector replay for open shadow-root targets when plain CSS cannot safely cross runtime boundaries
- synthesize normalized `post_action_context` for non-diagnostic runtimes so common action results remain structurally consistent across engines
- rank fallback candidates by relevance instead of scan order so complex-page target selection is less dependent on exploratory retries
- compress oversized HTML reads into managed previews plus summaries so high-noise pages do not flood downstream tool context
- maintain a managed recent-action trace so page and target diagnostics can include the causal steps that led to the current state
- enrich `diagnose_page` and `diagnose_target` with engine-agnostic managed metadata instead of only forwarding raw backend payloads
- expose a normalized `session_health` snapshot so action contexts and diagnostics can surface liveness, recent failure pressure, and recovery hints without backend-specific interpretation
- prioritize transient UI controls such as menu items, popup options, combobox/listbox entries, and overlay-backed actions when managed fallback candidate scoring is used on complex frontends
- run a unified target-resolution pipeline for `list_candidates`, `describe_target`, `wait_for`, and `diagnose_target` so scoring, scope hints, and fallback stage reporting stay consistent across tools
- expose `resolution_trace` metadata on managed fallback reads so callers can tell whether the hit came from direct selector resolution, ranked DOM fallback, cached snapshot refs, or snapshot-text scanning
- enrich fallback DOM extraction with `accessible_name`, `text_preview`, `control_type`, placeholder/title fields, ancestry tags, and overlay/dialog signals so complex component trees do not have to be reduced to raw page text
- keep `playwright_cli` on short target-scoped eval paths for explicit selectors, while using snapshot-backed enumeration for untargeted candidate scans to avoid oversized CLI eval payloads
- classify failure pressure into recovery-oriented buckets such as `target_resolution`, `page_synchronization`, `capability_gap`, and `session_lost` so diagnostics can recommend the next action instead of only reporting raw errors
- detect tab/page drift separately from generic runtime failures so diagnostics can recommend `reactivate_expected_tab`, `reopen_expected_url`, or `retry_on_sticky_tab` without site-specific logic

### Packaging Layer

Main files:

- `run_gui.py`
- `build_chromium_manage_gui_exe.ps1`

Responsibilities:

- provide the single public entry point for source usage
- build the GUI executable and internal MCP helper executables for Windows packaging
- keep desktop-delivered GUI, daemon, and worker artifacts aligned with the same managed runtime contract used in source mode

Packaging note:

- the desktop GUI is built with PyInstaller `--onefile`, so seeing a short-lived parent `ChromiumProfileManager.exe` process plus the real child GUI process is expected bootstrap behavior, not a duplicate second instance of the UI

## Session Model

The intended MCP lifecycle is:

1. list or inspect available profiles
2. check daemon and profile availability
3. start a profile session
4. perform browser work
5. release the session

This keeps real browser identities usable across many tasks without letting multiple tasks silently fight over the same logged-in state.

When `app.concurrency_mode=mirror_isolated`, the same lifecycle still applies, but the claimed session may run from an extracted runtime clone rather than from the live profile root. In that mode:

- live-root ownership is still exclusive
- keepalive and mirror refresh remain exclusive
- same-profile parallelism is implemented by independent snapshot clones, not by sharing one on-disk profile directory across runtimes

For multi-tab tasks, the intended page lifecycle inside one session is:

1. list tabs or open a new tab
2. activate the target tab explicitly
3. perform page actions on that active tab
4. switch again when needed
5. close the tab or release the whole session

For blocked or unstable pages, the intended diagnostic flow is:

1. capture the current interaction context
2. inspect recent console messages
3. inspect recent page errors
4. inspect recent failed or bad network requests
5. inspect `session_health.recovery_hint` to decide whether to retry, refresh candidates, or recreate the session
6. inspect `session_health.recovery_actions` and `failure_classification` to decide whether the next step is a scoped retry, page resync, or full session recreation
7. inspect `resolution_trace` on target-oriented reads to see whether the runtime hit directly, fell back to ranked DOM search, or had to rely on snapshot text
8. use the bundled page diagnosis payload before falling back to screenshots
9. if `session_health.failure_classification == "page_drift"`, prefer recovering the expected tab/page before changing selectors or recreating the full session

## Why Not a Generic Browser Sandbox

This project is intentionally not optimized for disposable generic browser automation. It is optimized for:

- persistent identities
- real login state
- human-managed browser profiles
- long-lived local automation environments

That design choice is what makes it useful for AI workflows that need access to real accounts in a controlled and reusable way.

## Suggested Future Cleanup

- split large GUI sections into smaller modules if the UI keeps growing
- keep expanding i18n coverage so all user-facing strings come from resource files
- add automated smoke tests for GUI startup, config loading, and MCP daemon lifecycle
- document cross-platform packaging separately from source-level compatibility
- consider eventually making `tab_id` a first-class optional parameter across every browser action, not just tab management and read/diagnostic helpers
