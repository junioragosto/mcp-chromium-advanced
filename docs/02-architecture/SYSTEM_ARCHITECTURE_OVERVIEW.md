# Chromium Advanced 系统架构图

这份文档给出当前项目的全景架构图，覆盖：

- GUI 控制面
- daemon / worker / MCP 服务面
- daemon automation API
- `SessionManager` 治理层
- 四套浏览器引擎
- keepalive 插件运行时
- 外部固定脚本接入
- 配置、日志、Profile UserData 存储

说明：

- VS Code 的 Mermaid 预览在当前环境下不稳定，下面主图改为静态 SVG。
- 如果需要再维护 Mermaid 版本，建议单独放到附录，不作为主阅读路径。

## 1. 系统总览

![System Architecture Overview](../assets/system_architecture_overview.svg)

对应含义：

- `Desktop GUI`：桌面图形入口
- `Control Plane`：GUI 通过 `/_control/*` 访问的控制面
- `Service Plane`：MCP 与 daemon automation 的业务面
- `Core Runtime`：daemon、worker、会话治理、能力归一化
- `Browser Engines`：四套浏览器引擎
- `Ops Runtime`：keepalive、浏览器扩展装载、日志
- `Data / Local Assets`：配置、UserData、运行时资源、本地资产

补充语义：
- `UserDataProfile*`：正式持久层，保存长期登录态与用户数据
- `mirror_disk`：镜像层，用于生成受治理运行副本
- `runtime`：临时执行层，用于 `official_playwright_mcp` 等隔离运行时，会后应清理

## 2. 控制面与业务面分层

当前系统不是单一入口，而是三套面：

- GUI 控制面
- MCP 业务面
- daemon automation 业务面

控制面下当前已稳定的核心命名空间包括：

- `/_control/profiles/*`
- `/_control/keepalive/*`
- `/_control/keepalive/sites*`
- `/_control/extensions*`
- `/_control/log-settings`

```mermaid
flowchart TB
    subgraph GUIPlane["GUI Plane"]
        GUI1["GUI tabs"]
        GUI2["profile management"]
        GUI3["keepalive / plugins / logs / worker"]
    end

    subgraph DaemonPlane["daemon HTTP surfaces"]
        CTRL["control endpoints"]
        MCP["mcp endpoint"]
        DA["daemon automation endpoints"]
    end

    GUI1 --> CTRL
    GUI2 --> CTRL
    GUI3 --> CTRL
```

token 边界：

- `control.api_token`：只给 GUI / control API
- `mcp.api_token`：只给 MCP 与普通 daemon automation
- admin token：只给高权限 daemon 管理动作

## 3. GUI 到 daemon 的控制链路

```mermaid
sequenceDiagram
    participant User as User
    participant GUI as GUI
    participant CTRL as control_api
    participant Daemon as daemon
    participant Config as config
    participant Logs as logs

    User->>GUI: click action
    GUI->>CTRL: query dashboard / profiles / sessions
    CTRL->>Daemon: request state
    Daemon->>Config: read config and profile metadata
    Daemon-->>CTRL: return state
    CTRL-->>GUI: return JSON
    GUI->>CTRL: launch/close profile, run keepalive, manage plugins, worker control
    CTRL->>Daemon: execute control action
    Daemon->>Logs: write events
    Daemon-->>GUI: return result
```

## 4. MCP 会话链路

