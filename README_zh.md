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
   共享的 Profile 管理与会话占用逻辑保持不变，浏览器执行后端当前支持 `selenium_uc`、`patchright`、`playwright_cli`，以及基于官方 `@playwright/mcp` runtime 的 `official_playwright_mcp`。
4. 它对上层暴露的是更稳定的运行时契约，而不是原始引擎差异。
   受管会话内核会补充结构化能力描述、统一错误码和通用 fallback，减少调用方直接面对引擎差异的概率。
5. 它的核心设计是“安全占用真实身份”。
   会话检查会避免多个 live root 任务、线程或 keepalive 作业同时抢占同一个已登录浏览器身份；并发能力建立在统一治理规则之上，而不是绕过锁。
6. 它连接的是真实 Chromium Profile。
   项目会用真实的 `user-data-dir` 和 `profile-directory` 启动浏览器，再由选定执行引擎接入这个持久化 Profile。
7. 它除了 MCP 控制，还支持保活工作流。
   GUI 可以对真实登录的 Profile 执行手动或定时保活任务，目标站点可以包括 ChatGPT、Gmail、Google、GitHub，未来也可通过插件扩展。

重要账号边界：Chromium 的 `Profile N` 是浏览器数据容器，不是所有网站共用的账号。GUI 里的 `Account` 字段只是人工维护的标签或备注，只能作为线索，不能当作 GitHub、YouTube、ChatGPT、Gmail、Google 等目标网站当前登录账号的证明。涉及账号正确性的自动化任务，必须进入目标网站后读取该网站自己的登录状态再继续。

重要提取边界：项目刻意保持通用、开源、可复用，不会在核心代码里为 Gmail、YouTube Studio、GitHub 等站点写死专用 DOM 适配器。当前受管运行时已经补上了更安全的 `run_script` 序列化、通用 DOM fallback 和 page-text fallback，但在复杂动态前端上，`playwright_cli` 的结构化提取质量仍可能弱于 `patchright`。如果任务核心在于高保真结构化抽取，而不是一般性的浏览、点击、填写、截图，应优先切到 `patchright`。

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
- 共享的 Profile 占用注册表与事件流
- 会话启动与释放接口
- 过期脚本租约或陈旧锁的回收 / 恢复原语
- 稳定 daemon + 懒启动 worker 结构
- GUI 内的开关、日志和状态展示
- 统一能力模型、统一错误语义与通用 fallback

## 运行控制与安全

daemon 现在把“业务调用面”和“管理调用面”分开了。

- `mcp.api_token`
  给普通 MCP 客户端和 daemon automation 调用方使用，用于正常浏览器业务请求。
- `control.api_token`
  只用于 GUI / control 端点，例如 dashboard、日志、keepalive 状态、插件管理和 worker 生命周期控制。

安全边界：

- `control.api_token` 与 `mcp.api_token` 是刻意分离的。
- 如果没有配置 `control.api_token`，`/_control/*` 端点会直接保持禁用，而不是静默回退接受 MCP token。
- GUI 与 daemon 在需要初始化 token 时，会分别生成独立的 `mcp.api_token` 和 `control.api_token`。

当前端点边界：

- MCP / 业务面：
  - `/mcp`
  - `/_daemon/status`
  - `/_daemon/profiles`
  - `/_daemon/profiles/{profile_name}`
  - `/_daemon/automation/*`
- GUI / control 面：
  - `/_control/ping`
  - `/_control/status`
  - `/_control/dashboard`
  - `/_control/profiles`
  - `/_control/profiles/{profile_name}`
  - `/_control/sessions`
  - `/_control/events`
  - `/_control/keepalive`
  - `/_control/logs`
  - `/_control/log-settings`
  - `/_control/plugins`
  - `/_control/profiles/{profile_name}/plugins`
  - `/_control/service/worker/start`
  - `/_control/service/worker/stop`

worker 运行策略现在也是显式可配的：

- `lazy`
  空闲超时后尽快回收 worker。
- `sticky`
  默认值。为真实业务流量保留更长寿命，减少频繁启停。
- `always_on`
  不因为空闲超时回收 worker。

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
- 在支持的引擎上提供正式的坐标/手势交互工具，例如 `browser_mouse_move_xy`、`browser_mouse_click_xy`、`browser_mouse_drag_xy`
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

