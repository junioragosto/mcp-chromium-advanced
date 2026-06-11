# MCP Chromium Advanced

MCP Chromium Advanced 是一个用于管理真实 Chromium 浏览器 Profile 的桌面 GUI 与 MCP 服务。它面向的不是一次性、无状态的自动化浏览器，而是需要复用真实登录态、Cookie、本地存储、扩展与书签的工作流。

[English README](./README.md)

## 项目概述

这个项目可以理解为“真实 Chromium 身份管理器 + MCP 浏览器服务”。

如果是第一次接触这个项目，可以先抓住下面 7 个关键点：

1. 它解决的是“真实登录态复用”问题。
   GUI 管理的 Chromium Profile 可以暴露给 MCP 客户端使用，自动化流程能够直接复用 Cookie、Local Storage、扩展、书签和站点权限。
2. 它采用分层运行结构。
   GUI 负责配置和 Profile 管理，daemon 提供稳定的 MCP 入口，worker 按需启动，而受管浏览器会话内核会在 MCP tools 调用前统一运行时行为。
3. 它支持多种浏览器执行引擎。
   共享的 Profile 管理与会话占用逻辑保持不变，浏览器执行后端当前支持 `selenium_uc`、`patchright` 和 `playwright_cli`。
4. 它对上层暴露的是更稳定的运行时契约，而不是原始引擎差异。
   受管会话内核会补充结构化能力描述、统一错误码和通用 fallback，减少调用方直接面对引擎差异的概率。
5. 它的核心设计是“安全占用真实身份”。
   会话检查会避免多个 live root 任务、线程或 keepalive 作业同时抢占同一个已登录浏览器身份；并发能力建立在统一治理规则之上，而不是绕过锁。
6. 它连接的是真实 Chromium Profile。
   项目会用真实的 `user-data-dir` 和 `profile-directory` 启动浏览器，再由选定执行引擎接入这个持久化 Profile。
7. 它除了 MCP 控制，还支持保活工作流。
   GUI 可以对真实登录的 Profile 执行手动或定时保活任务，目标站点可以包括 ChatGPT、Gmail、Google、GitHub，未来也可通过插件扩展。

重要账号边界：Chromium 的 `Profile N` 是浏览器数据容器，不是所有网站共用的账号。GUI 里的 `Account` 字段只是人工维护的标签或备注，只能作为线索，不能当作 GitHub、YouTube、ChatGPT、Gmail、Google 等目标网站当前登录账号的证明。涉及账号正确性的自动化任务，必须进入目标网站后读取该网站自己的登录状态再继续。

公开入口仍然是：

```bash
python run_gui.py
```

## 截图

![软件截图](docs/imgs/ScreenShot.png)

## 工作原理

当前浏览器自动化层支持：

- Selenium: <https://www.selenium.dev/>
- undetected-chromedriver: <https://github.com/ultrafunkamsterdam/undetected-chromedriver>
- Patchright: <https://github.com/Kaliiiiiiiiii-Vinyzu/patchright>
- Playwright CLI: <https://github.com/microsoft/playwright-cli>

项目会使用真实的 `user-data-dir` 和 `profile-directory` 启动 Chromium，再通过选定引擎接入这个浏览器上下文。这样 worker 可以复用真实的登录态、Cookie、Local Storage、扩展和其它持久化状态。

如果需要指纹相关插件，也可以配合 `my-fingerprint`：

- my-fingerprint releases: <https://github.com/omegaee/my-fingerprint/releases>

在浏览器控制层之上，MCP 服务再补充：

- Profile / 会话占用检查
- 会话启动与释放接口
- 稳定 daemon + 懒启动 worker 结构
- GUI 内的开关、日志和状态展示
- 统一能力模型、统一错误语义与通用 fallback

## 主要能力

- 用一个 GUI 管理多个 Chromium Profile
- 通过 MCP 把真实浏览器身份暴露给客户端
- 避免多个线程或任务抢占同一个 Profile
- 在 GUI 中切换默认浏览器执行引擎
- 对外暴露结构化运行时能力，而不只是原始引擎名
- 把动作失败统一成稳定错误码
- 只在需要时启动浏览器 worker
- 在空闲超时后自动回收资源
- 对真实登录 Profile 运行 keepalive 任务
- 通过显式 tab 工具支持多标签页协作
- 采集结构化的 console、页面错误与网络请求诊断信息
- 对弱能力 runtime 提供通用 fallback，例如 snapshot、候选元素枚举、等待和 target 诊断

