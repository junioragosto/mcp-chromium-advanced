# 20260623 GUI / Core 完全分离与内核化 API 总规划

## 1. 目标定位

这次不是从零重做，而是在现有“已经拆过一版”的基础上，做彻底化、契约化、内核化收口。

当前项目已经具备这些基础：

1. 已有独立 GUI / daemon / worker 三个编译运行产物
2. 已有 `/_control/*` 一批控制接口
3. 已有 `mcp.api_token` 与 `control.api_token` 的双 token 基础
4. 已有 occupancy、session、keepalive sites、extensions 的部分运行时治理

但问题也很明确：

1. GUI 里仍残留运行态解释和兜底逻辑
2. 配置与 profile 数据混在一个大 JSON 里，扩展性差，写入风险高
3. control API 还只是“能用的一批接口”，不是完整稳定的控制面契约
4. 日志流、事件流、指标、并发控制还没形成明确内核模型
5. 现有后端还不像 `mihomo/clash-core` 那样小而稳，职责边界不够硬

本规划的目标是：

`把当前项目推进成类似 clash/mihomo 的形态：在不增加新的运行产物前提下，把 ChromiumMcpDaemon 这一后端宿主内部收口成稳定小内核宿主，GUI 只是客户端，所有状态真值、控制能力、日志能力、并发治理全部以后端契约为中心。`

## 2. 当前 3 个运行产物的正式语义

后续规划、开发、文档和测试，统一以当前已存在的 3 个运行产物为准，不再引入新的可执行产物概念。

跨平台统一命名如下：

1. `ChromiumProfileManager`
2. `ChromiumMcpDaemon`
3. `ChromiumMcpWorker`

其中：

1. Windows 上通常表现为 `.exe`
2. macOS 上表现为 `.app` 主程序加随附可执行文件
3. Linux 上通常表现为无扩展名可执行文件

### 2.1 `ChromiumProfileManager`

这是 `GUI 客户端`。

职责：

1. 提供桌面入口
2. 展示 profile、session、keepalive sites、extensions、logs 等页面
3. 通过 `Control API` 调用后端
4. 负责托盘、窗口、交互、表单和轻量本地 UI 状态

不负责：

1. 运行态真值裁决
2. profile 并发规则裁决
3. session/occupancy 最终状态裁决
4. keepalive sites / extensions 的最终应用状态裁决

### 2.2 `ChromiumMcpDaemon`

这是 `后端宿主进程`，也是当前 `Core Runtime` 的承载者。

职责：

1. 对外暴露 `Control API`
2. 对外暴露 `Automation API` 的治理入口
3. 承载当前核心运行时逻辑
4. 管理 session、occupancy、keepalive sites、extensions、logging、config
5. 管理 worker 生命周期

说明：

1. 当前并不存在单独的 `Core` 编译产物
2. 当前所谓 `Core`，本质上是内嵌/承载在 `ChromiumMcpDaemon` 背后的核心领域逻辑
3. 后续“GUI 与后端完全分离”的重点不是再增加新的运行产物，而是把 daemon 内部进一步分层，做到 daemon 只做宿主与 API 发布，core 逻辑彻底收口

### 2.3 `ChromiumMcpWorker`

这是 `执行器进程`。

职责：

1. 承载实际自动化动作执行
2. 承载浏览器引擎调用
3. 承载工具级动作和 trace 采集
4. 被 daemon 拉起、监管、回收

不负责：

1. 系统真值裁决
2. profile 并发规则定义
3. control 面状态解释

### 2.4 当前 3 个运行产物的关系

统一表述为：

1. `ChromiumProfileManager` = GUI Client
2. `ChromiumMcpDaemon` = Daemon Host + Core Runtime Host
3. `ChromiumMcpWorker` = Worker Runtime

调用链统一理解为：

`GUI -> Control API -> Daemon/Core -> Worker -> Browser Engine`

而不是：

1. GUI 直接和 worker 建真值关系
2. MCP 直接定义系统真值
3. worker 自己维护系统级状态模型

## 3. 当前双端口的正式语义

后续 API 规划必须明确基于当前双端口模型，不得混淆。

### 3.1 `28888`

默认由 `ChromiumMcpDaemon` 监听。

这是 `总控入口端口`。

负责：

1. `/_daemon/*`
2. `/_control/*`
3. 自动化治理入口
4. profile/session/occupancy 的统一治理
5. GUI 控制请求
6. 服务状态查询

### 3.2 `28889`

默认由 `ChromiumMcpWorker` 监听。

这是 `执行入口端口`。

负责：

1. worker 侧能力执行
2. 浏览器动作执行
3. 自动化工具实际落地

### 3.3 端口职责约束

必须明确：

1. `28888` 是控制面和治理面真值入口
2. `28889` 是执行面
3. `28889` 不能独自形成系统级真值
4. GUI 只信任 `28888` 返回的正式状态
5. worker 的状态必须由 daemon 汇总后对外发布

## 4. 设计原则

### 4.1 真值只能在后端

GUI 不能再“读一点后端、补一点本地、扫一点进程、推一点结论”。

必须改成：

1. Core 统一生成运行态真值
2. Control API 统一暴露真值
3. GUI 只消费，不裁决

### 4.2 配置、元数据、运行态彻底分离

不能继续维护一个不断膨胀的大 JSON。

必须至少拆成三类：

1. 静态主配置
2. profile 元数据
3. 运行态状态

运行态状态不得再作为 GUI 的长期落盘真值。

### 4.3 API 面按职责分域

必须明确三层面：

1. `Control API`
   面向 GUI / 运维 / 桌面控制面
2. `Automation API`
   面向 MCP adapter / agent / 外部脚本 / 自动化消费者
3. `Core Internal Service`
   面向内核内部领域服务，不直接对外

这里强调：

1. 这次规划讨论的是 `系统 API 接口`
2. 不是 MCP tool surface 本身
3. MCP 只是 `Automation API` 之上的一种协议适配层，不得反过来主导系统边界设计

