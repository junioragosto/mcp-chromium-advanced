# Official MCP Experience Alignment Round 2 Plan

Date: 2026-06-20

## 1. Goal

This round targets the remaining browser-operation gap versus the official `playwright-mcp` experience.

The objective is not more governance work. The objective is to make real browser tasks feel more natural, more first-pass successful, and less probe-heavy on difficult dynamic frontends.

## 2. Target Outcome

After this round, the managed browser core should deliver:

- higher first-pass action hit rate on complex dynamic pages
- stronger structured extraction on Gmail / YouTube Studio / GitHub class frontends without site-specific adapters
- less dependence on raw `run_script(...)` as the fallback truth surface
- better continuity between action result, post-action context, structured page state, and next-step reasoning
- more official-Playwright-MCP-like default ergonomics on the `patchright` primary path

## 3. Scope

This round includes all of the following.

### 3.1 Structured Extraction Strengthening

- improve structured extraction quality for dynamic pages with:
  - custom elements
  - overlays
  - dialog/menu/listbox states
  - repeated collection items
  - search/filter/toolbars
- improve region-scoped extraction so the interaction hotspot is described before full-page fallback
- improve collection detection for:
  - inbox/message lists
  - comment/thread lists
  - repository/file/result lists
- reduce low-value noise in structured output

### 3.2 Action Continuation Alignment

- make post-action context more useful as the default continuation surface
- increase the amount of actionable state returned after:
  - click
  - type
  - select
  - navigate
  - wait
- improve next-step suggestion quality from:
  - structured page
  - interaction region
  - candidate ranking
  - recent failures
- reduce cases where the caller must immediately chain:
  - full snapshot
  - diagnose page
  - raw script probe

### 3.3 `run_script` Boundary Hardening

- keep the current normalized `script_result_state` contract
- improve runtime wrappers so common structured reads serialize more reliably
- distinguish better between:
  - page not ready
  - empty logical result
  - non-serializable result
  - script returned nothing
- add more batch-level diagnostics where useful

### 3.4 Candidate Resolution And Ranking

- improve ranking for:
  - primary actions
  - search boxes
  - filters
  - transient menu items
  - dialog-local controls
- improve scoped resolution before page-wide enumeration
- expose clearer ranking reasons to help callers continue without blind retries

### 3.5 Official-Style Default Path

- keep `patchright` as the primary default engine
- continue treating:
  - `selenium_uc` as stealth / challenge / gesture specialist
  - `playwright_cli` as lightweight compatibility path
- make docs and skills steer callers toward the strongest default interaction path first
- reduce cases where the best outcome requires manual engine switching for ordinary MCP tasks

## 4. Implementation Areas

Primary files likely involved:

- `chromium_advanced/browser_session_kernel.py`
- `chromium_advanced/browser_session_kernel_diagnostics.py`
- `chromium_advanced/action_pipeline.py`
- `chromium_advanced/browser_engines/patchright_engine.py`
- `chromium_advanced/browser_engines/selenium_uc_engine.py`
- `chromium_advanced/browser_engines/playwright_cli_engine.py`
- `chromium_advanced/mcp_server.py`
- `README.md`
- `README_zh.md`
- `docs/skill_templates/browser-identity-mcp.SKILL.md`
- `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
- `docs/BROWSER_CORE_VALIDATION_PLAYBOOK.md`

## 5. Testing Plan

### 5.1 Core Tests

Run and extend local tests for:

- run-script normalization
- batch script behavior
- structured page extraction
- candidate ranking
- post-action context
- action trace continuity
- engine contract consistency

### 5.2 Browser-Core Validation

Use the validation playbook and add explicit checks for:

- complex dynamic structured read quality
- first-pass action success on difficult pages
- reduced exploratory retries
- lower reliance on raw script probing

### 5.3 Real Release Validation

Before release, validate installed runtime with:

- authenticated GitHub flow
- one complex dynamic signed-in page flow
- one incognito isolation flow
- one gesture / challenge-sensitive flow when applicable
- cleanup back to idle

## 6. Acceptance Standard

This round is complete only when:

- code changes are implemented
- docs and skill templates match the final behavior
- relevant test suite passes
- packaged runtime validation passes
- the browser-operation experience is materially closer to official `playwright-mcp` on the `patchright` primary path

## 7. Non-Goals

This round is not for:

- GUI redesign
- introducing a fourth browser engine
- site-specific hardcoded adapters for Gmail / YouTube Studio / GitHub
- changing profile-governance rules unless required by a browser-core fix
