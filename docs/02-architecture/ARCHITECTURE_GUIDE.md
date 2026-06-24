# MCP Chromium Advanced Architecture Guide

## 目标

MCP Chromium Advanced 的核心目标不是“再造一个通用浏览器自动化框架”，而是提供一套围绕真实 Chromium 身份、显式占用治理、可切换执行引擎和桌面可控入口的本地浏览器自动化系统。

系统设计围绕四件事展开：

- 一个 `Profile N` 代表一个长期存在的真实身份
- GUI、daemon、worker 共享同一份配置和 profile 元数据
- 会话占用治理必须先于浏览器引擎执行
- 浏览器能力实现可以替换，但对上层暴露统一契约

## 分层结构

### 1. GUI 入口层

主要文件：

- `chromium_advanced/chromium_manage_gui.py`
- `chromium_advanced/gui/gui_runtime.py`
- `chromium_advanced/gui/gui_state.py`
- `chromium_advanced/gui/gui_widgets.py`
- `chromium_advanced/gui/gui_dialogs.py`
- `chromium_advanced/gui/gui_mcp_runtime.py`

职责：

- 组装桌面 UI
- 绑定事件、刷新表格、展示日志和状态
- 调用 control API / daemon 状态接口
- 执行 GUI 侧的轻量状态映射和视图模型组装

约束：

- GUI 主文件只应保留页面装配、事件调度和跨模块协调
- MCP/control 的 URL、鉴权、守护进程启停、状态轮询不应继续内联回 `chromium_manage_gui.py`
- 新的 GUI 行为优先下沉到 `chromium_advanced/gui/` 子模块

### 2. Shared Core 层

主要文件：

- `chromium_advanced/chromium_profile_lib.py`
- `chromium_advanced/keepalive_registry.py`

职责：

- 默认配置构建与归一化
- 路径、资源和运行目录解析
- profile 根目录管理
- Chromium 启动辅助
- 书签模板初始化
- keepalive 站点注册、保活插件发现、图标缓存和状态格式化
- 浏览器扩展目录解析、profile 级扩展关联解析

当前边界：

- `chromium_profile_lib.py` 继续作为 shared core 总入口
- keepalive 插件注册与元数据装载已显式进入 `keepalive_registry.py`
- 浏览器扩展目录和 profile 关联仍由 shared core 提供统一解析
- 这层应只保留“共享规则”和“共享工具”，不应继续吸收 GUI 或 daemon 专属流程

### 3. 治理与服务层

主要文件：

- `chromium_advanced/session_manager.py`
- `chromium_advanced/session_runtime_view.py`
- `chromium_advanced/session_runtime_cleanup.py`
- `chromium_advanced/mcp_daemon.py`
- `chromium_advanced/mcp_server.py`

职责：

- profile 级占用治理
- 会话创建、复用、释放和回收
- daemon 状态暴露
- worker 生命周期编排
- control API 和 MCP API 对外服务

当前边界：

- `SessionManager` 负责治理规则和生命周期编排
- active-session 视图投影转入 `session_runtime_view.py`
- runtime 进程清理转入 `session_runtime_cleanup.py`
- daemon 负责服务编排，不直接承担浏览器能力实现

### 4. 浏览器能力层

主要文件：

- `chromium_advanced/browser_session_kernel.py`
- `chromium_advanced/browser_session_kernel_watchers.py`
- `chromium_advanced/browser_engines/`

职责：

- 把 raw runtime 适配成统一浏览器能力契约
- 对高层工具输出统一结果、错误和诊断结构
- 提供 managed fallback
- 承接多引擎差异

当前结构：

- `browser_session_kernel.py`
  统一高层能力入口
- `browser_session_kernel_watchers.py`
  页面稳定/观察类 fallback
- `browser_engines/*_engine.py`
  引擎高层入口
- `browser_engines/*_tabs_pages.py`
  tab/page 能力域拆分

当前引擎定位：

- `official_playwright_mcp`
  默认高层浏览器能力路径，优先对齐官方 Playwright MCP 语义
- `patchright`
  兼容/回退路径，保留强交互与历史行为兼容性
- `selenium_uc`
  反自动化、challenge、XY/gesture 能力优先
- `playwright_cli`
  轻量兼容路径，不作为最高保真结构化读取主路径

## 会话与状态流

标准 MCP 生命周期：

1. 查询 daemon/server 状态
2. 查询目标 profile 是否可启动
3. 由 `SessionManager` 申请 profile 占用
4. 通过 engine factory 创建 raw session
5. 用 `ManagedBrowserSession` 暴露统一能力
6. 结束后释放 session 与占用

关键原则：

- 占用治理发生在 engine 启动前
- 同一 profile 是否可并发由治理层决定，不由引擎自行绕过
- GUI 手动打开、keepalive、MCP、未来脚本接入都必须落到同一套 profile occupancy 语义
- Chromium 浏览器扩展与保活插件是两套独立概念，配置、控制 API、GUI 管理入口都必须分离

## 为什么要做这轮分层

此前的主要问题不是“文件长”，而是几个入口文件同时承担：

- 状态编排
- 规则判断
- 底层执行
- 结果整形
- fallback 兼容

这会带来三类后果：

- 修改影响面大
- 测试无法映射真实边界
- 每次加功能都会把复杂度吸回入口文件

本轮重构的目标不是机械拆文件，而是把这些边界稳定下来：

- GUI 入口层只做 UI 编排
- 治理层只做会话/占用规则
- 能力层只做浏览器能力与 fallback
- Shared Core 只做共享规则和共享工具

## 测试分层

当前默认测试按 `tmp/tests` 分层：

- `tmp/tests/unit/`
  纯 helper、纯状态映射、纯 shared-core 逻辑
- `tmp/tests/integration/`
  `SessionManager`、daemon route、kernel 集成
- `tmp/tests/contract/`
  引擎/managed contract 稳定性
- `tmp/tests/smoke/`
  轻量 smoke
- `tmp/tests/slow/`
  本地重运行时、重依赖验证，不进入默认 `pytest -q`
- `tmp/tests/manual/`
  发布前人工脚本与一次性验证脚本

默认约定：

- `pytest -q` 只跑快速、稳定的默认层
- 慢测试和手工验证不得混入默认回归

## 当前仍保留的演进方向

这轮完成后，结构已经进入“可继续演进”的状态，但还有明确后续方向：

- 继续瘦身 `chromium_manage_gui.py`
- 继续从 `browser_session_kernel.py` 抽离结果整形/诊断 helper
- 继续把 engine 按 `targets_actions`、`diagnostics_traces` 等能力域拆开
- 继续收紧 `chromium_profile_lib.py` 与 shared-core 的边界

这些属于结构持续演进，不再是“入口职责完全混杂”的状态。
