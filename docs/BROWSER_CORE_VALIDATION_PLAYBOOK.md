# Browser Core Validation Playbook

This document defines the standard validation playbook for browser-core upgrades.

## Goal

Use one stable validation standard for:

- large browser-core releases
- smaller iteration smoke checks
- local pre-release verification before replacing the installed desktop build

The intent is to avoid ad hoc validation and keep future comparisons consistent.

## Engine Positioning

Use these engine assumptions throughout validation:

- `patchright`
  default primary path for structured extraction, complex frontend interaction, and richer diagnostics
- `selenium_uc`
  preferred for stealth-sensitive pages, recurring challenge/verification flows, and gesture/XY interaction
- `playwright_cli`
  lightweight compatibility and lower-overhead path, not the primary high-fidelity structured-read engine

## Large Release Validation

Every large release validation pass should cover all of the following:

1. Packaged startup
- GUI starts from the installed packaged root
- daemon and worker start through the packaged runtime path
- GUI remains alive after startup

2. Default engine verification
- `get_server_status()` confirms the configured default engine
- expected current baseline: `patchright`

3. Real authenticated scenario
- use `Profile 1` when available
- run at least one authenticated complex scenario such as:
  - Gmail inbox read
  - GitHub logged-in dashboard/page interaction
  - another real signed-in dynamic page with structured reads

4. Parallel validation
- run at least two independent profiles in parallel
- confirm no state confusion and no occupancy leak

5. Incognito validation
- validate isolation through the managed daemon automation path with `runtime_options.incognito=true`
- do not assume MCP session start directly supports `runtime_options`

6. Stealth / challenge / gesture validation
- run at least one `selenium_uc` flow that exercises one of:
  - challenge-heavy site access
  - stealth-sensitive browsing
  - slider / drag / gesture-style interaction

7. Cleanup validation
- all sessions release cleanly
- daemon state returns to `idle`
- no obvious stuck occupancy remains

## Small Iteration Smoke

For smaller iterations, run this minimum smoke set:

1. startup
- GUI/daemon reachable

2. one profile session
- `can_start_profile_session(...)`
- `start_profile_session(...)`

3. one simple navigation
- navigate to a known page

4. one structured read
- use `browser_get_interaction_context(...)` or `browser_list_candidates(...)`

5. one high-level action
- for example `click_target(...)`, `type_target(...)`, or `select_option(...)`

6. release
- close the session and confirm the service returns to `idle`

## Readback Guidance

When validating browser-core behavior on difficult frontends:

- prefer `structured_page`, `browser_list_candidates(...)`, `browser_get_interaction_context(...)`, action traces, and screenshots as the first validation surface
- treat raw `run_script(...)` as a supplemental readback surface, not the only truth source
- if `run_script(...)` returns `result=null`, treat that as a diagnostic runtime boundary rather than immediate proof that the page is broken
- if `run_script(...)` returns `script_result_state="stringified"`, treat that as a serialization boundary rather than a normal structured success
- verify that follow-up candidate selection is using recent structured context instead of reverting to broad full-page probing immediately after a successful action
- on collection-heavy pages, verify that later reads/actions stay biased toward the active collection kind and current interaction region when that is the semantically correct path

## Performance Baseline Checks

Every large release validation should also record:

- idle daemon CPU behavior
- action latency for one complex frontend flow
- whether the new path reduced exploratory round-trips compared with the prior build
- whether diagnostics/logging stayed readable instead of exploding in volume
- whether successful high-frequency actions stayed on the lightweight post-action path instead of silently triggering heavy follow-up probes
- whether recent structured context improved follow-up hit rate on popup/filter/search/result-list style pages

## Validation Output

Record at least:

- build identifier or commit
- validation date
- packaged install root used
- default engine observed
- authenticated scenario result
- parallel scenario result
- incognito scenario result
- stealth/challenge/gesture scenario result
- cleanup result
- open risks

For local-only notes, keep artifacts outside the repo.