### 4.4 token 是认证边界，不是等级系统

不引入复杂 RBAC。

只强调：

1. `mcp.api_token` 只管自动化面
2. `control.api_token` 只管控制面
3. 两者绝不混用，绝不 fallback

### 4.5 并发治理先于功能增长

后续任何新能力，先纳入统一的 occupancy/session/lease 模型，再开放接口。

不能继续出现“功能先跑起来，再补并发控制”的模式。

### 4.6 后端像 mihomo，一定要小而硬

参考 `mihomo/clash-core` 的优点：

1. 内核职责稳定
2. 控制面明确
3. 运行态真值统一
4. 日志、状态、配置都有清晰边界
5. 客户端随便换，但内核契约不乱

本项目应沿同一路线演进，而不是把 GUI 当成事实内核。

## 5. 现状评估

结合现有代码，当前已经存在这些接口和基础能力：

### 5.1 已有控制接口

当前 `mcp_daemon.py` 中已经存在：

1. `GET /_control/status`
2. `GET /_control/ping`
3. `GET /_control/dashboard`
4. `GET /_control/profiles`
5. `GET /_control/profiles/{profile_name}`
6. `GET /_control/sessions`
7. `GET /_control/events`
8. `GET /_control/keepalive`
9. `POST /_control/profiles/{profile_name}/launch`
10. `POST /_control/profiles/{profile_name}/close`
11. `POST /_control/keepalive/run`
12. `POST /_control/keepalive/stop`
13. `POST /_control/service/worker/start`
14. `POST /_control/service/worker/stop`
15. `GET /_control/logs`
16. `GET /_control/log-settings`
17. `PUT /_control/log-settings`
18. `GET /_control/plugins`
19. `POST /_control/plugins/preview`
20. `POST /_control/plugins`
21. `PUT /_control/plugins/{plugin_id}`
22. `DELETE /_control/plugins/{plugin_id}`
23. `GET /_control/profiles/{profile_name}/plugins`
24. `PUT /_control/profiles/{profile_name}/plugins`

说明：控制面已经不是空白，但目前还不完整。

### 5.2 已有认证基础

当前 `mcp_runtime_config.py` 已有：

1. `resolve_mcp_api_token`
2. `resolve_control_api_token`
3. `control_auth_required`

说明：双 token 基础已经存在，不需要重发明。

### 5.3 已有并发治理基础

当前已有：

1. `occupancy_registry.py`
   管理 occupancy registry、事件、lock、heartbeat、reclaimable
2. `session_manager.py`
   管理 session 生命周期、occupancy 写入、heartbeat、reclaim、release
3. `keepalive_runtime.py`
   已有 profile lock 与 keepalive 运行过程治理

说明：并发控制不是没有，而是还没有抽象成清晰稳定的“内核并发模型”。

### 5.4 当前最明显短板

1. 配置模型过于粗放
2. Control API 缺少完整资源契约和版本化思路
3. 日志接口还是“文件读取 + 简单设置”级别，离成熟控制面还差一层
4. profile 元数据、extension 关联、keepalive 站点定义、运行态状态没有彻底分库/分文件
5. GUI 仍可能持有局部真值与兼容逻辑

## 6. 目标架构

参考 clash/mihomo 风格，目标结构如下：

### 6.1 Core Runtime

Core Runtime 是唯一内核逻辑层，负责：

1. 配置加载、校验、变更、版本迁移
2. profile 元数据加载与治理
3. session / lease / occupancy 模型
4. keepalive 运行调度模型
5. keepalive site 与 extension 关联模型
6. event/log/metrics 统一生产
7. worker / engine 进程治理
8. 强制回收、超时恢复、异常清理

它不负责：

1. GUI 控件
2. HTTP 细节
3. MCP 协议封装细节

说明：

1. Core Runtime 不是新的运行产物
2. Core Runtime 是 `ChromiumMcpDaemon` 内部应当被清晰抽出的领域层

### 6.2 Daemon Host / Control Plane

Daemon Host / Control Plane 负责：

1. 暴露控制面 API
2. 暴露运行态查询 API
3. 暴露日志与事件订阅 API
4. 暴露配置读写 API
5. 暴露服务生命周期控制 API

这是 GUI 唯一依赖的后端控制面。

### 6.3 Automation Plane

Automation Plane 负责：

1. profile/session 获取与归还
2. 浏览器自动化动作
3. 纯资源租约
4. 自动化侧能力声明
5. 自动化侧运行态查询

这是 MCP adapter、agent、脚本消费者唯一依赖的后端自动化面。

### 6.4 MCP Adapter

MCP Adapter 不是核心真值层，它只是：

1. 将 MCP 协议请求转换为 `Automation API` / Core 调用
2. 将后端能力暴露成 MCP tools

强约束：

1. MCP adapter 不维护独立真值
2. MCP adapter 不自行解释 profile 可用性
3. MCP adapter 的状态展示必须来自 daemon/core

### 6.5 GUI Client

GUI 只做：

1. 配置编辑器
2. 状态看板
3. 日志查看器
4. 操作台
5. 托盘入口

GUI 不再承担任何运行态真值解释。

## 7. 配置与存储重构

这是本轮必须明确收口的重点。

### 7.1 问题

当前示例配置 `chromium_profiles.example.json` 把这些内容都混在一起：

1. 应用配置
2. 路径配置
3. MCP 配置
4. 启动参数
5. profile 列表
6. keepalive 配置
7. mirror 配置
8. profile keepalive 状态字段

这会导致：

1. 任意局部更新都要重写整个大文件
2. profile 数据和运行态状态混杂
3. 并发写入风险高
4. 版本迁移困难
5. GUI 容易错误接管配置真值

### 7.2 目标拆分

后续必须拆成至少如下结构：

#### A. 主配置

建议文件：

- `config/app.yaml`

负责：

1. 路径配置
2. 端口配置
3. token 配置
4. 浏览器默认引擎配置
5. 全局 launch 配置
6. 日志配置
7. keepalive 调度全局配置
8. mirror 全局配置

