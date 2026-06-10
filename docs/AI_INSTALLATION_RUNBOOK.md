# AI Installation Runbook

This runbook is written for an AI agent that receives a freshly cloned copy of
`mcp-chromium-advanced` and must prepare the environment for a user. Follow it as
an operational checklist. Do not rely on memory or assumptions when a local
command can verify the current state.

中文说明：这份文档不是普通用户教程，而是给 AI 执行安装、配置、验证时使用的操作手册。用户 clone 项目后，可以把本文件交给 AI，让 AI 完成环境探测、依赖安装、GUI 配置、MCP 验证和可选打包。

## 0. Operating Rules For The AI Agent

- Do not ask vague setup questions first. Inspect the machine, then report only concrete blockers.
- Do not commit or print secrets, tokens, cookies, browser profile data, or real `chromium_profiles.json`.
- Do not delete or rewrite a real `UserData` directory unless the user explicitly asks for that exact path.
- Do not enable headless browsing by default. This project is designed for visible real-profile automation; headless is only for explicit regression/background validation.
- Do not kill ordinary Chrome/Chromium processes by name alone. If cleanup is needed, only stop project-owned executables or browser processes whose executable/root path matches the configured project install/runtime paths.
- Treat the GUI profile `Account` field as an operator note only. It is not proof of login for every website. Verify the actual target-site account before account-sensitive work.
- The MCP server publishes standard tool annotations. Respect them when configuring an MCP client: normal profile/session operations, navigation, tab operations, clicking, typing, key presses, mouse actions, screenshots, diagnostics, and cleanup are trusted low-risk; arbitrary JavaScript remains non-read-only.
- Do not weaken profile ownership, keepalive, or mirror locks to avoid client approval prompts. Those locks protect real browser identity data.
- On Windows, preserve text encodings when editing Chinese documentation or config files.

## 1. Expected Runtime Shape

The normal source entry point is:

```powershell
python run_gui.py
```

The packaged application normally contains:

```text
<install_root>\ChromiumProfileManager.exe
<install_root>\ChromiumMcpDaemon\ChromiumMcpDaemon.exe
<install_root>\ChromiumMcpWorker\ChromiumMcpWorker.exe
```

The default MCP service shape is:

```text
GUI -> daemon http://127.0.0.1:28888 -> lazy worker http://127.0.0.1:28889
MCP endpoint: http://127.0.0.1:28888/mcp
```

Supported browser engines:

- `playwright_cli`: recommended default for normal MCP work, lower overhead, good for mirror-isolated parallel sessions.
- `selenium_uc`: best stealth-oriented option, useful when avoiding automation detection matters more than throughput.
- `patchright`: strongest structured inspection/debug path, useful for complex frontend diagnosis.

Changing the GUI default engine affects only future sessions. Existing sessions keep the engine used at startup.

## 2. Environment Detection Checklist

Run these checks first and record the result in your final handoff.

```powershell
$PSVersionTable.PSVersion
python --version
python -m pip --version
git --version
node --version
npm --version
playwright-cli --help
```

If `playwright-cli` is missing, install it:

```powershell
npm install -g @playwright/cli
playwright-cli --help
```

Python requirement: `3.10+`.

Node/npm are needed because the preferred `playwright_cli` engine depends on the Microsoft Playwright CLI executable being available on `PATH`.

## 3. Python Dependency Setup