```mermaid
sequenceDiagram
    participant Agent as Agent
    participant MCP as mcp_api
    participant Worker as worker
    participant SM as SessionManager
    participant Kernel as session kernel
    participant Engine as BrowserEngine
    participant Browser as Chromium

    Agent->>MCP: start_profile_session profile engine
    MCP->>Worker: tool call
    Worker->>SM: availability check and create session
    SM->>Engine: create_session(...)
    Engine->>Browser: launch or attach browser
    Engine-->>SM: raw BrowserSession
    SM-->>Worker: session record
    Worker->>Kernel: wrap managed session
    Worker-->>Agent: session_id

    Agent->>MCP: browser_* actions
    MCP->>Worker: tool call
    Worker->>Kernel: managed action / structured read / fallback
    Kernel->>Engine: raw browser action
    Engine->>Browser: click/type/tab/screenshot/diagnose
    Engine-->>Kernel: raw result
    Kernel-->>Worker: normalized result
    Worker-->>Agent: tool result

    Agent->>MCP: close_profile_session(session_id)
    MCP->>Worker: close
    Worker->>SM: release
    SM->>Engine: close_session
    Engine->>Browser: cleanup
    SM-->>Worker: released
    Worker-->>Agent: closed
```

## 5. daemon automation 固定脚本链路

```mermaid
sequenceDiagram
    participant Script as Script
    participant API as daemon_automation_api
    participant Daemon as daemon
    participant SM as SessionManager
    participant Engine as BrowserEngine
    participant Browser as Chromium

    Script->>API: acquire(profile, engine, runtime_options)
    API->>Daemon: request ownership
    Daemon->>SM: occupancy governance and create session
    SM->>Engine: create_session(...)
    Engine->>Browser: launch browser or resource lease
    Engine-->>SM: session
    SM-->>Daemon: session_id and dirs
    Daemon-->>Script: acquire result

    Script->>API: action(session_id, action, args)
    API->>Daemon: browser action
    Daemon->>SM: resolve session
    SM->>Engine: perform action
    Engine->>Browser: actual operation
    Daemon-->>Script: action result

    Script->>API: heartbeat
    API->>Daemon: refresh lease

    Script->>API: release
    API->>Daemon: release
    Daemon->>SM: close/release
    SM->>Engine: cleanup
    Daemon-->>Script: released
```

## 6. `SessionManager` 治理位置

```mermaid
flowchart TB
    Request["GUI / MCP / daemon automation / keepalive"]
    Request --> SM["SessionManager"]

    SM --> Check1["profile exists"]
    SM --> Check2["occupied by GUI"]
    SM --> Check3["occupied by MCP"]
    SM --> Check4["occupied by keepalive"]
    SM --> Check5["reuse_existing allowed"]
    SM --> Check6["concurrency / worker policy / runtime mode"]
    SM --> Paths["resolve user data / profile dir / runtime root"]
    SM --> Factory["BrowserEngine Factory"]

    Factory --> Session["raw BrowserSession"]
    Session --> Registry["session registry / occupancy registry"]
    Registry --> Response["session_id / status / error"]
```

`SessionManager` 统一负责：

- profile 粒度互斥
- session 注册与释放
- 引擎选择
- runtime 路径解析
- 复用判断
- busy-state 对外表达

## 7. 浏览器引擎层

```mermaid
flowchart LR
    Factory["BrowserEngine Factory"]

    Factory --> Official["official_playwright_mcp"]
    Factory --> Patchright["patchright"]
    Factory --> UC["selenium_uc"]
    Factory --> CLI["playwright_cli"]

    Official --> O1["bundled Node.js"]
    Official --> O2["bundled @playwright/mcp runtime"]
    Official --> O3["isolated runtime materialization"]

    Patchright --> P1["direct live-root session"]
    Patchright --> P2["CDP telemetry"]

    UC --> U1["undetected_chromedriver"]
    UC --> U2["stealth / anti-bot / gesture"]

    CLI --> C1["playwright-cli named session"]
    CLI --> C2["short eval + bounded diagnostics"]
```

当前定位：

- `official_playwright_mcp`：默认高层主路径
- `patchright`：live-root 兼容回退
- `selenium_uc`：stealth / challenge / gesture 优先
- `playwright_cli`：轻量兼容路径

## 8. `browser_session_kernel.py` 的位置