#### B. profile 元数据目录

建议目录：

- `profiles/`
- 每个 profile 一个文件，例如 `profiles/profile_1.yaml`

负责：

1. `profile_name`
2. `user_data_dir_name`
3. `account_label`
4. `enabled`
5. `notes`
6. 默认 engine 偏好
7. keepalive 站点关联
8. extension 关联

不再存放运行时 last_* 状态。

#### C. keepalive 站点目录

建议文件：

- `keepalive/sites.yaml`

负责：

1. 全局站点定义
2. 站点来源
3. 站点元数据
4. 图标定义
5. 插件路径

#### D. extension catalog

建议文件：

- `extensions/catalog.yaml`

负责：

1. 全局 Chrome 扩展定义
2. 来源类型
3. 来源位置
4. 校验状态
5. 版本信息

#### E. 运行态状态目录

建议目录：

- `runtime/`

包含：

1. `runtime/occupancy.json`
2. `runtime/sessions.json`
3. `runtime/service.json`
4. `runtime/keepalive.json`

说明：

1. 这些文件只是 Core 的持久化快照
2. GUI 不直接读这些文件
3. GUI 只通过 API 获取

#### F. 日志与事件目录

建议目录：

- `logs/`

包含：

1. `logs/core.log`
2. `logs/daemon.log`
3. `logs/events.jsonl`
4. `logs/audit.jsonl`

### 7.3 文件格式建议

主配置和 profile 元数据优先用 `YAML`，原因：

1. 适合用户手改
2. 支持注释
3. 比巨型 JSON 更可维护

运行态日志和事件优先：

1. `JSON`
2. `JSONL`

原因：

1. 结构化强
2. 便于筛选与回放
3. 便于 API 直接映射

### 7.4 版本迁移

必须实现：

1. 从旧的单大 JSON 迁移到新结构
2. 自动检测旧结构版本
3. 迁移时写出备份
4. 迁移失败可回滚

## 8. 认证与接口边界

### 8.1 保留并强化现有双 token

当前已有：

1. `mcp.api_token`
2. `control.api_token`

本轮不改概念，只彻底落实。

### 8.2 规则

1. 所有 `/_control/*` 必须只接受 `control.api_token`
2. 所有 `/mcp` 与 `/_automation/*` 必须只接受 `mcp.api_token`
3. 不因为 `127.0.0.1` 或 `localhost` 做豁免
4. 不允许任意 token fallback 到另一侧
5. 未配置 `control.api_token` 时，控制面可显式禁用，但不能回退到 MCP token

### 8.3 内部 GUI/Core 认证

GUI 与 Core 的通信仍属于 control 域，不单独复用 MCP token。

如果后续要进一步增强，可再加入 GUI 启动期握手 secret，但不影响现在的双 token 主模型。

## 9. Control API 总设计

当前已有一批接口，这次要把它们收口成稳定的正式控制面。

### 9.1 服务与健康

保留并规范：

1. `GET /_control/ping`
2. `GET /_control/status`
3. `GET /_control/dashboard`
4. `GET /_control/health`
5. `GET /_control/service/processes`

需要补充：

1. `GET /_control/version`
2. `GET /_control/config/schema`
3. `GET /_control/metrics`

### 9.2 profile 管理

当前已有：

1. `GET /_control/profiles`
2. `GET /_control/profiles/{profile_name}`
3. `POST /_control/profiles/{profile_name}/launch`
4. `POST /_control/profiles/{profile_name}/close`

需要补充：

1. `POST /_control/profiles`
2. `PUT /_control/profiles/{profile_name}`
3. `DELETE /_control/profiles/{profile_name}`
4. `POST /_control/profiles/sync`
5. `POST /_control/profiles/{profile_name}/force-release`
6. `POST /_control/profiles/{profile_name}/reclaim`

### 9.3 session / occupancy / lease

当前已有：

1. `GET /_control/sessions`

需要补充：

1. `GET /_control/sessions/{session_id}`
2. `GET /_control/occupancy`
3. `GET /_control/leases`
4. `POST /_control/sessions/{session_id}/terminate`
5. `POST /_control/sessions/{session_id}/heartbeat`

### 9.4 keepalive

当前已有：

1. `GET /_control/keepalive`
2. `POST /_control/keepalive/run`
3. `POST /_control/keepalive/stop`

需要补充：

1. `GET /_control/keepalive/sites`
2. `POST /_control/keepalive/sites`
3. `PUT /_control/keepalive/sites/{site_id}`
4. `DELETE /_control/keepalive/sites/{site_id}`
5. `POST /_control/keepalive/run/{profile_name}`
6. `GET /_control/keepalive/history`

### 9.5 现有 `plugins` 接口遗留区

说明：

1. 这是当前代码里的历史接口命名
2. 现实上主要仍偏 keepalive 脚本管理
3. 它不是未来目标的最终接口形态
4. 后续必须迁移到 `keepalive/sites` 与 `extensions` 两套独立接口

当前已有：

1. `GET /_control/plugins`
2. `POST /_control/plugins/preview`
3. `POST /_control/plugins`
4. `PUT /_control/plugins/{plugin_id}`
5. `DELETE /_control/plugins/{plugin_id}`
6. `GET /_control/profiles/{profile_name}/plugins`
7. `PUT /_control/profiles/{profile_name}/plugins`

需要补充：

1. `GET /_control/plugins/{plugin_id}`
2. `POST /_control/plugins/{plugin_id}/refresh`
3. `POST /_control/plugins/{plugin_id}/validate`

这些接口后续应视为 `legacy transitional routes`，而不是长期产品契约。

### 9.6 配置控制

当前缺口较大。

建议新增：

1. `GET /_control/config/runtime`
2. `GET /_control/config/app`
3. `PUT /_control/config/app`
4. `GET /_control/config/profiles`
5. `GET /_control/config/profiles/{profile_name}`
6. `PUT /_control/config/profiles/{profile_name}`
7. `GET /_control/config/logging`
8. `PUT /_control/config/logging`

