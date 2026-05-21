# MCP Chromium Advanced

MCP Chromium Advanced 是一个用于管理真实 Chromium 浏览器 Profile 的桌面 GUI 和 MCP 服务。它面向的不是一次性、无状态的自动化浏览器，而是需要复用真实登录态浏览器身份的工作流。

[English README](./README.md)

## 项目概述

这个项目可以理解为“真实 Chromium 身份管理器 + MCP 浏览器服务”。它并不是每次都新建一个一次性自动化浏览器，而是为了让 AI 工作流能够安全复用已经登录过的真实浏览器 Profile。

如果是第一次接触这个项目，可以先抓住 6 个关键点：

1. 它解决的是“真实登录态复用”问题。
   GUI 管理的 Chromium Profile 可以暴露给 MCP 客户端使用，让自动化流程直接复用 cookie、local storage、扩展、书签和站点权限。
2. 它的结构分成三层。
   GUI 负责配置和 Profile 管理，daemon 提供稳定的 MCP 入口，worker 按需启动并真正控制浏览器会话。
3. 它支持多种浏览器执行引擎。
   共享的 Profile 管理和会话占用逻辑保持不变，而浏览器执行后端目前可以选择 `selenium_uc` 或 `patchright`。
4. 它的核心设计是“安全占用真实身份”。
   会话检查会避免多个任务、线程或保活作业同时抢占同一个已登录浏览器身份。
5. 它连接的是真实 Chromium Profile。
   项目会用真实的 `user-data-dir` 和 `profile-directory` 启动浏览器，再通过选定的浏览器引擎接入这个持久化 Profile。
6. 它除了 MCP 控制，还内置了保活工作流。
   GUI 可以对真实登录态 Profile 执行手动或定时保活任务，目标站点包括 ChatGPT、Gmail 和 Google。

公开入口仍然是：

```bash
python run_gui.py
```

## 软件截图

![软件截图](docs/imgs/ScreenShot.png)

## 原理说明

浏览器自动化部分当前支持：