## 引擎选择策略

当前项目把浏览器引擎视为同一套 Profile / Session 治理之下的不同执行策略。

有两种选择方式：

- GUI 默认引擎
  存在 `app.browser_engine` 中。MCP 调用方没有显式传 `engine` 时，就使用这里的默认值。
- 单次请求显式指定引擎
  MCP 调用方可以在 `can_start_profile_session(...)` 和 `start_profile_session(...)` 中传 `engine`。

推荐的实际策略：

- `playwright_cli`
  适合作为日常 MCP 任务默认引擎。启动轻、交互开销低，更适合当前按 Profile live 并发模型。
- `selenium_uc`
  更适合风控敏感站点、登录站点、伪装优先场景。目前项目里它仍然是 stealth 最强的一条路径。
- `patchright`
  更适合复杂前端诊断、结构化调试、多标签观察、snapshot/ref 相关能力要求更高的任务。

关于切换引擎，有几个必须明确的边界：

- 修改 GUI 默认引擎，只影响之后新启动的会话。
- 已经运行中的 session 不会被热切换。
- `reuse_existing=true` 只会复用“同一个 Profile + 同一个引擎”的会话。
- 用不同引擎再开一个会话，不会把已有会话热切换过去；已经启动的 session 会保持原来的引擎。

## 运行要求

- Python 3.10+
- 本地可用的 Chromium 或 Chrome
- 与浏览器版本匹配的 ChromeDriver
- 桌面图形环境

安装依赖：

```bash
pip install -r requirements.txt
```

如果是 clone 后交给 AI 代装环境，请同时参考：

- [AI 安装与运行手册](./docs/AI_INSTALLATION_RUNBOOK.md)

## 浏览器与驱动准备

使用前需要准备三类本地资源：

1. Chromium 或 Chrome 浏览器
2. 匹配版本的 `chromedriver`
3. 持久化的用户数据目录

常见选择包括：

- ungoogled-chromium
- Chromium
- Google Chrome

`chromedriver` 的主版本应尽量与浏览器主版本一致。

### 强烈推荐

项目强烈推荐 `ungoogled-chromium`：

- 更适合长期、本地、稳定的自动化场景
- 不会频繁自动更新，能降低浏览器升级后导致 `chromedriver` 失效的概率
- 优先建议使用 `136` 以下版本，因为更高版本在部分环境下可能与 `ungoogled-chromium` 存在兼容性问题

下载地址：

- ungoogled-chromium: <https://ungoogled-software.github.io/ungoogled-chromium-binaries/>
- Chrome for Testing / ChromeDriver: <https://googlechromelabs.github.io/chrome-for-testing/>

## 配置文件

程序首次启动时，会在系统配置目录下创建配置文件：

- Windows: `%APPDATA%/ChromiumProfileManager/workstates/chromium_profiles.json`
- macOS: `~/Library/Application Support/ChromiumProfileManager/workstates/chromium_profiles.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/ChromiumProfileManager/workstates/chromium_profiles.json`

仓库内提供了脱敏模板：

- `chromium_profiles.example.json`
- `resources/bookmarks_template.html`

首次运行时，如果本地默认路径中还没有书签模板，程序会把仓库附带模板复制过去。

几个关键字段：

- `paths.chromium_dir`
  浏览器可执行文件路径，或包含它的目录
- `paths.chromedriver_path`
  `chromedriver` 路径，或包含它的目录
- `paths.user_data_root`
  仅保留给历史 shared-root 迁移兼容使用
- `paths.user_data_profiles_root`
  新的 split 根目录。每个 Profile 对应一个独立 UserData 根，例如 `UserDataProfile1/Profile 1`
- `paths.mirror_user_data_root`
  备份快照目录。正常 MCP 启动不再依赖镜像 runtime clone
- `paths.bookmarks_template_path`
  初始化 profile 时可选的书签模板路径
- `paths.fingerprint_zip_path`
  与 `my-fingerprint` 相关的可选路径
- `app.browser_engine`
  默认浏览器执行后端，目前支持 `selenium_uc`、`patchright`、`playwright_cli`
- `app.concurrency_mode`
  会话并发治理模式。当前默认 `per_profile_live`，允许不同 Profile 并发，但同一个 Profile 保持互斥
- `mcp.host` / `mcp.port` / `mcp.worker_port` / `mcp.path`
  daemon 与 worker 的网络配置