### 9.7 日志与事件

当前已有：

1. `GET /_control/logs`
2. `GET /_control/log-settings`
3. `PUT /_control/log-settings`
4. `GET /_control/events`

需要补充：

1. `GET /_control/logs/summary`
2. `GET /_control/logs/stream`
3. `GET /_control/events/stream`
4. `GET /_control/audit`
5. `POST /_control/logs/export`

### 9.8 当前还未完整写出的新增控制面接口

为了避免后续开发阶段再临时补接口，这里把建议新增但上文尚未系统展开的控制面接口一次性列清。

#### 诊断与恢复

1. `GET /_control/diagnostics/runtime`
   返回运行时总览、关键配置摘要、进程绑定、端口占用、最近错误
2. `POST /_control/diagnostics/export`
   导出诊断包，包含日志、事件、配置摘要、运行态快照
3. `POST /_control/recovery/reconcile`
   触发一次运行态自检与状态对账
4. `POST /_control/recovery/reclaim-stale`
   主动回收 stale session / stale occupancy
5. `POST /_control/recovery/clear-orphans`
   清理孤儿 worker / 孤儿 runtime 标记

#### Daemon / Worker 进程治理

1. `GET /_control/service/runtime`
   返回 daemon/core/worker 当前角色状态
2. `POST /_control/service/worker/restart`
   仅重启 worker，不影响 daemon
3. `POST /_control/service/daemon/reload-config`
   触发后端配置重载
4. `POST /_control/service/daemon/shutdown`
   正常关闭 daemon
5. `GET /_control/service/ports`
   返回当前 control/automation/worker 端口与监听状态

#### 引擎与能力面

1. `GET /_control/engines`
   返回已注册引擎、可用性、默认引擎、降级顺序
2. `GET /_control/engines/{engine_name}`
   返回单引擎详情、依赖、能力摘要
3. `PUT /_control/engines/default`
   修改系统默认引擎
4. `GET /_control/capabilities`
   返回当前后端汇总后的能力矩阵，供 GUI 展示

#### 运行态快照

1. `GET /_control/runtime/snapshot`
   一次性返回 GUI 首页所需的统一快照，减少多接口拼装
2. `GET /_control/runtime/revision`
   返回当前运行态 revision，用于 GUI 增量同步

#### Profile 站点状态

1. `GET /_control/profiles/{profile_name}/site-states`
   返回该 profile 当前已知站点在线状态数组/映射
2. `POST /_control/profiles/{profile_name}/site-states/refresh`
   触发一次轻量刷新

这类接口是为你前面提到的“业务需要先知道某个 profile 对某站点是否在线”准备的正式控制面，不属于 MCP 工具接口。

## 10. Automation API 总设计

自动化面要和控制面彻底分离。

这里的 `Automation API` 是系统 API，不等于 MCP tool surface。

MCP 只是其中一种消费者。

### 10.1 资源租约

必须支持两类模式：

1. 浏览器自动化会话
2. 纯资源租约

典型场景：

1. MCP 调浏览器动作
2. 外部工具只想拿 `user_data_dir/profile_dir`

### 10.2 建议接口

#### 资源获取

1. `POST /_automation/acquire`
2. `POST /_automation/release`
3. `POST /_automation/heartbeat`
4. `GET /_automation/sessions/{session_id}`
5. `GET /_automation/profiles/{profile_name}/availability`

#### 能力查询

1. `GET /_automation/capabilities`
2. `GET /_automation/engines`

#### 自动化动作

保留 MCP 工具与浏览器动作面，不在本规划中重定义动作细节，但其会话生命周期必须全部纳入资源租约体系。

### 10.3 资源获取响应结构

响应结构应统一至少包含：

1. `session_id`
2. `profile_name`
3. `engine_name`
4. `browser_family`
5. `lease_mode`
6. `user_data_dir`
7. `profile_dir`
8. `lease_expires_at`
9. `heartbeat_timeout_seconds`
10. `owner_type`

### 10.4 当前还未完整写出的新增自动化面接口

这里补全那些前面只隐含提到、但没有系统列出的自动化接口。

#### 资源与租约

1. `GET /_automation/leases`
   查看当前自动化租约列表
2. `GET /_automation/leases/{session_id}`
   查看单租约详情
3. `POST /_automation/leases/{session_id}/extend`
   延长租约
4. `POST /_automation/leases/{session_id}/release`
   显式释放租约

#### profile 可用性与选择

1. `GET /_automation/profiles`
   返回自动化面可见的 profile 列表
2. `GET /_automation/profiles/{profile_name}`
   返回单 profile 的自动化面状态
3. `POST /_automation/profiles/resolve`
   按约束条件解析一个可用 profile，例如空闲、指定引擎、指定在线站点

#### 纯资源模式

1. `POST /_automation/resource/acquire`
   获取 `resource_only` 租约
2. `POST /_automation/resource/release`
   释放 `resource_only` 租约
3. `GET /_automation/resource/{session_id}`
   查询资源租约详情

#### 会话能力

1. `GET /_automation/sessions/{session_id}/capabilities`
   返回当前 session 实际可用能力
2. `GET /_automation/sessions/{session_id}/state`
   返回当前 session 运行态
3. `POST /_automation/sessions/{session_id}/ping`
   轻量保活

#### 自动化端诊断

1. `GET /_automation/diagnostics`
   返回自动化面诊断状态
2. `GET /_automation/engines`
   返回自动化可用引擎摘要

这些接口仍然属于系统 API。MCP 只是一层协议适配，应当调用这些能力，而不是替代它们。

## 11. 日志流、事件流、审计流

这是这轮必须好好设计的重点。

### 11.1 三类流分离

必须明确：

1. `runtime logs`
   面向诊断与观察
2. `runtime events`
   面向状态变化
3. `audit events`
   面向安全与操作追踪

不能再把所有东西都混成“日志”。

### 11.2 日志等级

建议固定：

1. `debug`
2. `info`
3. `warning`
4. `error`