- Selenium: [https://www.selenium.dev/](https://www.selenium.dev/)
- undetected-chromedriver: [https://github.com/ultrafunkamsterdam/undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver)
- Patchright: [https://github.com/Kaliiiiiiiiii-Vinyzu/patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)

项目会使用真实的 `user-data-dir` 和 `profile-directory` 启动 Chromium，再通过所选引擎接入这个浏览器上下文。这样 worker 可以复用真实的 cookie、登录态、local storage、扩展和其他持久化浏览器状态。

如果你需要指纹相关插件，也可以配合 `my-fingerprint` 使用：

- my-fingerprint releases: [https://github.com/omegaee/my-fingerprint/releases](https://github.com/omegaee/my-fingerprint/releases)

在这个浏览器控制层之上，项目再增加：

- Profile / 会话占用检查
- 会话启动和归还接口
- 稳定 daemon + 懒启动 worker 结构
- GUI 中的开关、日志和状态展示

## 主要能力

- 用一个 GUI 管理多个 Chromium Profile
- 通过 MCP 把真实浏览器身份暴露给客户端
- 避免多个线程或任务抢占同一个 Profile
- 在 GUI 中切换默认浏览器执行引擎
- 只有在需要时才启动浏览器 worker
- 在空闲超时后自动回收资源
- 对真实登录态 Profile 运行保活任务
- 通过显式的 tab 工具支持多标签页协作
- 直接采集结构化的 console、页面异常、网络请求诊断信息，而不只是依赖截图

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

在使用前，需要准备三类本地资源：

1. Chromium 或 Chrome 浏览器
2. 匹配版本的 `chromedriver`
3. 持久化的用户数据目录

常见选择包括：

- ungoogled-chromium
- Chromium
- Google Chrome

`chromedriver` 的主版本应尽量与浏览器主版本一致。

### 强烈推荐

本项目强烈推荐使用 `ungoogled-chromium`。

- 更适合长期、本地、稳定的自动化使用场景
- 不会频繁自动更新，能明显降低浏览器升级后导致 `chromedriver` 失效的问题
- 推荐优先使用 `136` 以下版本，因为更高版本在部分环境下可能存在 `ungoogled-chromium` 兼容性问题

官方下载地址：

- ungoogled-chromium: [https://ungoogled-software.github.io/ungoogled-chromium-binaries/](https://ungoogled-software.github.io/ungoogled-chromium-binaries/)
- Chrome for Testing / ChromeDriver: [https://googlechromelabs.github.io/chrome-for-testing/](https://googlechromelabs.github.io/chrome-for-testing/)

Driver 对版原则：

- `Chromium` 的主版本号一定要和 `ChromeDriver` 的主版本号对上
- 如果默认下载页里没有你需要的精确版本，可以通过修改 driver 下载地址中的版本号，找到与本机 Chromium 对应的版本
- 在 GUI 中填写路径前，先确认浏览器版本和 driver 版本已经匹配

## 配置文件

程序首次启动时，会在系统配置目录下创建配置文件：

- Windows: `%APPDATA%/ChromiumProfileManager/workstates/chromium_profiles.json`
- macOS: `~/Library/Application Support/ChromiumProfileManager/workstates/chromium_profiles.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/ChromiumProfileManager/workstates/chromium_profiles.json`

仓库中提供了脱敏模板：

- `chromium_profiles.example.json`
- `resources/bookmarks_template.html`

首次运行时，如果本地工作目录里还没有书签模板，应用会自动把仓库内置模板复制到默认位置。

关键字段说明：

- `paths.chromium_dir`
  浏览器可执行文件路径，或者包含它的目录
- `paths.chromedriver_path`
  `chromedriver` 路径，或者包含它的目录
- `paths.user_data_root`
  保存所有持久化 Profile 的根目录
- `paths.bookmarks_template_path`
  可选，初始化 Profile 时使用的书签模板
- `paths.fingerprint_zip_path`
  可选，与 `my-fingerprint` 相关的路径
- `app.language`
  界面语言代码，例如 `en`、`ja`、`zh`
- `app.browser_engine`
  默认浏览器执行后端，当前支持 `selenium_uc` 与 `patchright`
- `launch.*`
  内置 Python 启动器使用的浏览器启动默认项，例如 `new_window`、`start_maximized`、`load_fingerprint_extension`、`check_url`、`extra_args`
- `mcp.host`、`mcp.port`、`mcp.worker_port`、`mcp.path`
  daemon 与 worker 的网络设置

## MCP 服务

在 GUI 中启用后，daemon 会暴露稳定 HTTP 入口，例如：

```text
http://127.0.0.1:28888/mcp
```

daemon 会持续可用；真正持有浏览器的 worker 只会在请求需要时启动，并在空闲超时后回收。

典型 MCP 调用顺序：

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. 执行浏览器操作
7. `close_profile_session(session_id)`

如果调用方需要，也可以在启动 session 时显式传入 engine；如果省略，则使用 GUI 配置中的默认引擎。

### 多 tab 工具

当前 worker 已正式暴露多标签页工具：

- `browser_list_tabs`
- `browser_open_tab`
- `browser_activate_tab`
- `browser_close_tab`

推荐工作流是：

1. 先列出或打开目标 tab
2. 显式激活要操作的 tab
3. 在当前活动 tab 上继续页面操作
4. 需要切换时再次显式激活

同时，`navigate`、`get_current_url`、`get_page_text`、`get_page_html`、`browser_snapshot`、`browser_list_candidates`、`inspect_elements`、`run_script`、`screenshot` 等读取或诊断类工具，也支持可选的 `tab_id` 参数。

### 调试与观测工具

当前 worker 还额外提供了结构化调试工具，用来替代很多人工 F12 截图场景：

- `browser_get_console_messages`
- `browser_get_page_errors`
- `browser_get_network_requests`
- `browser_clear_debug_buffers`
- `browser_diagnose_page`

其中 `browser_diagnose_page` 适合作为代理卡住时的第一诊断入口：它会把当前交互上下文、最近 console 错误、页面异常、失败请求以及最近的 4xx/5xx 响应一起返回。

## 引擎说明

### 共享部分

- Profile 创建、删除、同步、书签初始化、会话占用规则，都是引擎无关的共享逻辑
- GUI 和 MCP 的会话流程不会因为切换引擎而改变
- 真实的 `user-data-dir` 和 `profile-directory` 仍然是唯一状态来源

### Selenium + undetected-chromedriver

- 当前仍然是项目里最成熟、覆盖最完整的执行路径
- 现有 keepalive 工作流也仍然基于这一套
- 直接启动 Profile 时，会使用共享的 `launch.*` 启动配置
- 现在也暴露了同一套多 tab 与调试工具，但 console / network 观测依赖 Chromium 日志能力，属于尽力而为
- 结构化可访问性快照与 snapshot ref 目标能力目前仍然是 Patchright 更完整

### Patchright

- 已经接入真实持久化 Profile，会通过 MCP / session 层启动和回收
- 目前使用比 Selenium 更保守的已验证启动参数集，以优先保证兼容性
- 更适合后续吸收 Playwright 风格能力的执行路径
- 当前在多 tab 模型和结构化调试观测方面也是项目里最完整的一条路径
- 调试信息通过每个 tab 的 CDP 会话采集，因此代理可以直接读取 console、未捕获异常和网络失败，而不必手动打开开发者工具
- 当前阶段 keepalive 还没有切到 Patchright

## 跨平台说明

这是一个 Python 项目，源码层面会尽量保持跨平台。

- Windows 是当前主要测试平台
- macOS 和 Linux 在提供正确浏览器与驱动路径后，源码级运行是支持的
- Windows 的桌面打包流程目前最完整

## Skill 模板

仓库中提供了可复用的 agent skill 模板，位置在：

- `docs/skill_templates/`

这些文件是给 Codex 或其他 AI 工作流复用的示例模板，不是程序运行时自动加载的目录。

典型用法：

1. 把合适的 `.SKILL.md` 复制到你的全局或项目级 skill 目录
2. 如果你的环境端口、主机或 profile 命名规则不同，先按实际情况修改
3. 在其他 AI 任务里明确要求 agent 使用这份 skill 来调用本项目的 MCP 服务

## 隐私与安全

- 不要把真实 `chromium_profiles.json` 提交到仓库
- 不要提交真实 Profile 数据、cookie、会话状态或个人账号标记
- 不要把 MCP 服务暴露给不可信网络
- AI 或 agent 不应猜测真实身份，应该显式使用 `profile_name` 或先询问用户

书签模板本身默认不视为敏感信息，只要内容是通用的，就可以保留在仓库中。

## 仓库结构

- `run_gui.py`
  唯一公开入口
- `chromium_advanced/chromium_manage_gui.py`
  桌面 GUI
- `chromium_advanced/mcp_daemon.py`
  稳定 daemon 服务
- `chromium_advanced/mcp_server.py`
  浏览器 worker 实现
- `docs/ARCHITECTURE_GUIDE.md`
  额外的实现说明
- `docs/skill_templates/`
  提供给 Codex 或其他 AI 工作流复用的 skill 模板，包含 Windows 和 WSL 示例
- `resources/bookmarks_template.html`
  项目内置的书签模板

## License

本项目使用 MIT License。见 [LICENSE](./LICENSE)。
