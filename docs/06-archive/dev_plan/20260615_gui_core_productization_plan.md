# GUI / Core Productization Plan

## Goal

Refactor the current desktop app into a fully productized split architecture:

- `core`: runtime truth and governance
- `control daemon`: service/API facade over core
- `gui`: API client and operator console only

Target shape:

- GUI no longer owns runtime truth
- GUI no longer performs lock/governance decisions locally
- MCP-facing automation API and GUI-facing control API are explicitly separated
- logging is produced by core/daemon and consumed by GUI through API
- plugin catalog is globally managed, while plugin association remains profile-scoped
- future UI shell replacement does not require backend redesign

This plan is for a full long-task implementation later. It is not a partial cleanup plan.

## Product Principle

This refactor must be done as a clean split, not a half-split transition.

Acceptance baseline:

1. runtime truth exists only in `core/control daemon`
2. GUI no longer contains duplicated governance logic
3. MCP API and GUI control API are physically separated by route groups and auth domain
4. logs come from core/control API, not GUI-local buffers as truth
5. plugin catalog and profile-plugin associations are managed by core/control API, not GUI-local ad hoc state
6. verification output is recorded and checked against this plan item-by-item

Any implementation that leaves long-lived duplicated truth in both GUI and daemon is considered incomplete.

## Current Assessment

The current project already has useful backend primitives:

- `session_manager.py` already owns much of profile/session governance
- `occupancy_registry.py` already acts as shared runtime state storage
- `mcp_daemon.py` already exposes HTTP API and worker lifecycle controls

But the current GUI still mixes:

- view rendering
- timers/polling
- runtime interpretation
- keepalive orchestration
- occupancy writes/clears
- daemon/worker health assumptions
- action availability logic
- parts of state shaping
- parts of logging behavior

That mix is the main maturity bottleneck. The problem is not only UI appearance. The problem is that there is still more than one partial source of truth.

## Architecture Direction

Adopt a `clash-party` / `clash-core` style split:

- `core` is the only runtime truth source
- `control daemon` is the only process that exposes controllable APIs
- `gui` is a client of the control daemon
- `mcp` remains a separate automation-facing API surface

### Target Layers

#### 1. Core

Responsibility:

- profile runtime model
- occupancy model
- keepalive runtime model
- mirror/runtime selection rules
- plugin catalog truth
- profile-plugin association truth
- session lifecycle rules
- reclaim/reap rules
- external process detection
- log/event generation

Must not depend on:

- PyQt
- HTTP framework details
- GUI widgets
- MCP protocol specifics

#### 2. Control Daemon

Responsibility:

- host core
- expose local control API for GUI
- expose MCP/business API for automation clients
- expose event/log streams
- own worker/keepalive/service orchestration
- own config reload and persistence boundaries

Must provide:

- stable API contracts
- explicit auth domains
- process lifecycle ownership
- health/status/event endpoints
- log settings and retrieval endpoints
- plugin catalog and profile association endpoints

#### 3. GUI

Responsibility:

- API client
- configuration editor
- status/dashboard/log viewer
- operator actions
- tray/menu-bar integration
- local layout/window state persistence

Must not do:

- local occupancy truth assembly
- local keepalive truth assembly
- local worker health arbitration
- direct lock file truth decisions
- direct profile governance decisions
- GUI-local log truth ownership
- GUI-local plugin/profile association truth ownership

## Critical Design Rule

The key rule is not merely "split into two processes".

The key rule is:

`core/control daemon must become the only source of runtime truth.`

If GUI and daemon both continue to decide occupancy/keepalive/availability independently, the split will fail even if they run in separate processes.

## API Separation And Authentication

This is mandatory and must be designed up front.

The project currently treats token as security authentication, not as an authorization level model.

This plan preserves that principle.

### A. MCP Business API

Purpose:

- consumed by MCP clients, agents, external automation callers
- used for browser automation work

Authentication model:

- one dedicated MCP token only

Suggested config key:

- `mcp.api_token`

### B. GUI Control API

Purpose:

- consumed only by the desktop GUI
- used for service control, runtime inspection, config operations, logs/events, plugin management, and operator actions

Authentication model:

- one dedicated control token only

Suggested config key:

- `control.api_token`

### C. Required Rules

1. GUI token and MCP token must be stored separately in config.
2. GUI must never assume it can call MCP endpoints with `control.api_token`.
3. MCP clients must never call GUI/control endpoints with `mcp.api_token`.
4. Auth middleware must enforce scope by route group, not only by token presence.
5. Token rotation for GUI/control must not silently break MCP clients, and vice versa.
6. This iteration does not introduce auth levels or admin levels. Tokens are for authentication domain separation only.

### D. Route Grouping Proposal

#### MCP / automation-facing

- `/mcp/...` or current public MCP proxy paths
- `/_daemon/automation/*`
- browser session actions
- business profile/session start/stop flow

Auth:

- `mcp.api_token`

#### GUI / control-facing

- `/_control/status`
- `/_control/dashboard`
- `/_control/profiles`
- `/_control/sessions`
- `/_control/keepalive`
- `/_control/logs`
- `/_control/events`
- `/_control/plugins`
- `/_control/config/*`
- `/_control/service/*`

Auth:

- `control.api_token`

### E. Transport And Exposure Rules

Phase 1 default:

- bind control API to local-only interface by default
- bind GUI to local control API only
- do not expose GUI control API as an undocumented alias of MCP daemon routes

Future optional remote control:

- only after explicit enablement
- only with separate `control.api_token`
- preferably with origin/host allowlist and explicit UI warning

## Runtime Model To Centralize

The control daemon/core must own these models and return them as typed responses.

### ProfileRuntimeState

- profile name
- configured account label
- actual site-state summary if known
- occupancy state
- occupancy owner type
- occupancy owner id
- current runtime root path
- current engine
- keepalive enabled
- keepalive running
- mirror/runtime state
- external chromium running
- last launch
- last keepalive result
- warnings/errors

### SessionRuntimeState

- session id
- source type: `mcp | gui | keepalive | script | manual | system`
- profile name
- engine name
- created at
- last activity at
- runtime mode
- worker/process binding
- heartbeat status if applicable

### ServiceRuntimeState

- core state
- daemon state
- worker state
- keepalive scheduler state
- warmup state
- config revision
- active profiles count
- active sessions count
- warnings

### LogRuntimeState

- logger source: `core | daemon | worker | keepalive | automation | gui-bridge`
- level: `debug | info | warning | error`
- timestamp
- profile name if applicable
- session id if applicable
- event code/category
- message
- structured detail payload if present

This log model must come from core/control API, not from GUI-local ad hoc buffers.

### PluginCatalogItem

- plugin id
- display name
- plugin type: `system | user`
- source type: `bundled | github_url | download_url | local_crx | local_zip`
- source location
- version if known
- install state
- integrity metadata if available
- enabled/disabled state
- last refresh/apply result

### ProfilePluginAssociation

- profile name
- plugin id
- association state
- applied runtime mode
- last applied/result state

### Event Model

- timestamp
- event type
- profile name
- session id
- source type
- action
- result
- details

This event stream should power logs, dashboard summaries, and diagnostics.

## Proposed Implementation Phases

## Phase 0 - Freeze and Contract

Deliverables:

- inventory all GUI-owned runtime logic
- classify logic into `core`, `control daemon`, `gui-only`
- define API contracts before moving code
- define auth config schema for `control.*`
- define log config schema
- define plugin catalog and profile association schema

Output:

- architecture spec
- route/auth matrix
- runtime state models
- plugin model contract
- log model contract

## Phase 1 - Core Extraction

Move/normalize into core:

- occupancy truth assembly
- external process detection
- keepalive runtime state
- session availability decision
- reclaim/reap rules
- profile action availability logic
- structured log production and retention
- plugin catalog truth
- profile-plugin association truth