- `patchright`
  适合作为普通 MCP 任务默认引擎。结构化提取更强、诊断能力更完整，也更符合当前受管能力层的设计方向。
- `selenium_uc`
  更适合风控敏感站点、登录站点、challenge/伪装优先场景。目前项目里它仍然是 stealth 最强的一条路径。
- `playwright_cli`
  适合作为低开销、轻量级、兼容性导向的集成引擎，但不再是默认的高能力路径。
- `official_playwright_mcp`
  当前默认的受治理高层引擎。它使用内置 Node.js 与内置 `@playwright/mcp` runtime，通过 `isolated_runtime` 物化运行时接入，不直接持有 live-root 持久 Profile。

当前这轮对齐官方 MCP 的收口重点，已经不只是“多几个动作”，而是把动作结果语义也统一下来：

- tab 生命周期：
  `open_tab(...)`、`activate_tab(...)`、`close_tab(...)` 现在会稳定回填 `opened`、`activated`、`closed`、`active_tab_id`、`closed_tab_id`、`tab_count`
- wait 语义：
  `wait_for(...)`、`wait_for_timeout(...)` 现在会稳定暴露 `condition`、`by`、`waited`、`timeout_ms`
- 验证型输入：
  `type_target_and_verify(...)` 现在会统一保留 `target`、`requested_target`、`by`、`value`、`verified`，更方便上层推理和诊断
- 对话框处理：
  `handle_dialog(...)` 现在已经是正式动作能力，而不是只能依赖临时脚本兜底
- 文件上传：
  `file_upload(...)` 现在已经作为正式动作暴露，减少 `<input type="file">` 场景的调用方适配成本

这一轮还补上了更贴近官方 `playwright-mcp` 命名习惯的兼容别名工具，降低 agent/tool routing 适配成本：

- `browser_tabs`
  官方风格 tab 管理入口，支持 `action=list|new|select|close`
- `browser_take_screenshot`
  等价于 `screenshot`
- `browser_close`
  等价于 `close_profile_session`
- `browser_handle_dialog`
  官方风格的对话框处理入口
- `browser_file_upload`
  官方风格的文件上传入口
- `browser_resize`
  官方风格的浏览器窗口尺寸调整入口
- `browser_network_request`
  官方风格的单条网络请求详情读取入口，按 1-based `index` 取值

同时，`browser_diagnose_page(...)` 的 `structured_page` 也已经从“偏文本/评论提取”扩成更通用的页面结构模型，可以概括：

- interactive controls
- form controls
- custom element 预览
- dialog / menu / listbox / tab 密度
- 当前交互区域提示，例如 `overlay`、`dialog`

现在这一层又进一步泛化了。`structured_page` 还会尽量补充：

- 可能的主动作控件，例如 save / apply / open / run 一类操作
- 搜索与筛选类控件
- 导航型控件，例如 link、tab
- 列表 / 表格 / 线程型页面信号
- 集合摘要，例如评论线程、消息列表、仓库列表、通用结果列表
- 工具栏控件与状态面，便于驱动后续一步动作
- 轻量 role 密度与交互标签预览

对于局部目标诊断，`browser_diagnose_target(...)` 现在也会返回更丰富的 `structured_region`，包括：

- `region_kind`
- `interactive_controls`
- `visible_controls`
- `overlay_controls`
- `dialog_controls`
- `interactive_density`
- `primary_actions`
- `search_like_controls`
- `status_controls`
- `role_counts`

如果任务关注的是某一个动态控件、弹层、筛选菜单、状态区块或局部面板，应该优先读 `structured_region`，而不是先退回整页文本。

当前受管验证结果也更统一了：

- `browser_verify_text(...)`、`browser_verify_dialog(...)`、`browser_verify_element(...)` 会统一补 `verified`、`matched`
- `browser_verify_target_value(...)`、`browser_verify_target_visible(...)` 也会统一补 `verified`、`matched`、`target`、`by`
- `browser_describe_target(...)`、`browser_list_candidates(...)` 会补一个轻量的 `target_summary`
- `browser_list_candidates(...)` 返回的候选项现在还会补 `match_reason` 和 `ranking_reason`，方便调用方理解为什么某个候选排在最前面
- `run_script_batch(...)` 现在除了逐项结果，还会统一返回 `ok_count`、`error_count`、`all_ok` 和 `first_error`

