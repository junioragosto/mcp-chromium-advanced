# 2026-06-23 保活调度与 Runtime 治理收口计划

## 目标

一次性收口本轮 API 化后暴露出的治理问题，重点解决以下四类问题：

1. 保活定时仍依赖 GUI 存活，跨天时容易漏跑
2. `runtime` 作为临时执行层缺少稳定的生命周期治理，历史残留会累积
3. GUI 展示状态与 core 实际运行态存在偏差
4. 文档与运行语义没有把 `UserDataProfile* / mirror / runtime` 三层职责讲清楚

本轮完成后，验收标准如下：

1. 保活定时由 daemon/core 负责，GUI 不再承担定时触发职责
2. `runtime` 具备三层清理机制：
   - 会话关闭即时清理
   - daemon 启动/housekeeping 巡检清理孤儿 runtime
   - 保活完成后兜底清理孤儿 runtime
3. GUI 的保活状态、下一次运行、MCP 运行态均以 control API 返回为准
4. 文档与术语统一，明确正式持久层、镜像层、临时执行层的区别
5. 完成本地验证：
   - 保活状态接口正确
   - 删空 `runtime` 后 MCP 会话可重建
   - 会话结束后 runtime 正常回收
   - core 能根据计划时间判断是否应调度

## 实现范围

### 1. Core 侧保活调度

- 在 daemon 内增加 keepalive scheduler
- 调度判断直接读取配置中的：
  - `keepalive.schedule_time`
  - `keepalive.last_scheduled_run_date`
  - `profiles[*].keepalive_enabled`
- 调度器依赖 daemon 生命周期，不依赖 GUI 是否启动
- 触发源继续写为 `internal-schedule`

### 2. Runtime 清理治理

- 复用现有 `mirror_manager.cleanup_stale_runtimes()`
- 将其接入 daemon housekeeping
- 会话关闭后继续保留即时清理
- 保活任务完成后增加一次孤儿 runtime 清理
- 清理过程不得影响活跃 session

### 3. GUI 状态统一

- GUI 的保活“下一次运行/状态/结果”统一读取 control API 返回
- GUI 不再自己负责定时触发保活，只保留展示和手动触发
- MCP 勾选框语义与真实运行态继续分离展示：
  - 勾选框表示配置启用意图
  - 状态栏显示 control API 的实时状态

### 4. 文档同步

- 更新架构文档
- 更新 keepalive 集成/运行文档
- 明确：
  - `UserDataProfile*` 是正式持久层
  - `mirror_disk` 是镜像层
  - `runtime` 是临时执行层

## 验证计划

1. 代码级验证
   - `py_compile`
   - 相关模块导入验证

2. 功能验证
   - daemon 状态接口可返回 scheduler 运行态
   - 手动删除 `runtime` 后真实 MCP session 可成功启动
   - session 关闭后 runtime 被清理
   - keepalive control 状态接口返回计划信息

3. 回归验证
   - GUI 打开后能正确显示 keepalive 状态
   - GUI 不再因自身定时器承担调度职责

## 非目标

- 本轮不做 GUI 全面重构
- 本轮不切换保活执行引擎体系
- 本轮不处理具体业务网站逻辑正确性，只处理通用调度与运行治理
