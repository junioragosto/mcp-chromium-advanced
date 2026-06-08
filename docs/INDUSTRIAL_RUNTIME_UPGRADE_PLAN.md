# Industrial Runtime Upgrade Plan

## Goal

Upgrade MCP Chromium Advanced from a multi-engine browser controller into a more industrial browser automation runtime while keeping the external MCP tool surface stable.

The target path is:

`GUI / MCP tools -> SessionManager -> ManagedBrowserSession / Action Kernel -> BrowserEngine runtime -> Chromium backend`

## Scope

This upgrade stays generic and open source friendly.

- No site-specific adapters
- No tool renames
- No behavior tied to a single backend

## Required Outcomes

### 1. Unified runtime capability model

- expose a structured capability payload instead of only flat legacy booleans
- classify runtimes by execution profile, not only by engine name
- keep legacy fields for compatibility

### 2. Unified action execution kernel

- place a managed session wrapper between MCP tools and raw engine sessions
- normalize success and failure payloads
- attach consistent action metadata
- keep current MCP method names intact

### 3. Generic fallback layer

- avoid surfacing raw `NotImplementedError` to the caller for common read/action tools
- provide runtime-agnostic fallbacks using DOM script evaluation where possible
- support snapshot-ref style workflows even on runtimes without native snapshot refs

### 4. Error normalization

- classify failures with stable product-level error codes
- distinguish runtime unsupported, target missing, target not interactable, timeout, and generic runtime failures
- preserve raw engine details in the payload

### 5. Verification

- automated tests for capability negotiation
- automated tests for fallback behavior
- automated tests for snapshot-ref translation
- automated tests for deep DOM and open shadow-root ref replay
- automated tests for normalized runtime failures

## Implementation Plan

### Phase A: Kernel and runtime model

- add `ManagedBrowserSession`
- add `RuntimeCapabilities`
- wrap every raw engine session through the managed session in `SessionManager`

Status: completed

### Phase B: Generic fallback coverage

- `snapshot`
- `list_candidates`
- `inspect_elements`
- `wait_for`
- `describe_target`
- `verify_target_visible`
- `verify_target_value`
- `verify_active_element`
- `diagnose_target`
- snapshot-ref translation for `click_target`, `type_target`, `type_target_and_verify`

Status: completed

### Phase C: Documentation and verification

- document the new runtime layering
- add regression tests
- validate compile + unit tests
- validate built daemon/worker executable behavior
- validate desktop executable replacement and startup

Status: completed

## Verification Matrix

### Capability model

- `ManagedBrowserSession.get_capabilities()` returns `capability_version=2`
- runtime profile is exposed as `fast`, `diagnostic`, or `compatible`

Status: verified by unit test

### Fallback candidate enumeration

- unsupported `list_candidates` no longer has to surface a tool error
- managed session returns DOM-derived candidates with refs

Status: verified by unit test

### Snapshot ref translation

- fallback snapshot creates refs
- runtimes without native snapshot refs can still resolve them for target actions

Status: verified by unit test

### Wait semantics

- unsupported `wait_for` falls back to polling DOM state

Status: verified by unit test

### Error normalization

- runtime exception returns stable `error_code`

Status: verified by unit test

### Complex frontend fallback path

- fallback candidate enumeration now preserves deep selectors for elements inside open shadow roots
- managed target actions can replay cached deep refs even when the raw runtime only understands plain selectors or native snapshot refs
- `playwright_cli` wait polling no longer depends on `null` eval payloads, avoiding unstable behavior in transient DOM states

Status: verified by unit test and local runtime integration test

### Managed post-action context

- managed runtime now upgrades `supports_post_action_context` when a raw runtime exposes interaction-context primitives even if it does not natively attach post-action payloads
- common action results and normalized failures now carry a consistent `post_action_context` shape across `playwright_cli`, `selenium_uc`, and `patchright`
- the managed layer rewrites raw `inspect` interaction-context payloads to the actual action name so callers do not need engine-specific interpretation rules

Status: verified by unit test and local runtime integration test

### Token and targeting efficiency

- managed `get_page_html` now emits an HTML summary and truncates oversized payloads into bounded previews instead of returning unbounded raw markup by default
- fallback candidate enumeration now ranks matches by visibility, interactivity, and text affinity instead of using DOM scan order
- these changes specifically reduce diagnostic noise and improve first-hit behavior on complex frontends without introducing site-specific adapters
- automated MCP/runtime validation can now run with `mcp.headless=true`, reducing desktop interference during local regression work

Status: verified by unit test and local runtime integration test

### Managed diagnostics and action trace

- managed runtime now records a bounded recent-action trace with timestamps, target hints, fallback usage, and normalized failure codes
- `diagnose_page` and `diagnose_target` now include managed diagnostic metadata plus recent action history instead of relying only on raw engine-specific payloads
- diagnostic payloads exclude the current diagnose call from the history view so the returned trace stays focused on the causal user/runtime actions
- interaction contexts and diagnosis payloads now include a normalized `session_health` snapshot with liveness, recent failure counts, and generic recovery hints
- fallback candidate ranking now boosts transient UI controls such as popup/menu/listbox actions so overlay-heavy frontends are less dependent on exploratory retries

Status: verified by unit test and local runtime integration test

### Built runtime path

- packaged daemon and worker can lazy-start, create a managed browser session, run browser actions, and reclaim the worker after idle timeout

Status: verified against built executables

### Desktop delivery path

- desktop `ChromiumProfileManager.exe` can be replaced with the rebuilt artifact
- rebuilt desktop daemon responds on the configured MCP endpoint
- real busy-state governance still blocks unsafe startup when the configured Chromium root is already running

Status: verified on the Windows desktop delivery path
