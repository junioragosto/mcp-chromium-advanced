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
   会话检查会避免多个 live root 任务、线程或 keepalive 作业同时抢占同一个已登录浏览器身份；开启镜像隔离后，则通过受控的 runtime clone 提供并发路径。

6. 它连接的是真实 Chromium Profile。  
   项目会用真实的 `user-data-dir` 和 `profile-directory` 启动浏览器，再由选定执行引擎接入这个持久化 Profile。

7. 它除了 MCP 控制，还支持保活工作流。  
   GUI 可以对真实登录的 Profile 执行手动或定时保活任务，目标站点可以包括 ChatGPT、Gmail、Google 和 GitHub。

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

如果你需要指纹相关插件，也可以配合 `my-fingerprint`：

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
  适合作为日常 MCP 任务默认引擎。启动轻、交互开销低，且更适合新的按 Profile live 并发模型。
- `selenium_uc`
  更适合风控敏感站点、登录站点、伪装优先场景。当前项目里它仍然是 stealth 最强的一条路径。
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
- `app.browser_engine`
  默认浏览器执行后端，当前支持 `selenium_uc`、`patchright`、`playwright_cli`
- `app.concurrency_mode`
  会话并发治理模式。当前默认 `per_profile_live`，允许不同 Profile 并发，但同一个 Profile 仍然互斥
- `mcp.*`
  MCP daemon 与 worker 的地址、端口、超时与日志级别
- `profiles[]`
  Profile 名称、账号标识、备注、保活配置等

### 镜像隔离并发

当前运行时已经改成“每个逻辑 Profile 拥有一个独立 UserData 根目录”：

- `per_profile_live`
  当前默认模式。不同 Profile 可以并发运行，但同一个 Profile 在 GUI、keepalive、MCP 之间仍然严格互斥。
- `block`
  兼容性的保守模式。

需要明确的规则：

- keepalive 不再因为别的 Profile 正在使用就全局跳过，而是逐 Profile 加锁、逐 Profile 执行。
- 镜像现在是备份工件，不再是正常会话启动的主路径。
- 同一个 Profile 的并发是明确禁止的。

## MCP 使用方式

推荐的 MCP 生命周期如下：

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. 执行浏览器操作
7. `close_profile_session(session_id)`

运行态补充：

- `启动` 按钮现在会根据该 Profile 的真实 Chromium 进程状态自动切换为 `关闭`。
- 点击 `关闭` 只会结束当前 Profile 对应的 Chromium 进程，不会影响其他 Profile。
- 如果只是手动关闭浏览器窗口，GUI 会继续探测该 Profile 的真实进程状态；只有在进程完全退出后，按钮才会自动切回 `启动`。
- keepalive 现在按 Profile 加锁，而不是全局锁死所有 Profile。其他未占用的 Profile 仍可继续用于 MCP。

如果需要按场景显式指定引擎，可以这样调用：

```text
can_start_profile_session(profile_name="Profile 4", engine="selenium_uc")
start_profile_session(profile_name="Profile 4", engine="playwright_cli")
```

如果是多标签页任务，建议显式使用：

1. `browser_list_tabs`
2. `browser_open_tab`
3. `browser_activate_tab`
4. 页面动作
5. `browser_close_tab`

## 工业化运行时升级

当前版本已经引入受管浏览器会话内核：

- 对外保持现有 MCP tool 名称不变
- 通过结构化能力模型暴露运行时能力
- 统一错误语义，例如：
  - `action_not_supported_by_runtime`
  - `target_not_found`
  - `target_not_interactable`
  - `timeout`
  - `runtime_action_failed`
- 对部分高层工具在弱能力 runtime 上提供通用 fallback

对应的升级规划见：

- [docs/INDUSTRIAL_RUNTIME_UPGRADE_PLAN.md](docs/INDUSTRIAL_RUNTIME_UPGRADE_PLAN.md)

## 架构说明

更完整的分层说明见：

- [docs/ARCHITECTURE_GUIDE.md](docs/ARCHITECTURE_GUIDE.md)