Requirements:

- GUI no longer recomputes these from raw files/timers/processes
- daemon exposes canonical results from core

## Phase 2 - Control API Introduction

Add new control API surface:

- status/dashboard endpoints
- profile/session listing endpoints
- event/log endpoints
- service lifecycle endpoints
- config read/write endpoints where safe
- plugin catalog endpoints
- profile-plugin association endpoints

Suggested route families:

- `GET /_control/status`
- `GET /_control/dashboard`
- `GET /_control/profiles`
- `GET /_control/profiles/{profile_name}`
- `GET /_control/sessions`
- `GET /_control/keepalive`
- `GET /_control/logs`
- `GET /_control/logs/summary`
- `GET /_control/log-settings`
- `PUT /_control/log-settings`
- `GET /_control/plugins`
- `POST /_control/plugins`
- `PUT /_control/plugins/{plugin_id}`
- `DELETE /_control/plugins/{plugin_id}`
- `GET /_control/profiles/{profile_name}/plugins`
- `PUT /_control/profiles/{profile_name}/plugins`

Requirements:

- separate control auth middleware
- explicit route namespace
- versioned response schema where practical

## Phase 3 - GUI Client Refactor

Refactor GUI into:

- API client layer
- polling/subscription coordinator
- page-level view models
- widget/page components

Replace local logic with:

- control API calls
- state mapping only

GUI local state should be limited to:

- selected tabs
- filters/sorts
- expanded rows
- form draft values
- window/tray state

## Phase 4 - GUI Product Shell

Rebuild the shell around API-backed pages:

- sidebar navigation
- header/global state area
- content stack pages
- footer/status summary

Suggested pages:

- Dashboard
- Profiles
- Sessions
- MCP Service
- Keepalive
- Logs
- Plugins
- Settings
- About

## Phase 5 - Hardening

Add:

- event trace correlation ids
- retry/backoff rules for GUI polling
- daemon reconnect model
- degraded mode UI when daemon unavailable
- token rotation UX
- diagnostics export
- bounded log retention
- configurable log level and retention through control API

## Phase 6 - Packaging and Release

Finalize:

- bundled config defaults
- token bootstrap behavior
- migration behavior from legacy config
- release docs
- skill updates
- cross-platform packaging validation

## Current Implementation Status

As of `2026-06-15`, the following items are already implemented in code:

### Completed

- config schema now includes:
  - `control.*`
  - `logging.*`
  - `profile_plugins`
- daemon auth is split into two domains only:
  - MCP/business routes use `mcp.api_token`
  - GUI/control routes use `control.api_token`
- control route family exists in daemon:
  - `/_control/status`
  - `/_control/ping`
  - `/_control/dashboard`
  - `/_control/profiles`
  - `/_control/sessions`
  - `/_control/logs`
  - `/_control/log-settings`
  - `/_control/plugins`
  - `/_control/profiles/{profile_name}/plugins`
  - `/_control/service/worker/start`
  - `/_control/service/worker/stop`
- GUI daemon heartbeat now uses lightweight control ping instead of heavy daemon status
- GUI table/status/bottom summary now prefer control-side profile runtime payloads over GUI-local occupancy truth
- legacy `admin_token` has been removed from active daemon startup flow and active runtime-config path
- targeted regression tests for GUI/control/auth/runtime paths are passing

### Partially Completed

- GUI still contains local fallback/runtime compatibility logic:
  - local occupancy cache reads
  - external Chromium process scan
  - manual occupancy reconciliation
- log model exists only as an initial control-log file API, not yet as a full structured runtime event pipeline
- plugin support currently reuses keepalive plugin records plus profile association mapping; it is not yet a full global productized plugin catalog

### Not Yet Completed

- full core extraction of runtime truth out of GUI
- GUI page/shell modularization into API-backed page components
- control-side keepalive runtime model endpoints
- control-side config CRUD beyond current log/plugin scope
- structured event stream and diagnostics export
- release docs / skill updates for the new split architecture
- packaging/release verification for this refactor branch

