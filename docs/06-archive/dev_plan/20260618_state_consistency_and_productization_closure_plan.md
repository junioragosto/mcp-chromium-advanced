# 2026-06-18 状态一致性与产品化收口计划

## 1. 背景

当前项目已经达到“可实际投入使用”的阶段，但还没有完成最后一轮产品化收口。

本轮需要一次性补齐的关键遗留问题主要有 5 类：

1. 手动占用识别虽已修复到可用，但仍存在短暂延迟，不是事件驱动级一致性。
2. GUI 与 core 仍未彻底分层，状态读取、控制逻辑、缓存策略仍然耦合。
3. 外部 Chromium 进程识别仍依赖命令行特征，极端启动形态存在识别盲区。
4. 全局外部进程扫描里仍混入 `on_device_model` 等低价值噪音子进程。
5. 缺少一份正式的“状态一致性 / 占用治理 / GUI-control-core 边界”设计文档，后续维护风险较高。

本轮目标不是再加零散功能，而是把这 5 个问题一次性系统化收口。

## 2. 本轮目标

把当前系统从“已经能用、但还有轮询/缓存/识别边界”的状态，推进到更稳定、更可维护、更接近产品级控制面的状态。

验收目标：

1. GUI 状态列、控制接口、后台运行态三者对同一 profile 的占用状态保持一致。
2. 状态变化延迟从“秒级缓存感知”推进到“接近实时”。
3. GUI 不再承担状态推断职责，只负责展示和控制。
4. external/manual/keepalive/mcp/script 几类占用都进入统一状态模型。
5. 对外文档、开发文档、skill 模板与实际行为一致。

## 3. 设计原则

### 3.1 单一真值源

profile 占用状态只能以 core/control 输出为准。

GUI 不允许自行拼装真值，不允许本地缓存反客为主，只能：

1. 读取 control 快照
2. 接收 control 事件
3. 做短期显示缓存

### 3.2 事件优先，轮询兜底

轮询可以保留，但只能做保底。

主路径改成：

1. core 内部状态变更
2. 写入 occupancy / runtime state
3. 触发 control 事件流
4. GUI 收到事件后局部刷新

### 3.3 分层清晰

分为 3 层：

1. `core/runtime`
   负责 profile 生命周期、锁、occupancy、进程识别、状态快照
2. `control api`
   负责提供只读状态、控制接口、事件流、日志流
3. `gui client`
   负责调用 control api、展示状态、发起控制动作

### 3.4 状态模型先于界面

先统一内部状态模型，再让 GUI 展示。

不能继续由 GUI 反推“应该是什么状态”。

## 4. 本轮实施范围

### 4.1 状态一致性模型收口

目标：

1. 统一 `busy_state / occupancy / lock / external_process / active_session`
2. 明确优先级
3. 保证同一 profile 任一时刻只有一个最终对外状态

实现项：

1. 梳理 `SessionManager.list_profiles()`、`get_runtime_status_snapshot()`、control profiles/status/dashboard 输出字段。
2. 建立统一状态优先级：
   - `active_sessions`
   - `occupancy active`
   - `profile_lock_active`
   - `external_chromium_running`
   - `idle`
3. 清理 GUI 侧对状态的二次推断，改成直接消费 control 输出。

### 4.2 事件驱动状态刷新

目标：

减少 GUI 依赖大 TTL 轮询导致的延迟。

实现项：

1. 为 profile runtime 状态补充轻量事件。
2. 状态变更时发送事件：
   - launch
   - close
   - mcp acquire/release
   - keepalive start/stop
   - script acquire/release
   - reclaim
3. GUI 收到事件后仅刷新受影响 profile，而不是全表重刷。
4. 轮询继续保留，但周期只作为兜底和断线恢复。

### 4.3 GUI / control / core 进一步解耦

目标：

把当前“GUI 中夹带状态逻辑”的部分继续外移。

实现项：