当前可以简化理解为：

`GUI / MCP tools -> SessionManager -> ManagedBrowserSession / Action Kernel -> BrowserEngine runtime -> Chromium backend`

并发开启后的实际路径则变为：

`GUI / MCP tools -> SessionManager -> mirror snapshot selection -> isolated runtime clone -> BrowserEngine runtime -> Chromium backend`

## 构建

Windows 下可使用：

```powershell
.\build_chromium_manage_gui_exe.ps1
```

它会构建：

- `ChromiumProfileManager`
- `ChromiumMcpDaemon`
- `ChromiumMcpWorker`

## 诊断与可观测性

MCP 侧现在同时提供两层轻量追踪：

- `browser_get_action_trace(session_id)`：查看单个浏览器会话最近的受管动作、慢动作、失败、fallback 次数和平均耗时
- `get_mcp_tool_trace()`：查看当前 MCP worker 进程内最近工具调用的耗时、结果大小和错误摘要

`browser_diagnose_page` 仍然是页面卡住时的首选诊断工具，但 `playwright_cli` 路径下的 console/network/raw 诊断已经做了短超时、单次 network 拉取和原始输出截断。复杂站点即使日志很吵，也应尽量返回部分诊断结果，而不是把 MCP worker 卡到分钟级。

MCP 工具级 trace 会写入 JSONL 文件，并自动按大小轮转，避免长期运行无限增长。GUI 的 MCP 状态面板会显示当前 trace 文件路径，便于现场排查。

## 当前已知边界

- keepalive 目前仍未切到 `playwright_cli`
- 不同 runtime 的底层能力仍有差异，但上层会尽量通过能力模型和 fallback 统一行为
- 对真实 Profile 的占用治理仍然优先于“强行复用”

## 三种引擎的当前定位

### `selenium_uc`

- 当前项目里最成熟、最稳的一条路径
- keepalive 仍然使用这条引擎链
- 如果你的目标是“尽量像真人、尽量减少自动化暴露”，优先考虑它
- 在代码层它仍然是兜底默认值

### `patchright`

- 更偏重复杂页面的结构化诊断与调试能力
- 对 snapshot/ref、多标签上下文、DevTools 风格观测更有优势
- 不适合作为所有高频普通任务的默认执行引擎

### `playwright_cli`

- 当前这台机器上适合作为普通 MCP 任务的默认高性能引擎
- 在新的按 Profile live 架构下更适合作为默认执行引擎
- stealth 不如 `selenium_uc`
- 原生观测能力不如 `patchright`，但受管运行时已经补了不少统一 fallback
- 简单 selector 的 `click` / `fill` 会优先走 fast DOM eval path，失败后再回退 `playwright-cli` 原生命令，以降低高频动作延迟
- console/network 诊断会标注第三方、静态资源、媒体片段、CSP/CORS、auth 等噪声分类，便于区分真正问题和常见站点噪声
- 已对 upstream `playwright-cli` 的 Chromium 启动参数做清洗，避免通过 `--disable-blink-features` 注入 `AutomationControlled`
- 默认遵守 `mcp.start_minimized=true`，可见 MCP 浏览器会先最小化到任务栏，避免抢占桌面焦点，同时用户需要时仍可点开窗口接管或观察
- 默认保持 `mcp.headless=false`；headless 只用于用户明确要求的回归测试或后台验证，不作为普通 MCP 浏览任务的默认方案
- session 关闭时会尝试清理归属的 `playwright-cli` daemon、Chromium 子进程和隔离 runtime 目录；启动时也会清理未被进程占用的空目录、过期目录和超出保留数量的 `chromium-advanced-playwright-cli-*` 临时目录，降低残留窗口与残留目录风险

## 开发说明

如果你打算继续扩展这个项目，建议优先遵守以下原则：

- Profile 身份治理必须继续由 `SessionManager` 统一控制
- 新 runtime 应先声明能力，再接入动作内核
- 不要把引擎内部报错直接暴露为上层产品语义
- 优先补通用能力，不做站点特化 adapter