在默认 `patchright` 主路径上，高频成功动作现在也会尽量留下更高信号的 `post_action_context`，而不只是一个最小页面指针。常见情况下，返回结果里会更容易直接带上：

- 有界 `snapshot`
- 推导后的 `structured_page`
- 轻量 `interaction_hints`
- 最近动作与会话健康上下文

这样做的目的，是减少调用方在普通多步流程里，刚完成一步又立刻补一个 `browser_snapshot(...)` 或 `browser_diagnose_page(...)` 才能继续的情况。

在默认 `patchright` 主路径上，候选排序现在也更偏语义优先，而不只是接近 DOM 顺序。弹层项、搜索/筛选控件、可能的主动作控件会获得更强的排序信号，复杂前端里的后续一步命中率会更高。

这一轮还补上了“最近结构化上下文驱动后续候选排序”的能力。实际表现是：

- 最近一次 `structured_page` 与 `interaction_hints` 会缓存在受管 session 内
- 后续候选解析会优先参考当前交互区域，例如 `overlay`、`dialog`
- 集合型页面会优先沿着当前集合类型继续推断，例如 `comment_threads`、`message_list`、`repository_list`、`result_list`
- 上一步提取到的工具栏 / 筛选 / 搜索 / 状态标签，也会参与下一步候选排序，而不是每一步都立刻退化成整页盲扫

目标就是让复杂前端里的 follow-up 操作更少盲试、更少全页探测。

对于强动态页面，这一轮还补了两类更通用的等待能力：

- `wait_for_page_stable(...)`
  适合页面还在持续重渲染、文本和 HTML 长度还在变化的场景
- `wait_for_text_change(...)`
  适合等待状态文本、任务文本、区域文本从旧值变成新值，而不是只判断“有没有出现”
- `watch_page_state(...)`
  适合一次性拿到初始文本、等待变化、再等待页面稳定，并返回前后状态与 diff 摘要
- `watch_target_state(...)`
  适合盯住单个控件、状态位、菜单项或局部目标区域，等待其文本或状态变化后再稳定下来

另外，如果 `run_script(...)` 返回 `result=null`，受管运行时现在会显式补上 `script_result_state="null"` 和诊断提示，不再把这种情况和普通成功混在一起。

关于切换引擎，有几个必须明确的边界：

- 修改 GUI 默认引擎，只影响之后新启动的会话。
- 已经运行中的 session 不会被热切换。
- `reuse_existing=true` 只会复用“同一个 Profile + 同一个引擎”的会话。
- 用不同引擎再开一个会话，不会把已有会话热切换过去；已经启动的 session 会保持原来的引擎。

## 受管自动化脚本

除了 MCP 之外，项目现在还支持第二条正式消费路径：受管本地脚本。

推荐模型是：

```text
固定脚本 -> AutomationRunner -> SessionManager -> BrowserEngine -> 真实 Chromium Profile
```

这样做的目标是：固定 Python 自动化脚本不应该绕过治理，直接拿真实 `user-data-dir` 自己启动浏览器。

使用 `AutomationRunner` 或 daemon automation API 时，可以获得：

- 与 MCP 相同的 Profile 锁和占用治理
- `automation` 场景下的统一占用登记
- 当同一 Profile 已被 MCP、GUI 手工启动或 keepalive 占用时，脚本会被阻止
- 统一的会话释放与异常回收路径

当前 daemon 也提供了固定脚本可直接接入的 HTTP 入口：

```text
固定脚本 -> daemon HTTP API -> SessionManager -> BrowserEngine -> 真实 Chromium Profile
```

主要端点包括：

- `POST /_daemon/automation/acquire`
- `POST /_daemon/automation/action`
- `POST /_daemon/automation/heartbeat`
- `POST /_daemon/automation/release`

推荐脚本调用策略：

1. 先获取一个 session
2. 在整个任务期间复用同一个 `session_id`
3. 用 `action` 连续执行多个步骤
4. 长任务中按需发送 heartbeat
5. 任务结束后再统一 release