## Migration Strategy

The migration must avoid a half-split system.

Rules:

1. Do not leave duplicated truth logic in GUI and daemon for long.
2. Move one runtime domain at a time behind a daemon API.
3. Once a domain is daemon-backed, delete GUI-local governance for that domain.
4. Keep compatibility shims short-lived and documented.

Recommended migration order:

1. status/health truth
2. profile occupancy/runtime truth
3. keepalive runtime truth
4. service lifecycle control
5. sessions/events/logs truth
6. plugin catalog and profile-plugin associations
7. GUI shell redesign

## Development Tasks

### Workstream A - Contracts

- define control API namespace and auth model
- define response schemas
- define config schema for `control.*`
- define migration rules from current `mcp.*` only config
- define log API schema and retention settings schema
- define plugin catalog schema and profile-plugin association schema

### Workstream B - Core Consolidation

- extract runtime truth builders from GUI
- centralize action availability rules
- centralize keepalive state assembly
- centralize external/manual usage detection
- centralize structured log storage/retention
- centralize plugin resolution and profile-plugin association truth

### Workstream C - Daemon Control Surface

- implement control auth middleware
- implement control endpoints
- implement event/log snapshot endpoints
- implement config reload/persist endpoints as needed
- implement control-side log management endpoints
- implement plugin management endpoints
- implement profile-plugin association endpoints

### Workstream D - GUI Refactor

- add control API client
- add typed view-model mappers
- remove direct runtime truth logic from GUI
- modularize pages/components
- replace GUI-local log assumptions with control-log API consumption
- replace GUI-local plugin/profile association assumptions with control API consumption

### Workstream E - Product Shell

- sidebar/header/status shell
- page navigation
- tray/menu bar behavior
- persistent window bounds/layout
- logs page with source/level filtering and retention controls
- plugin management page
- profile create flow with plugin association selection
- profile update flow with plugin association update behavior clearly defined

### Workstream F - Documentation

- architecture guide update
- GUI/control API guide
- auth guide with MCP vs GUI boundary
- migration guide
- operator guide for logs
- operator guide for plugin catalog and per-profile associations
- skill updates

## Additional Product Requirements

### 1. Logging Productization

The logging system must follow the same split principle:

- logs are produced by `core/daemon/worker`, not authored as GUI truth
- GUI reads logs through control API
- GUI can control log display/filter settings through control API-backed configuration

Required capabilities:

- configurable log level
- configurable retention duration
- source filtering
- profile/session filtering where meaningful
- bounded storage policy
- export/diagnostic retrieval path

### 2. Plugin Catalog And Per-Profile Associations

Current plugin handling is too static. Future design must support:

- globally configured plugin catalog
- independent per-profile plugin associations

Plugin source types to support:

- remote GitHub/download URL
- local CRX
- local ZIP
- bundled/system plugin

Rules:

1. plugin definition is global
2. plugin-to-profile association is independent per profile
3. profile create flow supports initial plugin association selection
4. profile update flow supports association changes without corrupting unrelated profile associations
5. plugin update/removal must define behavior for already-associated profiles

Required GUI surfaces:

- plugin catalog management page
- plugin selection during profile creation
- plugin association editing during profile update
- clear display of system plugins vs user plugins

## Testing Plan

## 1. Unit Tests

Focus:

- core runtime truth assembly
- occupancy transitions
- session availability decisions
- keepalive state derivation
- auth middleware scope enforcement
- config migration behavior
- log retention/config behavior
- plugin catalog mutation behavior
- profile-plugin association behavior

Must include:

- `control.api_token` cannot access MCP routes
- `mcp.api_token` cannot access control routes
- log filtering and retention rules work deterministically
- profile association updates do not mutate unrelated profiles

## 2. Integration Tests

Focus:

- daemon + core startup
- control API auth and route behavior
- GUI client against mock/real daemon
- worker lifecycle controls
- keepalive status reflection
- event/log retrieval
- log settings update and effect
- plugin catalog CRUD through control API
- profile-plugin association CRUD through control API