```mermaid
flowchart LR
    Tool["MCP browser_* tools"]
    Tool --> Kernel["browser_session_kernel.py"]
    Kernel --> Cap["capability normalization"]
    Kernel --> Meta["action_meta / session_health / resolution_trace"]
    Kernel --> Fallback["DOM fallback / structured read / ranking"]
    Kernel --> Diag["diagnose_page / diagnose_target / anti_bot"]
    Kernel --> Raw["raw BrowserSession"]

    Raw --> Official["official_playwright_mcp"]
    Raw --> Patchright["patchright"]
    Raw --> UC["selenium_uc"]
    Raw --> CLI["playwright_cli"]
```

它负责把多引擎差异收敛成统一的外部工具语义。

## 8.1 Capability Kernel / Orchestrator

当前浏览器内核不再只是“统一接口后直接调原始引擎”。

新增能力层职责：

- `browser_action_registry.py`
  定义标准动作集与各引擎默认 `native_actions`
- `browser_capability_kernel.py`
  统一补齐 `capability_version=3`、`native_actions`、`preferred_paths`、`capability_kernel`
- `browser_action_orchestrator.py`
  在 managed action 执行时决定走 `native_engine` 还是 `legacy_standard`

当前原生优先能力面：

- `official_playwright_mcp`
  `get_page_text` / `get_current_url` / `get_page_html` / `get_interaction_context` / `inspect_elements` / `list_candidates` / `snapshot`
- `patchright`
  与 `official_playwright_mcp` 相同的第一批原生读能力
- `selenium_uc`
  同样接入第一批原生读能力
- `playwright_cli`
  仅对已真实实现的读动作开放原生路径：
  `get_page_text` / `get_current_url` / `get_page_html` / `get_interaction_context` / `snapshot`

结果：

- 治理层仍统一
- 调用方能力面仍统一
- 但强引擎不再被过度适配后损失能力
- 弱引擎也不会被错误声明成支持并不存在的高保真原生能力

## 9. keepalive 架构

```mermaid
flowchart LR
    Scheduler["schedule / manual trigger"]
    GUI["GUI Keepalive tab"]
    Scheduler --> Keepalive["keepalive_runtime.py"]
    GUI --> Keepalive

    Keepalive --> Registry["keepalive_registry.py"]
    Registry --> Builtin["built-in sites"]
    Registry --> External["external Python plugins"]

    Keepalive --> SM["SessionManager"]
    SM --> Engine["browser engine path"]
    Engine --> Browser["real profile browser"]

    Keepalive --> Result["last_keepalive and online_sites"]
    Keepalive --> Mirror["mirror refresh / backup"]
```

## 10. 数据与目录结构

```mermaid
flowchart TB
    Root["config root / workstates"]
    Root --> Config["chromium_profiles.json"]
    Root --> Logs["logs / trace / events"]

    ProfilesRoot["paths.user_data_profiles_root"]
    ProfilesRoot --> P1["UserDataProfile1/Profile 1"]
    ProfilesRoot --> P2["UserDataProfile2/Profile 2"]
    ProfilesRoot --> PN["UserDataProfileN/Profile N"]
    ProfilesRoot --> Mirror["mirror_disk"]

    Resources["resources"]
    Resources --> Runtime["runtime/node and official runtime"]
    Resources --> Bookmarks["bookmarks_template.html"]
    Resources --> Fingerprint["fingerprint / plugin assets"]
```

## 11. 进程模型

```mermaid
flowchart TB
    Launcher["ChromiumProfileManager.exe launcher"]
    GUI["GUI process"]
    Daemon["daemon process"]
    Worker["worker process"]
    Browser["Chromium process group"]

    Launcher --> GUI
    GUI --> Daemon
    Daemon --> Worker
    Worker --> Browser
    Daemon --> Browser
```

## 12. 一句话主线

主线一：

```text
GUI / MCP / Script / keepalive
    -> daemon / worker
    -> SessionManager
    -> BrowserEngine Factory
    -> specific engine
    -> real Chromium profile
```

主线二：

```text
all entrypoints must go through SessionManager before touching a real profile
```

主线三：

```text
browser_session_kernel.py is the capability-unification layer above all engines
```