不要把 `acquire -> 执行一步 -> release` 当成每个小动作的包装器，这会增加启动/关闭开销，也更容易引入会话抖动。

参考文档：

- [Daemon Automation Integration](./docs/DAEMON_AUTOMATION_INTEGRATION.md)

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
  默认浏览器执行后端，目前支持 `selenium_uc`、`patchright`、`playwright_cli`、`official_playwright_mcp`
- `app.concurrency_mode`
  会话并发治理模式。当前默认 `per_profile_live`，允许不同 Profile 并发，但同一个 Profile 保持互斥
- `mcp.host` / `mcp.port` / `mcp.worker_port` / `mcp.path`
  daemon 与 worker 的网络配置
- `mcp.api_token`
  可选 Bearer token。配置后，所有 MCP / 业务 daemon 请求都必须带 `Authorization: Bearer <token>`，不会因为是 localhost 而豁免
- `control.api_token`
  GUI / control 端点专用 bearer token。可与 `mcp.api_token` 不同，用于 `/_control/*` 下的状态、日志、keepalive、插件和 worker 控制操作
- `mcp.worker_policy`
  worker 生命周期策略，合法值为 `lazy`、`sticky`、`always_on`。当前默认 `sticky`

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

- 如果配置了 `mcp.api_token`，每个 MCP / 业务 daemon 请求都必须带 `Authorization: Bearer <token>`。没有 localhost 特殊豁免。
- 如果配置了 `control.api_token`，每个 `/_control/*` 请求都必须带 `Authorization: Bearer <token>`。这里也没有 localhost 特殊豁免。
- `mcp.api_token` 不能调用 `/_control/*`，`control.api_token` 也不能调用 MCP / 业务路由。
- GUI 自己轮询 daemon 状态和控制面时，会走独立的 `/_control/*` + `control.api_token` 规则；外部 MCP 客户端则只走 MCP / 业务面。
- daemon 设计上是稳定常驻的，worker 则是短生命周期、按需启动。
- worker 生命周期由 `mcp.worker_policy` 控制。默认 `sticky` 更适合真实业务流量，可减少“做一步就启停一次”的抖动。
- 因 `idle_timeout` 被回收的 worker 属于正常托管生命周期事件，不是 crash。
- 如果配置的 Chromium 根目录里已经有 live browser process，session startup 会被有意阻断，并报告 `external_chromium_running` 等状态。
- 这套 busy-state 规则现在也是按 profile root 生效，`playwright_cli` 也遵循同样治理规则。
- MCP tools 会发布标准 tool annotations，便于支持该能力的客户端区分“可信的本地 / 浏览器操作”和“任意脚本执行”。
- 这些 annotations 可以减少不必要的审批提示，但不会绕过客户端本身的审批策略，也不会绕过本项目的 profile / busy-state 治理规则。
- 正常的 profile/session 操作、导航、tab 操作、点击、输入、按键、鼠标、截图、诊断和清理都被标成可信的低风险 MCP 操作。
- `run_script` 和 `run_script_batch` 明确保留为高信任、非只读动作，因为它们会在真实登录态浏览器里执行任意 JavaScript。

典型 MCP 生命周期：

1. `list_profiles`
2. `get_server_status`
3. `get_profile_status(profile_name)`
4. `can_start_profile_session(profile_name)`
5. `start_profile_session(profile_name)`
6. 执行浏览器动作
7. `close_profile_session(session_id)`

对于一个包含很多步骤的用户任务，应该尽量在整个任务期间复用同一个 session，而不是每一步都重复：

```text
start_profile_session -> 做一步 -> close_profile_session
```

只有当任务本身明确要求隔离时，才应该这样做。

如果需要显式指定引擎，可以这样调用：

```text
can_start_profile_session(profile_name="Profile 4", engine="selenium_uc")
start_profile_session(profile_name="Profile 4", engine="playwright_cli")
```

如果你是手动配置 MCP 客户端，同样必须在每个请求上附带 bearer token。例如 Codex 配置可以写成：

```toml
[mcp_servers.browserIdentity]
url = "http://127.0.0.1:28888/mcp"

[mcp_servers.browserIdentity.http_headers]
Authorization = "Bearer <token>"
```