可选扩展：

5. `trace`

但只有在确认真的需要时再打开，默认不要把系统做重。

### 11.3 日志来源

建议固定来源枚举：

1. `core`
2. `daemon`
3. `session`
4. `occupancy`
5. `keepalive`
6. `extension`
7. `engine`
8. `worker`
9. `automation`
10. `control`
11. `gui_bridge`

### 11.4 运行日志结构

建议结构：

```json
{
  "ts": "2026-06-23T12:00:00.000Z",
  "level": "info",
  "source": "session",
  "category": "lease",
  "profile_name": "Profile 1",
  "session_id": "sess_xxx",
  "message": "lease acquired",
  "details": {
    "engine": "official_playwright_mcp",
    "lease_mode": "browser_session"
  },
  "correlation_id": "corr_xxx"
}
```

### 11.5 事件结构

建议结构：

```json
{
  "event_id": "evt_xxx",
  "ts": "2026-06-23T12:00:00.000Z",
  "type": "profile_occupancy_changed",
  "profile_name": "Profile 1",
  "session_id": "sess_xxx",
  "source_type": "mcp",
  "state_before": "idle",
  "state_after": "occupied",
  "details": {},
  "correlation_id": "corr_xxx"
}
```

### 11.6 审计结构

建议结构：

```json
{
  "audit_id": "audit_xxx",
  "ts": "2026-06-23T12:00:00.000Z",
  "actor_type": "control_api",
  "actor_id": "gui_local",
  "action": "extension_update",
  "target": "extension:sample_extension",
  "result": "success",
  "remote_addr": "127.0.0.1",
  "details": {}
}
```

### 11.7 GUI 与日志的关系

GUI 不维护日志真值，只做：

1. 拉取
2. 过滤
3. 高亮
4. 导出

日志等级与保留策略以后端配置为准。

### 11.8 日志配置

至少支持：

1. `level`
2. `retention_days`
3. `max_file_size_mb`
4. `max_files`
5. `enable_audit_log`
6. `enable_event_log`

### 11.9 日志等级切换能力

日志等级必须支持通过 `Control API` 动态切换，使用方式应接近 clash/mihomo 一类内核：

1. 平时默认 `info`
2. 调试时临时切到 `debug`
3. 问题排查结束后切回 `info`
4. 严格压缩输出时可切到 `warning` / `error`
5. 需要近似静默时可切到 `silent`

建议正式支持的等级枚举：

1. `debug`
2. `info`
3. `warning`
4. `error`
5. `silent`

说明：

1. `silent` 表示关闭普通运行日志输出，但不应关闭关键审计/致命错误记录
2. `debug` 应明显增加诊断细节，但必须受限于 retention 和文件滚动策略

建议接口：

1. `GET /_control/logging/config`
2. `PUT /_control/logging/config`
3. `POST /_control/logging/level`
4. `POST /_control/logging/reset`

其中：

1. `PUT /_control/logging/config` 用于整体配置更新
2. `POST /_control/logging/level` 用于快速切级，适合 GUI 上像 clash 那样一键切换

### 11.10 日志体系必须正式分层

现在项目里已经出现了多种日志概念：

1. GUI 可见日志
2. daemon 日志
3. worker / MCP 调用日志
4. keepalive 运行日志
5. 插件/脚本运行日志
6. 事件流
7. 审计记录

后续不能再继续混用“日志”这个词，而必须分成正式的几类 `log channels`。

建议固定为以下日志通道：

#### A. `system`

系统级日志，覆盖：

1. daemon 启停
2. worker 启停
3. 配置加载/重载
4. 端口监听
5. 全局恢复流程

#### B. `control`

控制面日志，覆盖：

1. GUI 调用 control API
2. 配置修改
3. profile 管理动作
4. 插件管理动作

#### C. `automation`

自动化治理日志，覆盖：

1. acquire / release
2. session lease
3. heartbeat
4. reclaim
5. automation API 请求

#### D. `worker`

执行器日志，覆盖：

1. worker 进程生命周期
2. tool/action 调用
3. 浏览器动作执行
4. trace 摘要

#### E. `engine`

引擎日志，覆盖：

1. `official_playwright_mcp`
2. `patchright`
3. `selenium_uc`
4. `playwright_cli`

每条日志应携带 `engine_name`，便于过滤。

#### F. `keepalive`

保活日志，覆盖：

1. 调度触发
2. profile 锁定
3. 站点脚本执行
4. 结果汇总

#### G. `extension`

插件日志，覆盖：

1. 插件加载
2. 插件校验
3. 插件更新
4. 插件运行错误

#### H. `audit`

审计日志，覆盖：

1. token 鉴权失败
2. 配置写入
3. 强制释放
4. 删除动作
5. 恢复动作

#### I. `event`

严格来说这是事件流，不是普通文本日志，但查询和展示层可以作为单独通道暴露。

### 11.11 日志接口也必须分面

不能只提供一个笼统的 `GET /_control/logs`。

建议保留聚合查询，同时提供按通道/用途的正式接口：

#### 聚合查询

1. `GET /_control/logs`
   支持 channel / level / profile / session / engine / time-range 过滤
2. `GET /_control/logs/summary`
3. `GET /_control/logs/stream`

#### 分通道查询

1. `GET /_control/logs/system`
2. `GET /_control/logs/control`
3. `GET /_control/logs/automation`
4. `GET /_control/logs/worker`
5. `GET /_control/logs/engine`
6. `GET /_control/logs/keepalive`
7. `GET /_control/logs/extension`
8. `GET /_control/logs/audit`

#### 事件与审计

1. `GET /_control/events`
2. `GET /_control/events/stream`
3. `GET /_control/audit`

### 11.12 GUI 上的日志视图目标

GUI 不应再只是“控制台日志”和“MCP 日志”两个粗窗口。

目标应改成统一日志页，具备：

1. 通道选择
2. 等级选择
3. profile 过滤
4. session 过滤
5. engine 过滤
6. 时间范围过滤
7. 一键切换日志等级
8. 导出诊断包

