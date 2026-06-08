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
   会话检查会避免多个任务、线程或 keepalive 作业同时抢占同一个已登录浏览器身份。

6. 它连接的是真实 Chromium Profile。  
   项目会用真实的 `user-data-dir` 和 `profile-directory` 启动浏览器，再由选定执行引擎接入这个持久化 Profile。

7. 它除了 MCP 控制，还支持保活工作流。  
   GUI 可以对真实登录的 Profile 执行手动或定时保活任务，目标站点可以包括 ChatGPT、Gmail、Google 和 GitHub。

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
  保存所有持久化 Profile 的根目录
- `app.browser_engine`
  默认浏览器执行后端，当前支持 `selenium_uc`、`patchright`、`playwright_cli`
- `mcp.*`
  MCP daemon 与 worker 的地址、端口、超时与日志级别
- `profiles[]`
  Profile 名称、账号标识、备注、保活配置等

## MCP 使用方式

推荐的 MCP 生命周期如下：

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. 执行浏览器操作
7. `close_profile_session(session_id)`

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

## 构建

Windows 下可使用：

```powershell
.\build_chromium_manage_gui_exe.ps1
```

它会构建：

- `ChromiumProfileManager`
- `ChromiumMcpDaemon`
- `ChromiumMcpWorker`

## 当前已知边界

- keepalive 目前仍未切到 `playwright_cli`
- 不同 runtime 的底层能力仍有差异，但上层会尽量通过能力模型和 fallback 统一行为
- 对真实 Profile 的占用治理仍然优先于“强行复用”

## 开发说明

如果你打算继续扩展这个项目，建议优先遵守以下原则：

- Profile 身份治理必须继续由 `SessionManager` 统一控制
- 新 runtime 应先声明能力，再接入动作内核
- 不要把引擎内部报错直接暴露为上层产品语义
- 优先补通用能力，不做站点特化 adapter