## 调试与可观测性

worker 暴露了正式的多标签工具与调试工具，减少依赖人工盯着浏览器窗口的需要。

多标签相关：

- `browser_list_tabs`
- `browser_open_tab`
- `browser_activate_tab`
- `browser_close_tab`
- 不要把这套基于 `session_id` 的工具和另一套 MCP 浏览器服务混用，例如通用的 `mcp:playwright/*`。`browserIdentity` 返回的会话只属于本服务。

兼容官方风格命名的别名入口：

- `browser_tabs`
- `browser_take_screenshot`
- `browser_close`
- `browser_resize`
- `browser_network_request`

与常规页面交互相关的新正式工具：

- `browser_handle_dialog`
- `browser_file_upload`

其中 `browser_tabs` 不再只是 list 别名，而是支持：

- `action="list"`
- `action="new"`，可带 `url`
- `action="select"`，通过 `index` 切换
- `action="close"`，通过 `index` 关闭

对于网络诊断，这一层现在同时支持：

- `browser_get_network_requests`
  更偏列表/调试输出
- `browser_network_request`
  更贴近官方风格的单条请求详情读取，按 1-based `index` 取值

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
- 也是当前最适合手势解锁、滑块拖动、图形验证码轨迹、坐标级 fallback 的引擎之一
- 在没有显式配置引擎时，代码层 fallback 默认仍然偏向它

### Patchright

- 已支持通过 MCP / session 层接入真实持久化 Profile
- 启动参数比 Selenium 更克制，用于兼容性
- 适合 Playwright 执行模型更可靠的网站
- 在当前项目中提供最强的 tab 模型和结构化调试能力
- 能通过每个 tab 的 CDP session 收集 DevTools 风格诊断信息
- 支持正式的坐标/手势工具族
- 当前阶段 keepalive 还没有走 Patchright

### Playwright CLI

- 轻量级集成兼容引擎，不是默认的高能力主路径
- 一旦从这里启动了会话，后续 tab / click / type / snapshot / diagnose 等动作都应该继续走本服务；同一个任务再切去别的浏览器 MCP，是调用侧/agent 的集成错误，不是支持的模式。
- 更适合当前 per-profile live 运行时下的低开销任务执行
- 原生 stealth 弱于 `selenium_uc`
- 原生 inspection fidelity 弱于 `patchright`，但受管运行时会用 fallback、diagnostics 和恢复元数据把它补强
- 它作为与其它引擎并行的第三种实现，接在同一条 `SessionManager -> BrowserEngine factory` 路径之下
- 启动时用 `playwright-cli open --persistent`，之后复用 named session 做后续命令
- 复用真实 `user-data-dir` 与 Chromium `--profile-directory=Profile N`，所以可以保留登录态
- 当前已覆盖的稳定能力包括：会话启动、导航、多标签基础、脚本执行、type / click / key、截图、console、requests 和粗粒度页面诊断
- 受管运行时为它补充通用 `snapshot`、候选元素枚举、等待、target 验证与 snapshot-ref 风格定位
- 简单 selector `click` / `fill` 会优先走快速 DOM eval path，失败时再退回原生命令
- `run_script` 会优先走更安全的结果序列化包装；如果直接结构化抽取结果为空，通用文本提取会退回到 bounded DOM chunk / page-text fallback
- 即便如此，在复杂动态前端上结构化提取仍可能出现噪音较大、保真度不够高的情况；如果任务依赖高质量结构化读数，而不是页面文本兜底，应优先切到 `patchright`
- 会把 console / network 噪音分类，例如 third-party、asset、media、security policy、CORS、auth
- 会清洗上游 Chromium launch args，避免通过真实 `--disable-blink-features` 注入 `AutomationControlled`
- 默认遵守 `mcp.start_minimized=true`，可见 MCP 浏览器会最小化停在任务栏，而不是抢前台焦点
- 默认保持 `mcp.headless=false`；headless 只用于用户明确要求的回归 / 后台验证
- 支持 `runtime_options.incognito=true`，用于“同一个 Profile 治理路径下的无痕验证”场景，例如想验证未继承常规标签页会话状态的流程
- 关闭 session 时会尝试清理自有 `playwright-cli` daemon / Chromium 进程，并回收隔离运行时目录；启动时也会清理不再被活进程引用的陈旧临时目录
- 共享 root 运行时现在仅作为迁移兼容布局，正常运行应使用 `paths.user_data_profiles_root`
- 当前还不支持正式的坐标/手势工具族；如果任务依赖拖拽、滑块、图案解锁或坐标级 fallback，应切到 `selenium_uc` 或 `patchright`
- 当前阶段 keepalive 还没有切到 `playwright_cli`
- Windows 打包后的 GUI、daemon、worker EXE 已在受管运行时路径上完成验证