- `mcp.api_token`
  可选 Bearer token。配置后，所有 daemon 请求都必须带 `Authorization: Bearer <token>`，不会因为是 localhost 而豁免

### 按 Profile live 并发

当前运行时已经改成“每个逻辑 Profile 拥有一个独立 UserData 根目录”：

- `per_profile_live`
  当前默认模式。不同 Profile 可以并发运行，但同一个 Profile 在 GUI、keepalive、MCP 之间仍然严格互斥。
- `block`
  兼容性的保守模式。

需要明确的规则：

- keepalive 不再因为别的 Profile 正在使用就全局跳过，而是逐 Profile 加锁、逐 Profile 执行。
- 镜像现在是备份工件，不再是正常会话启动的主路径。
- 同一个 Profile 的并发是明确禁止的。

### 手动启动 / 关闭行为

GUI 里的 `启动` 按钮现在是所选 Profile 的运行时切换按钮：

- 如果该 Profile 当前未运行，按钮会启动一个可见 Chromium 窗口。
- 如果该 Profile 当前已运行，按钮会切到 `关闭`，并只结束该 Profile 对应的 Chromium 进程。
- 如果用户手动关闭了窗口，GUI 会继续检测真实进程状态；只有在该 Profile 进程完全退出后，按钮才会自动切回 `启动`。
- 如果窗口已经没了，但还有后台 Chromium 进程残留，GUI 会继续显示 `关闭`，让操作者显式回收残留进程。

这个行为是 Profile 级别的，不会影响其它 Profile。

## MCP 服务

在 GUI 中启用后，daemon 会暴露稳定 HTTP 入口，例如：

```text
http://127.0.0.1:28888/mcp
```

daemon 在任务之间保持可用；真正的浏览器 worker 只会在请求需要时才启动，并在配置的空闲超时后回收。

运行说明：

- 如果配置了 `mcp.api_token`，每个 daemon 请求都必须带 `Authorization: Bearer <token>`。没有 localhost 特殊豁免。
- GUI 自己轮询 daemon 状态时也会使用同一个 bearer token，因此 GUI 和外部 MCP 客户端遵循的是同一套认证规则。
- daemon 设计上是稳定常驻的，worker 则是短生命周期、按需启动。
- 因 `idle_timeout` 被回收的 worker 属于正常托管生命周期事件，不是 crash。
- 如果配置的 Chromium 根目录里已经有 live browser process，session startup 会被有意阻断，并报告 `external_chromium_running` 等状态。
- 这套 busy-state 规则现在也是按 profile root 生效，`playwright_cli` 也遵循同样治理规则。
- MCP tools 会发布标准 tool annotations，便于支持该能力的客户端区分“可信的本地 / 浏览器操作”和“任意脚本执行”。
- 这些 annotations 可以减少不必要的审批提示，但不会绕过客户端本身的审批策略，也不会绕过本项目的 profile / busy-state 治理规则。

典型 MCP 生命周期：

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. 执行浏览器动作
7. `close_profile_session(session_id)`

如果需要显式指定引擎，可以这样调用：

```text
can_start_profile_session(profile_name="Profile 4", engine="selenium_uc")
start_profile_session(profile_name="Profile 4", engine="playwright_cli")
```

## 调试与可观测性

worker 暴露了正式的多标签工具与调试工具，减少依赖人工盯着浏览器窗口的需要。

多标签相关：

- `browser_list_tabs`
- `browser_open_tab`
- `browser_activate_tab`
- `browser_close_tab`

调试与诊断相关：

- `browser_get_console_messages`
- `browser_get_page_errors`
- `browser_get_network_requests`
- `browser_clear_debug_buffers`
- `browser_diagnose_page`
- `browser_get_action_trace`
- `get_mcp_tool_trace`

其中：

- `browser_diagnose_page` 是卡住时优先使用的高信号诊断入口。
- `browser_get_action_trace` 用于看单个 session 的动作耗时、fallback 和失败。
- `get_mcp_tool_trace` 用于看 MCP worker 级别的工具调用耗时。

MCP trace 也会写入轮转 JSONL 文件，GUI 的 MCP 状态面板中会显示当前 trace 文件路径。

## 引擎说明

### 共同点

- Profile 创建、删除、同步、书签和会话占用都共享同一套治理逻辑
- GUI 和 MCP 的会话流转方式对上层保持一致
- 真实 `user-data-dir` + `profile-directory` 始终是事实来源