Scenarios:

- daemon down, GUI degraded mode
- daemon restart, GUI reconnect
- token missing/invalid/rotated
- stale occupancy recovery
- manual Chromium open while GUI active
- log retention cleanup runs and does not block runtime
- plugin source unavailable / malformed / deleted locally

## 3. End-to-End Product Tests

Focus:

- GUI launches daemon/core
- GUI reads dashboard/profiles/sessions correctly
- GUI triggers profile launch/close/reclaim through control API
- MCP clients still work independently with MCP token
- keepalive actions reflect correctly in GUI
- logs page reflects core/daemon state accurately
- profile create/update plugin association flows work end-to-end

## 4. Security Tests

Mandatory checks:

- route-scope auth separation
- no accidental fallback from control token to MCP token
- no accidental fallback from MCP token to control token
- local-only defaults enforced
- secret values not leaked in logs/UI exports
- plugin remote source handling does not silently trust arbitrary malformed inputs

## 5. Regression Tests

Preserve:

- current MCP business flows
- current isolated runtime/mirror logic
- current worker policy behavior
- existing keepalive plugin behavior until intentionally redesigned

## Validation Plan

## A. Functional Validation

Validate under real local install:

- GUI reads true runtime state from control API only
- GUI action availability matches daemon/core truth
- MCP browser task flow still works
- keepalive flow still works
- manual browser usage is reflected consistently
- log page reflects actual runtime events from control API
- plugin catalog and profile associations behave consistently

## B. State Consistency Validation

Run mixed scenarios:

- GUI opens profile
- MCP acquires another profile
- script acquires a third profile
- keepalive runs on fourth profile

Verify:

- each profile shows one canonical owner/source
- no GUI-side phantom states
- no contradictory button availability
- plugin associations shown in GUI match daemon/core truth exactly

## C. Failure Validation

Scenarios:

- daemon killed
- worker killed
- GUI restarted
- token rotated during runtime
- config file changed externally
- stale occupancy/session heartbeat expiry
- broken plugin source
- deleted local plugin file
- log storage retention boundary reached

Verify:

- recovery path is explicit
- UI degrades predictably
- no silent wrong-state display

## D. Performance Validation

Targets:

- GUI idle CPU materially lower than current heavy polling behavior
- no large UI freezes during periodic refresh
- bounded polling cadence
- event/log retrieval does not block UI thread
- log queries remain bounded under larger history volume
- plugin association rendering does not trigger heavy synchronous I/O on UI thread

## E. Release Validation

Before shipping:

- Windows packaged app
- daemon/core/service startup
- GUI-control auth bootstrap
- MCP auth bootstrap
- upgrade from old config
- docs and skills updated
- logs feature works from packaged build
- plugin catalog and profile association flow works from packaged build

## Verification Record Requirement

Implementation is not accepted by code review alone.

Required acceptance artifact:

- a verification record document mapped back to this plan
- each completed capability must have:
  - scenario
  - method
  - result
  - evidence location if relevant

Suggested later file naming:

- `review/verification_gui_core_productization_<yyyymmdd>.md`

Acceptance must be based on verification record, not on implementation claim alone.

## Non-Goals For This Iteration

- immediate frontend tech-stack replacement
- remote web admin console
- complete keepalive plugin architecture redesign
- redesigning MCP protocol itself

Those can follow after the runtime-truth/control split is stable.

## Recommended Final Decision

Proceed with the split.

This is the correct productization direction, provided the implementation follows these constraints:

1. split by truth boundary, not only by process boundary
2. introduce a separate GUI/control auth domain
3. migrate runtime domains fully, not half-in/half-out
4. refactor GUI into a client, then redesign the shell
5. treat logs and plugin associations as first-class runtime products, not as GUI-local conveniences

If these rules are followed, the project will move from "powerful local tool" toward a more industrial desktop product.