From the repository root:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m compileall -q chromium_advanced
```

Optional isolated virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PyInstaller packaging is required, install PyInstaller in the same Python environment:

```powershell
python -m pip install pyinstaller
```

## 4. Browser And Driver Preparation

Prepare these local resources before first GUI configuration:

- Chromium-compatible browser executable, strongly recommended: `ungoogled-chromium`.
- Matching `chromedriver`.
- Persistent UserData root for real browser profiles.
- Optional mirror UserData root for snapshot-backed parallel MCP sessions.

Version checks:

```powershell
& '<path_to_chrome.exe>' --version
& '<path_to_chromedriver.exe>' --version
```

The browser major version and ChromeDriver major version should match. A mismatch is a common cause of Selenium/UC startup failures.

Recommended Windows layout:

```text
<tool_root>\chromium\chrome.exe
<tool_root>\drivers\chromedriver.exe
<tool_root>\UserData
<tool_root>\UserData\temp_user_data
```

The split-profile root should be configured as:

```text
paths.user_data_profiles_root = <tool_root>\UserData\UserDataSplited
paths.mirror_user_data_root = <tool_root>\UserData\UserDataSplited\mirror_disk
```

The project stores split-profile data and backups under:

```text
<user_data_profiles_root>\UserDataProfile1\Profile 1
<user_data_profiles_root>\UserDataProfile2\Profile 2
<user_data_profiles_root>\mirror_disk
```

The backup directory name remains `mirror_disk`.

## 5. First GUI Launch And Config

Start the GUI from source:

```powershell
python run_gui.py
```

On first launch, the app creates:

```text
Windows: %APPDATA%\ChromiumProfileManager\workstates\chromium_profiles.json
macOS: ~/Library/Application Support/ChromiumProfileManager/workstates/chromium_profiles.json
Linux: ${XDG_CONFIG_HOME:-~/.config}/ChromiumProfileManager/workstates/chromium_profiles.json
```

Configure these fields in the GUI or config file:

- `paths.chromium_dir`: browser executable path, or directory containing the browser executable.
- `paths.chromedriver_path`: ChromeDriver executable path, or directory containing it.
- `paths.user_data_root`: legacy shared-root path kept for migration compatibility.
- `paths.user_data_profiles_root`: split-profile root used at runtime.
- `paths.mirror_user_data_root`: backup snapshot directory, normally `<user_data_profiles_root>\mirror_disk`.
- `app.browser_engine`: recommended `playwright_cli` for normal MCP work.
- `app.concurrency_mode`: use `per_profile_live` as the normal mode. Use `block` only if you intentionally want conservative single-session gating.
- `mcp.enabled`: `true`.
- `mcp.host`: normally `127.0.0.1`.
- `mcp.port`: normally `28888`.
- `mcp.worker_port`: normally `28889`.
- `mcp.headless`: normally `false`.
- `mcp.start_minimized`: normally `true`, so MCP browser windows stay in the taskbar instead of stealing foreground focus.
- `keepalive.schedule_time`: recommended low-usage time, commonly `06:00`.
- `keepalive.plugin_dirs`: optional trusted local directories for Python keepalive site plugins; the GUI keepalive settings panel can also edit this field.

Keepalive plugins can add new site logic without rebuilding the app. The GUI exposes a dedicated Keepalive Plugins tab for inspecting built-in plugin source and editing trusted local plugins. See `docs/KEEPALIVE_PLUGIN_GUIDE.md`.

Do not treat `chromium_profiles.example.json` as a real config. It is a sanitized template.

## 6. Profile Setup

Profiles are browser containers such as `Profile 1`, `Profile 4`, or `Default`.

Required setup flow:

1. Create or discover profiles through the GUI.
2. Launch the profile visibly from the GUI.
3. Log in to required websites manually when needed.
4. Close the GUI-launched browser.
5. Run keepalive or MCP verification.

Important account rule:

- A profile can be logged in to Google but not GitHub, or GitHub but not YouTube Studio.
- The GUI `Account` label is not authoritative for every site.
- Before a task that needs login state, verify the actual site identity in the page DOM or page text.

## 7. Keepalive And Mirror Refresh

Keepalive uses real browser profiles to refresh selected sites. It now locks one profile at a time instead of blocking every profile globally.

Expected behavior:

- Keepalive may run while other profiles remain available for MCP, as long as they are not the same locked profile.
- After each finished profile run, the app automatically clears re-creatable cache, lock, and log artifacts from that profile's dedicated UserData root.
- After keepalive completes successfully, backup snapshots are refreshed when mirror support is enabled.
- Normal MCP sessions launch from the live per-profile root, not from extracted runtime clones.
- Same-profile parallelism is intentionally blocked.
- In the GUI, a profile row's `Launch` button becomes `Close` while that profile's Chromium processes are still alive. If the user closes the browser window manually, the GUI flips back to `Launch` after process exit is detected.

Verify mirror output:

```powershell
Get-ChildItem -Recurse '<user_data_profiles_root>\mirror_disk' | Select-Object -First 40
Get-ChildItem '<user_data_profiles_root>' -Directory | Select-Object Name, FullName
```

If `mirror_disk` is empty after a successful keepalive, inspect GUI logs and the mirror status in the config.

## 8. MCP Verification Flow

First verify the daemon:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:28888/_daemon/status' -TimeoutSec 5 | ConvertTo-Json -Depth 8
```

Then use the MCP tools in this order:

```text
list_profiles
get_server_status
get_profile_status(profile_name)
can_start_profile_session(profile_name, engine?)
start_profile_session(profile_name, engine?)
browser action tools
close_profile_session(session_id)
```

Rules for AI agents:

- If the user did not specify a `profile_name`, ask which profile to use. Do not guess real identities.
- If `can_start_profile_session` reports busy/unavailable, report the state instead of forcing reuse.
- Always call `close_profile_session(session_id)` when finished.
- Use explicit `engine` only when the task benefits from a specific engine. Otherwise use the GUI default.
- If the MCP client supports approval modes or trusted tools, prefer trusting all normal MCP browser tools from this server. Keep arbitrary JavaScript execution under stricter review.

Minimal browser smoke test:

```text
start_profile_session(profile_name="Profile 1")
navigate("https://example.com")
get_current_url()
get_page_text()
close_profile_session(session_id)
```

Login-sensitive smoke test:

```text
start_profile_session(profile_name="<profile>")
navigate("<target site>")
verify actual logged-in account from DOM/page text
close_profile_session(session_id)
```

## 9. Engine Selection Policy For AI Tasks

Use this policy unless the user gives a different explicit instruction:

- Use `playwright_cli` for ordinary browsing, forms, navigation, multi-tab work, screenshots, console/network checks, and parallel mirror-isolated workloads.
- Use `selenium_uc` for stealth-sensitive sites, or when the user reports automation banners/detection problems and accepts lower throughput.
- Use `patchright` for complex frontend debugging, structured snapshots, target inspection, and rich diagnostics.

Do not assume an engine switch affects an already running session. Close the current session and start a new one with the desired engine.

## 10. Build And Package

Only build after source-level verification passes.

If the C drive is space constrained, redirect temp directories before packaging:

```powershell
New-Item -ItemType Directory -Force -Path '<install_root>\build_tmp' | Out-Null
$env:TEMP = '<install_root>\build_tmp'
$env:TMP = '<install_root>\build_tmp'
```

Build:

```powershell
.\build_chromium_manage_gui_exe.ps1
```

Expected output:

```text
dist\ChromiumProfileManager.exe
dist\ChromiumMcpDaemon\
dist\ChromiumMcpWorker\
```

Replace an installed copy:

```powershell
$src = (Resolve-Path 'dist').Path
$dst = '<install_root>'
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\ChromiumProfileManager.exe" "$dst\ChromiumProfileManager.exe" -Force
robocopy "$src\ChromiumMcpDaemon" "$dst\ChromiumMcpDaemon" /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
robocopy "$src\ChromiumMcpWorker" "$dst\ChromiumMcpWorker" /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
```

Post-package verification:

```powershell
& '<install_root>\ChromiumProfileManager.exe'
Invoke-RestMethod -Uri 'http://127.0.0.1:28888/_daemon/status' -TimeoutSec 5 | ConvertTo-Json -Depth 8
```

Then run the MCP verification flow from section 8.

## 11. Troubleshooting

`playwright-cli` not found:

- Run `npm install -g @playwright/cli`.
- Confirm the global npm bin directory is on `PATH`.
- Restart the GUI/daemon after PATH changes.

ChromeDriver mismatch:

- Compare browser and driver major versions.
- Replace ChromeDriver with a matching build.

`external_chromium_running`:

- A live browser is using the real root.
- In `block` mode, close that browser before MCP startup.
- In `per_profile_live` mode, only the matching profile is blocked; other profiles can still start.

`keepalive_running` or `mirroring`:

- Wait for keepalive/mirror refresh to finish.
- Do not bypass this lock; it protects profile data integrity.

Browser windows steal focus:

- Confirm `mcp.start_minimized=true`.
- Do not set `mcp.headless=true` unless the user explicitly asks for headless operation.

Login state appears missing:

- Verify the actual target site, not the GUI `Account` label.
- Ask the user to log in through the visible GUI-launched profile if needed.
- Re-run keepalive/mirror refresh after login changes if mirror-isolated sessions should inherit the new state.

Daemon starts but worker does not:

- Check `http://127.0.0.1:28888/_daemon/status`.
- Confirm `mcp.worker_port` is not occupied.
- Check GUI MCP logs and the MCP trace path shown in the GUI status panel.

Orphan browser window remains after session close:

- Treat this as a cleanup bug or stale process condition.
- Inspect the session engine, runtime root, process path, and command line before terminating anything.
- Only kill processes that clearly belong to this project/runtime.

## 12. Required Final Handoff Format

When setup is complete, report:

- OS and shell used.
- Python, pip, Node, npm, and `playwright-cli` versions.
- Chromium executable path and version.
- ChromeDriver path and version.
- Config path used.
- `user_data_root` and `mirror_user_data_root`.
- Default engine and concurrency mode.
- MCP endpoint and daemon status.
- Profiles verified and which target-site login states were actually checked.
- Tests run, including MCP startup/action/close results.
- Remaining blockers or risks, if any.

Do not include secrets, tokens, cookies, or private profile data in the handoff.
