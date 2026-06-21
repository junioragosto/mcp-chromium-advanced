# Runtime State Consistency

## Goal

This document defines the single-source-of-truth model for profile runtime state across:

- core/session manager
- daemon control API
- GUI
- external/manual Chromium detection

The objective is to keep profile state deterministic under concurrent MCP, manual launch, keepalive, and script-driven usage.

## State Sources

Profile runtime state is assembled from four sources, in priority order:

1. active in-memory MCP sessions
2. shared occupancy registry entries
3. profile runtime lock presence
4. external Chromium process detection

If none of the above are present, the profile is `idle`.

## Resolution Rules

Core resolves profile runtime state through `SessionManager._resolve_profile_runtime_state(...)`.

Returned fields:

- `busy_state`
- `occupancy`
- `occupancy_state`
- `occupancy_scene_type`
- `occupancy_owner_label`
- `profile_lock_active`
- `external_process_count`

Priority:

1. `active_sessions`
2. `occupancy scene/state`
3. `profile_lock_active`
4. `external_chromium_running`
5. `idle`

This same model must be reused by:

- `list_profiles`
- `get_profile_status_with_options`
- `can_start_session`
- control API profile payloads

## GUI Responsibility

GUI is no longer allowed to assemble primary truth from local heuristics.

GUI responsibilities:

- read control API profile snapshots
- render status labels/tooltips
- subscribe to occupancy events
- keep a local fallback occupancy cache only for temporary degradation

GUI must not overwrite fallback occupancy cache with control payload data.
GUI should actively prune fallback cache entries when:

- control profile snapshots confirm the profile list and the fallback entry is no longer valid
- fallback state is `released`, `idle`, or `start_failed`
- a fallback `manual` occupancy entry no longer has a matching external primary Chromium process

## Event-First Refresh

The occupancy events timer now acts as the primary refresh trigger.

Behavior:

- poll recent control events
- de-duplicate events by stable key
- on new event:
  - invalidate cached control profile snapshot
  - force refresh control profile snapshot
  - request debounced UI refresh

Polling remains as a fallback, not the primary state propagation path.

## External Chromium Detection

External Chromium detection is profile-scoped, not service-global.

Detection output now distinguishes:

- primary browser-owning processes
- auxiliary/noise child processes

Examples of noise-only roles:

- `renderer`
- `gpu`
- `utility`
- `utility_network`
- `utility_storage`
- `utility_audio`
- `utility_model`
- `crashpad`

Profile occupancy and start gating should only use primary processes.
Auxiliary processes may still be kept for diagnostics, but they must not independently mark a profile as busy.

## Known Fallbacks

Fallback occupancy cache still exists for short-lived failure cases such as:

- control API temporarily unavailable
- transient lock/permission issues reading occupancy registry

Fallback is display-only and must never become the authoritative start-governance source.
Fallback should also be treated as self-healing cache, not durable state.

## Validation Requirements

Any runtime-state change should be validated against:

1. unit tests for state resolution and process classification
2. daemon/control status verification
3. installed runtime verification in the packaged app

Key scenarios:

- manual launch -> GUI changes to manual/external quickly
- close manual launch -> GUI returns to idle quickly
- MCP session start -> profile becomes busy without conflicting local fallback state
- noise-only Chromium child processes do not block session startup