## Keepalive 插件

keepalive 站点现在走插件化运行时。内置站点包括 `chatgpt`、`google`、`gmail`、`github`；外部 Python 插件可以不重编译程序就新增 `youtube`、`youtube_studio` 或其它站点。桌面 GUI 也已经有专门的 Keepalive Plugins 页面，用于查看内置插件源码、创建外部插件、编辑受信任本地插件。

- [Keepalive Plugin Guide](./docs/KEEPALIVE_PLUGIN_GUIDE.md)

## Skill 模板

仓库包含可复用的 agent skill 模板：

- `docs/skill_templates/`

这些文件是给 Codex 或其它 AI 工作流复用的模板，不是程序自动装载的运行目录。

skill 模板里应明确告诉 agent：

- `patchright` 是普通 MCP 任务的默认引擎，尤其适合结构化提取和复杂前端诊断。
- `selenium_uc` 适合 stealth、反自动化压力较高、反复 challenge、手势/拖拽/坐标 fallback 等场景。
- `playwright_cli` 是轻量级集成引擎，适合低开销流程，但不应被描述成默认的高能力路径。
- `official_playwright_mcp` 目前只是实验性后端名，除非发布说明明确写明已正式启用，否则不应用于正常 live Profile 任务。
- 如果需要在同一个 Profile 治理路径下做隔离验证，可以使用 `runtime_options.incognito=true`。

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

## 最新发布验证

- `2026-06-19` 已基于安装目录 `D:\softs\chromium\ChromiumProfileManager` 完成最新一轮打包版发布验证
- 打包版 GUI 启动后能够稳定存活，并能正确拉起打包版 daemon / worker 运行时
- 打包版运行时下，`get_server_status()` 已确认默认引擎为 `patchright`
- 真实登录态验证已通过：`Profile 1` 可正常进入 Gmail Inbox
- 并行验证已通过：`Profile 1` 与 `Profile 4` 可同时建立独立 MCP 会话而不发生状态串扰
- 无痕验证已通过：可通过 daemon automation 路径配合 `runtime_options.incognito=true` 完成隔离启动与释放
- 清理验证已通过：所有会话释放后，服务状态会回到 `idle`

## 当前已确认边界

- 复杂页面下，当前最可靠的验收与读取面仍然是高层结构化路径：`structured_page`、`browser_list_candidates(...)`、`browser_get_interaction_context(...)`、action trace 与 screenshot
- `run_script(...)` 在部分真实动态页面上仍可能出现“执行成功但 `result=null`”的情况，这应被视为当前读回边界，而不是自动判定为脚本执行失败
- 如果任务依赖复杂动态前端上的高保真结构化提取，应优先使用默认的 `patchright` 路径，并优先调用高层结构化工具，而不是直接把原始脚本执行结果当作唯一读数面
- 标准浏览器核心验证手册见 `docs/BROWSER_CORE_VALIDATION_PLAYBOOK.md`，其中同时定义了大版本发布验证剧本与小迭代 smoke 剧本
- 本地空闲运行时测量可使用 `python scripts/measure_idle_runtime.py`
- 当前的产品目标不是“空闲永远 0 CPU”，而是让空闲占用可度量、可解释、可跨版本比较

## License

This project is licensed under the MIT License. See [LICENSE](./LICENSE).
## Daemon 自动化接入

如果调用方不是 MCP/Agent，而是固定的本地脚本或服务，现在推荐走 daemon 托管自动化接口，而不是直接自己占用浏览器 profile。

专门接入文档见：

- [docs/DAEMON_AUTOMATION_INTEGRATION.md](./docs/DAEMON_AUTOMATION_INTEGRATION.md)