### 11.13 日志架构收口要求

后续实现时必须做到：

1. 所有日志最终归口到同一套 `logging service`
2. 各子系统只发结构化日志事件，不各自造文件格式
3. GUI 不直接读散落文件做拼装
4. `keepalive`、`mcp/automation`、`worker/tool calls`、`system` 都是统一日志体系下的不同 channel
5. 日志等级切换必须通过 control API 生效，不允许 GUI 只改本地显示而不改后端真实配置

## 12. 并发控制与租约模型

这部分要像内核一样硬。

### 12.1 当前基础

当前已经有：

1. occupancy registry lock
2. file lock
3. heartbeat_timeout_seconds
4. reclaimable
5. reclaim/release

说明：基础不错，但还需要上升成统一的租约模型。

### 12.2 统一概念

后续统一使用以下概念：

1. `profile lock`
   表示 profile 级排他使用权
2. `session lease`
   表示某个调用方的租约对象
3. `heartbeat`
   表示租约仍然活着
4. `reclaim`
   表示后端强制回收租约
5. `owner_type`
   表示占用来源

### 12.3 owner_type 枚举

建议固定：

1. `mcp`
2. `control`
3. `manual`
4. `script`
5. `keepalive`
6. `system`

### 12.4 lease_mode 枚举

建议固定：

1. `browser_session`
2. `resource_only`
3. `keepalive_run`
4. `manual_open`

### 12.5 统一状态机

建议 profile 运行态最少包含：

1. `idle`
2. `starting`
3. `occupied`
4. `reclaiming`
5. `stale`
6. `error`

### 12.6 强制规则

1. 同一时刻同一 profile 只能有一个主租约
2. 所有使用路径都必须登记到同一套 Core 运行态模型
3. 用户手工启动和 MCP 调用只是不同 `owner_type`，不能形成两套状态体系
4. 手工打开浏览器也要尽可能映射到 `manual_open`
5. keepalive 只能锁单个 profile，不能锁全局
6. stale 租约由 Core 自动判定和回收，GUI 不裁决
7. worker 不能单独宣布某 profile 空闲或占用，必须由 daemon/core 汇总

### 12.7 并发恢复

必须支持：

1. 心跳超时自动标记 stale
2. worker 崩溃后租约可回收
3. GUI 崩溃不影响已有租约真值
4. daemon 重启后从 runtime snapshot 恢复

## 13. GUI 彻底客户端化

### 13.1 GUI 只保留

1. 页面结构
2. 表格与表单
3. 请求触发
4. 结果渲染
5. 轻量 UI 状态

### 13.2 GUI 必须移除

1. 本地占用状态推断
2. 本地 keepalive 状态推断
3. 本地 extension 应用状态推断
4. 本地 daemon/worker 真值推断
5. 本地配置真值写盘逻辑

### 13.3 GUI 建议模块

1. `gui/shell/`
2. `gui/api_client/`
3. `gui/viewmodels/`
4. `gui/pages/`
5. `gui/widgets/`
6. `gui/state/`

## 14. 代码分层建议

参考 `mihomo` 一类核心内核项目，后端建议拆成：

### 14.1 core/models

放：

1. config models
2. runtime state models
3. lease/session models
4. log/event/audit models

### 14.2 core/repositories

放：

1. config repository
2. profile repository
3. runtime snapshot repository
4. log repository
5. extension repository

### 14.3 core/services

放：

1. session service
2. occupancy service
3. keepalive service
4. extension service
5. config service
6. logging service
7. recovery service

### 14.4 core/api

放：

1. control routes
2. automation routes
3. auth middleware
4. response schemas

## 15. 一次性到位的代码架构设计

这部分是本次规划新增的硬要求：后续实现不能只补接口，必须同时把代码架构收成长期可扩展形态，避免做完接口后很快再重构。

### 15.1 顶层目录目标

建议后端与 GUI 最终收成如下形态：

```text
chromium_advanced/
  app/
    bootstrap.py
    runtime_roles.py
  core/
    models/
    enums/
    services/
    repositories/
    policies/
    events/
    logging/
    recovery/
  api/
    control/
    automation/
    middleware/
    schemas/
    transport/
  worker/
    runtime/
    engines/
    adapters/
    traces/
  gui/
    shell/
    api_client/
    viewmodels/
    pages/
    widgets/
    state/
  integrations/
    mcp_adapter/
    keepalive_plugins/
    external_tools/
  storage/
    config_store.py
    profile_store.py
    runtime_store.py
    log_store.py
  compat/
    legacy_config_migration.py
    legacy_runtime_bridge.py
```

### 15.2 顶层边界规则

必须遵守：

1. `gui/` 不能 import `worker/` 具体实现
2. `gui/` 不能直接读 `storage/` 真值文件
3. `worker/` 不能直接修改 profile 真值配置
4. `api/control` 与 `api/automation` 不能各自维护独立状态缓存
5. 所有运行态真值只能通过 `core/services` + `repositories` 组合产生

### 15.3 core 层的职责拆分

#### `core/models`

只放数据模型：

1. profile model
2. session lease model
3. occupancy model
4. keepalive runtime model
5. keepalive site model
6. browser extension model
6. runtime snapshot model
7. log/event/audit model

#### `core/enums`

集中放：

1. owner_type
2. lease_mode
3. profile_state
4. log_level
5. event_type
6. engine_name

#### `core/policies`

专门放规则，而不是散落在 service 里：

1. profile availability policy
2. lease arbitration policy
3. reclaim policy
4. keepalive locking policy
5. engine resolution policy

#### `core/services`

只做领域服务编排，不碰 HTTP：

1. profile_service
2. session_service
3. occupancy_service
4. keepalive_service
5. keepalive_site_service
6. extension_service
6. config_service
7. diagnostics_service
8. recovery_service
9. logging_service

#### `core/repositories`

只做持久化读写：