1. 抽出 GUI 所需的统一 profile runtime view model 来源。
2. GUI 不再直接依赖本地 occupancy registry 作为主数据源。
3. `query_control_profiles()`、`query_control_keepalive()`、日志拉取、状态拉取使用统一失效策略。
4. 为后续彻底 GUI/core 分离保留稳定 API 边界。

### 4.4 外部 Chromium 识别增强

目标：

降低“明明在用但未识别”或“识别错 profile”的概率。

实现项：

1. 强化基于 split user data root 的 profile 识别。
2. 补强命令行解析：
   - `--user-data-dir`
   - `--profile-directory`
   - 路径规范化
   - 短路径/大小写差异
3. 对 `manual`、`script`、`direct launch` 等场景统一识别口径。
4. 对无法归属的 Chromium 进程明确标注为 unowned，而不是错误挂到 profile。

### 4.5 外部进程噪音过滤

目标：

降低全局运行态里的低价值噪音，提升诊断可读性。

实现项：

1. 过滤明显不代表 profile 占用的临时模型/utility 子进程。
2. profile 级别状态只统计与目标 user-data-root/profile 直接相关的进程。
3. server status 保留全局统计，但增加：
   - `counted_profile_processes`
   - `ignored_auxiliary_processes`
4. 避免 GUI 状态列被全局噪音误导。

### 4.6 文档与规则固化

目标：

避免后续继续靠口头约定维护状态系统。

实现项：

1. 新增状态一致性设计文档。
2. 说明几类状态来源、优先级、更新时机、事件机制。
3. 更新 README 中关于占用治理、GUI 状态、control api 的说明。
4. 更新 skill 模板，让调用方明确知道：
   - 何时以 control 状态为准
   - 何时 busy 代表真占用
   - 不要自行假设 profile 空闲

## 5. 代码改造点

预计涉及但不限于：

1. `chromium_advanced/session_manager.py`
2. `chromium_advanced/mcp_daemon.py`
3. `chromium_advanced/chromium_manage_gui.py`
4. `chromium_advanced/gui/gui_state.py`
5. 可能新增：
   - `chromium_advanced/runtime_state.py`
   - `docs/runtime_state_consistency.md`

## 6. 测试计划

### 6.1 单元测试

覆盖：

1. `list_profiles()` 状态优先级
2. external process 归属识别
3. 噪音进程过滤
4. control status/profiles/dashboard 输出一致性
5. GUI view model 对 control 字段消费逻辑

### 6.2 集成测试

覆盖：

1. `/_control/profiles`
2. `/_control/profiles/{profile}`
3. `/_daemon/status`
4. launch/close
5. mcp session acquire/release
6. keepalive start/stop
7. script runtime acquire/release

### 6.3 安装态真实验证

必须在安装版上验证：

1. GUI 启动后空闲态正常
2. 手动启动 `Profile 1` 后，控制接口和 GUI 状态同步切到 busy
3. 关闭后恢复 idle
4. MCP 占用、manual 占用、keepalive 占用分别验证
5. 同时两个 profile 占用时状态独立准确
6. 异常回收后状态正确复原

### 6.4 长时间稳定性验证

1. GUI 持续运行至少 30 分钟
2. 多次启动/关闭 profile
3. 多次 acquire/release
4. 状态不漂移、不残留、不长时间滞后

## 7. 验收标准

满足以下条件才算本轮完成：

1. 代码已完成并本地测试通过
2. `pytest tests -q` 全绿
3. 安装版已编译、替换、重启
4. 真实验证证明：
   - 忙时不显示空闲
   - 释放后能恢复空闲
   - GUI/control/daemon 三者一致
5. 文档与 skill 已更新
6. 代码已清理并提交

## 8. 执行前置条件

在正式进入本轮开发前，需要先完成：

1. 把当前未提交的状态一致性修复提交成一个干净基线
2. 以该基线作为本轮重构起点

否则本轮计划无法保证可回溯和可验收。

## 9. 非目标

本轮不做：

1. 新引擎引入
2. 新业务站点插件扩展
3. GUI 大规模视觉重做
4. 远程多机协同控制

本轮只聚焦：状态一致性、识别准确性、分层清晰度、文档固化。
