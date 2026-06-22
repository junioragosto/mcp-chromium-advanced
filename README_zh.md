# MCP Chromium Advanced

面向真实浏览器身份复用的桌面 GUI 与 MCP 服务。

MCP Chromium Advanced 不是一次性、无状态的自动化浏览器工具，而是一套用于管理和复用真实 Chromium Profile 登录态的本地系统。

版本与发布信息以后应以项目版本和打包产物中的 release metadata 为准，而不是在 README 中单独手工维护版本号。

- [English README](./README.md)
- [文档目录](./docs/README.md)

![Managed Real-Browser Identity Flow](./docs/assets/readme_value_flow.svg)

## 它是什么

这个项目把几件事放到了一起：

- 管理真实 Chromium Profile 的桌面 GUI
- 稳定常驻的 daemon 与按需启动的 worker
- 给 Agent 使用的 `/mcp`
- 给固定脚本使用的 `/_daemon/*`

核心思路很简单：

- 一个 `Profile N` 就是一个浏览器身份容器
- 所有入口都必须先经过统一治理层才能占用它
- 一旦占用成功，就能复用真实 Cookie、Local Storage、扩展和站点登录态

## 它解决什么问题

大多数浏览器自动化工具更适合一次性临时会话。

这个项目主要解决：

- 真实登录态复用
- Profile 身份显式治理
- GUI、MCP、keepalive、固定脚本之间不互相抢占
- 多引擎下的统一浏览器能力接口

重要账号边界：

- Chromium 的 `Profile N` 只是浏览器数据容器，不是所有网站通用账号
- GUI 里的 `Account` 字段只是人工标签
- 涉及账号正确性的自动化任务，必须进入目标网站后读取该网站自己的登录状态

## 主要价值

- 复用真实已登录 Chromium Profile
- 安全地把这些身份暴露给 MCP 客户端
- 用统一治理规则避免会话冲突
- 在多种浏览器引擎之上提供一致的高层能力
- 提供结构化诊断、trace 和统一动作结果
- 支持 daemon automation，固定脚本不必绕开系统自己抢 Profile
- 支持 keepalive 与插件化站点保活

## 引擎定位

![Engine Positioning](./docs/assets/readme_engine_stack.svg)

当前支持：

- `official_playwright_mcp`
  默认受治理高层路径，普通 MCP 任务首选
- `patchright`
  live-root 兼容回退路径，适合某些站点在旧直连路径上更稳定的情况
- `selenium_uc`
  适合 stealth、challenge、手势、拖拽、坐标级 fallback
- `playwright_cli`
  轻量兼容路径，不是默认高能力主路径

## 用户入口

源码入口：

```bash
python run_gui.py
```

典型 Windows 打包入口：

```text
<install_root>\ChromiumProfileManager.exe
```

默认本地 MCP 地址：

```text
http://127.0.0.1:28888/mcp
```

## 典型链路

### 1. GUI + MCP

```text
GUI / Agent
-> daemon / worker
-> SessionManager
-> selected engine
-> real Chromium profile
```

### 2. 固定脚本 + daemon automation

```text
Local script
-> /_daemon/automation/*
-> SessionManager
-> selected engine
-> real Chromium profile
```

### 3. keepalive

```text
GUI / scheduler
-> keepalive runtime
-> profile-scoped lock
-> site check / refresh
-> status writeback
```

## 安全模型

系统把“浏览器业务调用面”和“GUI 控制面”分开了。

- `mcp.api_token`
  给 MCP 客户端和普通 daemon automation 使用
- `control.api_token`
  给 GUI / control 路由使用，例如 dashboard、日志、插件、keepalive、worker 控制

规则：

- 没有 localhost 豁免
- MCP token 不能调用 `/_control/*`
- control token 不能调用 `/mcp`
- 不会为了减少审批或摩擦而绕过 Profile 治理规则

## 受管能力层做了什么

调用方并不是直接面对裸引擎，而是先经过受管能力层。它补齐了：

- 统一动作结果
- 统一高层浏览器工具
- 结构化读取与候选排序
- 诊断与 trace
- session health 与恢复提示
- 适当的跨引擎 fallback

这也是为什么现在可以做到：

- 引擎彼此独立演进
- 调用方仍然看到统一的浏览器能力接口

## 当前边界

- 项目刻意保持通用、开源、可复用，不会在核心代码里为某些特定业务网站写死专用 DOM 适配器。
- 在复杂动态前端上，最可靠的读取与验收面仍然是高层结构化路径，例如 `structured_page`、`browser_list_candidates(...)`、`browser_get_interaction_context(...)`、截图和 trace。
- `run_script(...)` 在健康页面上仍可能合法返回 `result=null`，这应视为读回边界，而不是自动判定为页面坏了。
- 如果任务依赖高保真结构化抽取，应优先使用默认 `official_playwright_mcp` 或显式切到 `patchright`。

## 文档入口

建议从这里开始：

- [文档目录](./docs/README.md)
- [AI 安装与运行手册](./docs/01-getting-started/AI_INSTALLATION_RUNBOOK.md)
- [架构说明](./docs/02-architecture/ARCHITECTURE_GUIDE.md)
- [系统架构总览](./docs/02-architecture/SYSTEM_ARCHITECTURE_OVERVIEW.md)
- [daemon automation 接入](./docs/03-integrations/DAEMON_AUTOMATION_INTEGRATION.md)
- [Keepalive 插件指南](./docs/03-integrations/KEEPALIVE_PLUGIN_GUIDE.md)
- [浏览器核心验证手册](./docs/04-operations/BROWSER_CORE_VALIDATION_PLAYBOOK.md)
- [Skill 模板](./docs/skill_templates/)

## 截图

![软件截图](./docs/imgs/ScreenShot.png)

## 仓库结构

- `run_gui.py`
  源码入口
- `chromium_advanced/chromium_manage_gui.py`
  桌面 GUI
- `chromium_advanced/mcp_daemon.py`
  稳定 daemon 服务
- `chromium_advanced/mcp_server.py`
  浏览器 worker
- `chromium_advanced/session_manager.py`
  Profile / Session 治理核心
- `chromium_advanced/browser_session_kernel.py`
  受管能力统一层
- `docs/`
  用户、集成、运维、架构、参考文档

## License

本项目使用 MIT License。详见 [LICENSE](./LICENSE)。