1. app_config_repository
2. profile_repository
3. keepalive_site_repository
4. extension_repository
5. keepalive_runtime_repository
5. runtime_snapshot_repository
6. event_log_repository
7. audit_log_repository

### 15.4 api 层的职责拆分

#### `api/control`

只处理：

1. control route 定义
2. request -> service 调用映射
3. response schema 封装

#### `api/automation`

只处理：

1. automation route 定义
2. session/resource 租约请求映射
3. automation response schema

#### `api/middleware`

集中放：

1. auth middleware
2. request id / correlation id middleware
3. error translation middleware
4. access log middleware

#### `api/schemas`

必须单独放，不能混在 route 文件里：

1. control request/response schema
2. automation request/response schema
3. shared runtime schema

### 15.5 worker 层的职责拆分

#### `worker/runtime`

负责：

1. worker 进程启动
2. worker 健康状态
3. worker session registry

#### `worker/engines`

每个引擎一个独立目录，避免继续堆成超大单文件：

```text
worker/engines/
  official_playwright_mcp/
    session.py
    actions.py
    diagnostics.py
  patchright/
    session.py
    actions.py
    diagnostics.py
  selenium_uc/
    session.py
    actions.py
    diagnostics.py
  playwright_cli/
    session.py
    actions.py
    diagnostics.py
```

#### `worker/adapters`

负责统一 worker 能力输出，不在上层做引擎细节判断。

### 15.6 GUI 层的职责拆分

GUI 必须拆成下面几层，避免再次长成 3k+ 行单文件：

1. `gui/shell`
   主窗口、导航、托盘、生命周期
2. `gui/api_client`
   统一调用 control API
3. `gui/viewmodels`
   把后端数据映射成 GUI 视图数据
4. `gui/pages`
   Dashboard / Profiles / Sessions / Keepalive Sites / Extensions / Logs / Settings
5. `gui/widgets`
   可复用控件
6. `gui/state`
   只存 UI 本地状态，不存系统真值

### 15.7 兼容层规则

如果必须兼容老逻辑，只能放在 `compat/`：

1. 老配置迁移
2. 老状态文件兼容
3. 旧字段桥接

禁止再把兼容逻辑直接塞回 GUI、daemon 主入口、engine 主文件里。

### 15.8 实现时必须守住的架构红线

后续开发时，以下情况一律视为架构违规：

1. GUI 直接扫描进程后自行判定最终状态
2. route 文件里直接写大段业务规则
3. worker 自己修改系统配置真值
4. session_manager 再次无限膨胀成总控杂物间
5. engine 文件继续堆到 1500+ 行以上而不拆分
6. 新增功能绕过 service/repository 直接读写状态文件

## 16. 保活脚本与 Chrome 扩展必须彻底分语义

这里必须明确：当前项目里至少存在两类完全不同的可扩展对象，之前命名混在一起是错误的，后续必须彻底拆开。

### 16.1 第一类：保活脚本（Keepalive Site Scripts）

这是“站点保活逻辑扩展”，不是 Chrome 浏览器扩展。

它的职责是：

1. 定义某个站点如何检查登录态
2. 定义某个站点如何做保活动作
3. 定义站点图标、显示名、元数据
4. 支持用户动态新增站点脚本

它的核心特征是：

1. 面向 keepalive 逻辑
2. 运行在后端 keepalive 执行流程里
3. 和浏览器是否安装某个 Chrome 扩展不是一回事

建议正式命名统一为：

1. `keepalive site`
2. `keepalive script`
3. `keepalive site definition`

不要继续泛称为 `plugin`。

#### Keepalive 脚本来源

支持来源：

1. 系统内置
2. 用户本地脚本文本
3. 用户指定脚本目录

#### Keepalive 与 Profile 的关系

每个 profile 可以动态关联多个 keepalive site。

每个 keepalive site 也可以被多个 profile 关联。

这是标准 `多对多` 关系。

应有独立关系模型：

- `profile_keepalive_site_associations`

### 16.2 第二类：Chrome 扩展（Browser Extensions）

这是“浏览器扩展挂载系统”，不是 keepalive 脚本。

它的职责是：

1. 让用户导入/注册浏览器扩展
2. 把扩展与 profile 做动态关联
3. 在 profile 启动时按关联关系挂载扩展
4. 支持不同 profile 使用不同扩展组合

你前面提到的“当前只固定了一个扩展，但用户应当也能导入、安装并动态和 profile 关联，多对多，启动时带上”，本质上说的就是这套 Chrome 扩展系统。

这部分当前确实没有完整产品化落地，之前只保留了固定扩展/单路径扩展的痕迹，没有真正做成“全局扩展目录 + profile 多对多关联”。

#### Extension 来源

支持来源：

1. 系统内置扩展
2. 本地 `.crx`
3. 本地 `.zip`
4. 解压目录
5. GitHub release/download URL

#### Extension 与 Profile 的关系

每个 profile 可以关联多个 extension。

每个 extension 也可以被多个 profile 关联。

这同样是标准 `多对多` 关系。

应有独立关系模型：

- `profile_extension_associations`

### 16.3 两套系统的边界

必须明确：

1. keepalive script 不等于 Chrome extension
2. keepalive script 作用于保活逻辑
3. Chrome extension 作用于浏览器启动挂载逻辑
4. 两套目录、两套配置、两套接口、两套关联关系必须分开

### 16.4 当前 `/_control/plugins` 的整改方向

当前 `/_control/plugins` 实际上主要还是 keepalive 脚本管理接口，命名是有歧义的。

后续必须改成明确分层：

#### Keepalive 相关

1. `GET /_control/keepalive/sites`
2. `POST /_control/keepalive/sites`
3. `PUT /_control/keepalive/sites/{site_id}`
4. `DELETE /_control/keepalive/sites/{site_id}`
5. `GET /_control/profiles/{profile_name}/keepalive-sites`
6. `PUT /_control/profiles/{profile_name}/keepalive-sites`

#### Chrome 扩展相关