### Selenium + undetected-chromedriver

- 当前项目里最成熟的一条路径之一
- 也是现有 keepalive 工作流的执行基础
- 直接 Profile 启动会使用共享的 `launch.*` 默认配置
- 目前项目里 stealth 最强
- 在没有显式配置引擎时，代码层 fallback 默认仍然偏向它

### Patchright

- 已支持通过 MCP / session 层接入真实持久化 Profile
- 启动参数比 Selenium 更克制，用于兼容性
- 适合 Playwright 执行模型更可靠的网站
- 在当前项目中提供最强的 tab 模型和结构化调试能力
- 能通过每个 tab 的 CDP session 收集 DevTools 风格诊断信息
- 当前阶段 keepalive 还没有走 Patchright

### Playwright CLI

- 当前这台机器上，GUI 默认引擎设成 `playwright_cli` 时，它是日常 MCP 任务的优先引擎
- 更适合当前 per-profile live 运行时下的低开销任务执行
- 原生 stealth 弱于 `selenium_uc`
- 原生 inspection fidelity 弱于 `patchright`，但受管运行时会用 fallback、diagnostics 和恢复元数据把它补强
- 它作为与其它引擎并行的第三种实现，接在同一条 `SessionManager -> BrowserEngine factory` 路径之下
- 启动时用 `playwright-cli open --persistent`，之后复用 named session 做后续命令
- 复用真实 `user-data-dir` 与 Chromium `--profile-directory=Profile N`，所以可以保留登录态
- 当前已覆盖的稳定能力包括：会话启动、导航、多标签基础、脚本执行、type / click / key、截图、console、requests 和粗粒度页面诊断
- 受管运行时为它补充通用 `snapshot`、候选元素枚举、等待、target 验证与 snapshot-ref 风格定位
- 简单 selector `click` / `fill` 会优先走快速 DOM eval path，失败时再退回原生命令
- 会把 console / network 噪音分类，例如 third-party、asset、media、security policy、CORS、auth
- 会清洗上游 Chromium launch args，避免通过真实 `--disable-blink-features` 注入 `AutomationControlled`
- 默认遵守 `mcp.start_minimized=true`，可见 MCP 浏览器会最小化停在任务栏，而不是抢前台焦点
- 默认保持 `mcp.headless=false`；headless 只用于用户明确要求的回归 / 后台验证
- 关闭 session 时会尝试清理自有 `playwright-cli` daemon / Chromium 进程，并回收隔离运行时目录；启动时也会清理不再被活进程引用的陈旧临时目录
- 共享 root 运行时现在仅作为迁移兼容布局，正常运行应使用 `paths.user_data_profiles_root`
- 当前阶段 keepalive 还没有切到 `playwright_cli`
- Windows 打包后的 GUI、daemon、worker EXE 已在受管运行时路径上完成验证

## Keepalive 插件

keepalive 站点现在走插件化运行时。内置站点包括 `chatgpt`、`google`、`gmail`、`github`；外部 Python 插件可以不重编译程序就新增 `youtube`、`youtube_studio` 或其它站点。桌面 GUI 也已经有专门的 Keepalive Plugins 页面，用于查看内置插件源码、创建外部插件、编辑受信任本地插件。

- [Keepalive Plugin Guide](./docs/KEEPALIVE_PLUGIN_GUIDE.md)

## Skill 模板

仓库包含可复用的 agent skill 模板：

- `docs/skill_templates/`

这些文件是给 Codex 或其它 AI 工作流复用的模板，不是程序自动装载的运行目录。

## 隐私与安全

- 不要提交真实 `chromium_profiles.json`
- 不要提交真实 profile 数据、Cookie、session 状态或个人账号标签
- 不要把 MCP endpoint 暴露给不可信网络
- Agent 不应猜测真实 profile 身份；应显式询问或使用明确的 `profile_name`

## 仓库结构

- `run_gui.py`
  公开入口
- `chromium_advanced/chromium_manage_gui.py`
  桌面 GUI
- `chromium_advanced/mcp_daemon.py`
  稳定 daemon 服务
- `chromium_advanced/mcp_server.py`
  浏览器 worker 实现
- `docs/ARCHITECTURE_GUIDE.md`
  更多实现说明
- `docs/skill_templates/`
  提供给 Codex 或其它 AI 工作流复用的 skill 模板
- `resources/bookmarks_template.html`
  项目附带的书签模板

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).