1. `GET /_control/extensions`
2. `POST /_control/extensions`
3. `PUT /_control/extensions/{extension_id}`
4. `DELETE /_control/extensions/{extension_id}`
5. `POST /_control/extensions/{extension_id}/validate`
6. `POST /_control/extensions/{extension_id}/refresh`
7. `GET /_control/profiles/{profile_name}/extensions`
8. `PUT /_control/profiles/{profile_name}/extensions`

也就是说：

1. `/_control/plugins` 这个名字不应继续承担两套语义
2. keepalive 用 `sites`
3. Chrome 插件用 `extensions`

### 16.5 Chrome 扩展系统的目标能力

后续必须正式实现，而不是再退回“固定一个扩展路径”模式。

至少要支持：

1. 全局扩展目录管理
2. 扩展导入
3. 扩展元数据识别
4. 扩展图标与显示名
5. 扩展校验与来源记录
6. profile 级多对多关联
7. profile 启动时按关联关系挂载扩展
8. 不同 profile 可使用不同扩展组合
9. GUI 可视化关联编辑

### 16.6 启动挂载规则

profile 启动时，daemon/core 应统一完成：

1. 读取 profile 关联的 extension 列表
2. 解析每个 extension 的实际挂载路径
3. 根据当前 engine 能力决定如何挂载
4. 将最终挂载结果写入 runtime state

注意：

1. 是否支持挂载、如何挂载，是 engine 能力问题
2. 但“该 profile 关联了哪些扩展”是 Core 真值问题
3. GUI 不负责拼扩展挂载参数

### 16.7 配置与存储拆分

后续必须独立存储：

1. `keepalive/sites.yaml`
   存 keepalive site 定义
2. `profiles/<profile>.yaml`
   存该 profile 的 keepalive site 关联与 extension 关联
3. `extensions/catalog.yaml`
   存全局 Chrome 扩展目录

### 16.8 代码结构也必须拆开

不能再把这两套能力混在一个 `plugin` 抽象里。

建议后端明确分为：

1. `core/services/keepalive_site_service.py`
2. `core/services/extension_service.py`
3. `core/repositories/keepalive_site_repository.py`
4. `core/repositories/extension_repository.py`
5. `api/control/keepalive_routes.py`
6. `api/control/extension_routes.py`

### 16.9 GUI 页面目标

后续 GUI 不应再只有一个模糊的插件页。

应拆成至少两个页面或两个明确分区：

1. `Keepalive Sites`
   管理站点保活脚本及 profile 关联
2. `Extensions`
   管理 Chrome 扩展及 profile 关联

### 16.10 当前缺失项结论

当前缺失的不是“插件功能整体没有”，而是：

1. keepalive script 已经部分存在，但命名和接口抽象不清
2. Chrome extension catalog + profile 多对多关联没有完整实现
3. 两套系统在命名、接口、配置、GUI 上都没有彻底分离

这个缺口必须在后续实现里一次性补上，不能再继续用一个模糊的 `plugin` 概念承载两套完全不同的系统。

### 16.11 图标能力

两套系统都应支持图标，但来源与意义不同：

1. keepalive site 的图标表示“站点”
2. extension 的图标表示“浏览器扩展”

统一字段可以相似，但模型必须独立：

1. `icon_uri`
2. `icon_hash`
3. `display_name`

## 17. 实施顺序

后续实现必须按这个顺序推进。

### 17.1 先冻结契约

完成：

1. 配置拆分结构
2. 领域模型
3. 路由设计
4. 日志结构
5. 并发租约模型

### 17.2 再收口 Core

完成：

1. occupancy/session/keepalive/extensions/logging 全部后端真值化
2. runtime snapshot 与恢复机制
3. 旧大 JSON 迁移器

### 17.3 再补齐 Control / Automation API

完成：

1. 现有接口标准化
2. 缺失接口补齐
3. 错误码与响应结构统一

### 17.4 最后 GUI 瘦身

完成：

1. GUI 只读 API
2. GUI 不再解释真值
3. GUI 模块化拆分

## 18. 测试与验收

### 18.1 配置迁移测试

验证：

1. 旧大 JSON 可迁移
2. 迁移后结构完整
3. 回滚可用

### 18.2 认证测试

验证：

1. `control.api_token` 不能访问 automation
2. `mcp.api_token` 不能访问 control
3. localhost 无豁免

### 18.3 并发测试

验证：

1. 不同 profile 并发正常
2. 同 profile 双占用被拒绝
3. stale heartbeat 自动回收
4. keepalive 只锁单个 profile
5. resource_only 与 browser_session 都被统一治理

### 18.4 日志与事件测试

验证：

1. logs/events/audit 三类输出结构正确
2. 按等级过滤正常
3. retention 生效
4. GUI 读取不阻塞

### 18.5 GUI 一致性测试

验证：

1. GUI 展示和 control API 一致
2. GUI 重启后状态恢复一致
3. daemon 重启后 GUI 正常重连

### 18.6 性能验收

对齐你前面定的方向，至少满足：

1. GUI 空闲 CPU 显著下降
2. daemon 空闲 CPU 不应继续维持在不可接受的高占用
3. 高并发日志与状态刷新不阻塞 UI

## 19. 这次规划相对上一版的补强点

相比前一版“GUI/Core 分离计划”，这次新增并压实了这些内容：

1. 不再接受“大 JSON 配置”路线，明确要求配置/元数据/运行态拆分
2. 明确区分 control / automation / internal 三个平面
3. 把现有接口与需补接口分开列清楚
4. 单独设计 logs / events / audit 三类流
5. 单独设计 lease / occupancy / heartbeat / reclaim 并发模型
6. 明确以 `mihomo/clash-core` 风格作为内核化对照

## 20. 结论

这次不是“再做一版分离”，而是：

`把现有半分离状态，推进成真正的内核化架构。`

硬标准只有一条：

`GUI 不再持有真值，Core 成为唯一真值源，Control/Automation 形成稳定契约，配置/状态/日志/并发都以小而硬的内核方式收口。`

这才是后续把系统做稳、做小、做成熟的正确方向。
