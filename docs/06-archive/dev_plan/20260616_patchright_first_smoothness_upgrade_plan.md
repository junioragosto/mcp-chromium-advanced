# Patchright-First Smoothness Upgrade Plan

## Context

当前项目已经具备真实可用性，但在“丝滑度、默认能力、复杂页面稳定性、状态一致性、长期工业化可维护性”上仍有明显差距。本轮不是继续做零散修补，而是围绕性能架构、默认引擎策略、能力层抽象和高频链路成本做系统级提升。

本计划是本轮长任务的唯一实施基线，开发、测试、验证、文档、skill 模板和系统内 skill 更新都应严格按本计划执行。

## Product Goal

本轮目标不是只让系统“能工作”，而是让它更像一个高性能、本地工业级浏览器身份运行时，也更像一个成熟的高质量浏览器自动化 MCP。默认体验要尽量接近官方高质量 Playwright MCP 的顺滑程度，同时保留本项目独有的真实身份、GUI 管理、保活、并发治理和脚本接入能力。

## Architecture Direction

本轮采用“两层能力模型”作为重构总方向。

### Identity Runtime Layer

负责 profile 生命周期、独占锁、GUI / MCP / keepalive / script 占用管理、API 认证、keepalive 调度、UserData / mirror / cache 清理、外部浏览器占用识别、worker / engine 启停调度、日志和错误归一化。该层必须低开销、状态一致、可观测、可恢复。

### Automation Capability Layer

负责页面导航、tab 管理、选择器 / locator 交互、结构化 DOM 提取、高级动作、调试能力和统一错误语义。该层必须以 `patchright` 为主，`selenium_uc` 负责反自动化与特殊场景增强，`playwright_cli` 作为轻量补充实现。

## Comparison Baseline

本轮对比的主要依据是官方 `playwright-mcp`，但评估维度拆成两条并行主线：一条是状态控制架构，一条是拿到浏览器会话之后的实际能力。状态控制层并不是次要问题，它直接决定并发治理、锁一致性、恢复能力、可观测性和整体稳定性；而浏览器会话后的动作质量、结构化提取、稳定性和复杂页面处理能力，则决定自动化体验上限。

对比维度固定为：

- 会话建立后第一跳能力是否顺滑
- 页面导航与 tab 操作是否自然
- locator / selector 命中稳定性
- 结构化提取是否足够强
- snapshot / inspect / diagnose / candidate 质量
- console / network / page error fidelity
- challenge / gesture / drag 处理能力
- 复杂前端页面上的成功率与 fallback 次数
- 连续多步任务的整体流畅度
- 错误语义是否清晰，是否容易定位失败原因

本轮优化优先级也据此排列：

1. 状态控制架构的正确性、一致性和恢复能力
2. 浏览器会话后的动作能力
3. 复杂前端页面结构化提取能力
4. 复杂交互和 challenge 处理能力
5. 错误语义与调试信息质量
6. 状态控制链路的轻量化与低开销

## Core Decisions

### 1. 默认引擎调整

引擎优先级调整为：

1. `patchright` 作为默认引擎
2. `selenium_uc` 作为增强反自动化 / challenge / gesture / 特殊站点引擎
3. `playwright_cli` 作为轻量集成引擎和补充实现

这意味着：

- GUI 默认值切到 `patchright`
- skill 模板默认推荐 `patchright`
- 文档里明确三种引擎的最佳适用场景
- 自动切换策略优先考虑 `patchright -> selenium_uc -> playwright_cli`

### 2. 优先解决性能架构问题

当前不丝滑，更多来自状态面采样过重、高频路径反复全量扫描、worker/session 生命周期过短、GUI 刷新策略不够克制、能力层包装损耗过多。本轮先解决运行时性能与状态治理，再做能力层增强。

### 3. 以官方 `playwright-mcp` 为能力 benchmark

本轮不要求直接引入官方实现，但要求在动作语义、结构化提取、fallback 探测、包装层损耗、结果语义和交互延迟上持续向其靠拢。

### 4. worker 重新定位

保留 worker 机制，但优化其启动策略、复用策略、空闲回收策略、与 daemon 的心跳和状态同步、日志与错误上报。

## Scope

### In Scope

- 运行时性能诊断与指标补全
- GUI/daemon/worker 状态面瘦身
- profile 状态一致性修复与占用模型强化
- 默认引擎切到 `patchright`
- 三引擎能力定位重写
- `patchright` 能力层增强
- `selenium_uc` challenge / challenge fallback / gesture 适配增强
- `playwright_cli` 降级为补充引擎并保留必要能力
- 会话生命周期优化和热复用优化
- 统一错误语义和 trace / log 输出
- 文档、release 文档、skill 模板、系统 skill 全量更新

### Out Of Scope

- 引入全新外部引擎栈替换当前三引擎体系
- 完整重做 GUI 交互视觉设计
- 引入新的分布式远程调度体系
- 彻底重写 keepalive 插件系统
- 大规模变更 0.0.0.0 / 内网暴露策略之外的网络产品形态

## Implementation Plan

### Phase A. Performance Baseline And Instrumentation

目标：先把“慢在哪里、重在哪里、抖在哪里”量化，不再靠主观体感修补。

实施项：

1. 为以下链路增加稳定 timing / counter / error-rate 采样：
   - `get_server_status`
   - `can_start_profile_session`
   - `start_profile_session`
   - `resolve_session`
   - `automation action`
   - `close_profile_session`
   - GUI profile list refresh
   - GUI keepalive state refresh
   - external Chromium detection
   - worker startup / warmup / teardown
2. 增加 profile 维度 runtime status snapshot，支持轻量缓存与时间戳。
3. 增加 daemon 内部健康指标：
   - active sessions
   - active workers
   - profile locks
   - stale locks
   - stale workers
   - failed actions
   - engine distribution
4. 日志补充 request-id / session-id / profile-name / engine-name / owner-source 字段。

交付标准：

- 能定位一次会话从申请到释放的完整耗时链路。
- 能区分“状态查询慢”还是“引擎动作慢”还是“worker 冷启动慢”。

### Phase B. Runtime Hot-Path Slimming

目标：把最频繁的查询和刷新改造成低开销路径。

实施项：

1. 对 daemon 状态面做缓存分层：热缓存、采样缓存、惰性计算。
2. 降低 GUI 全量轮询频率，高频仅刷新轻状态。
3. 收敛 external Chromium 扫描策略，避免每轮 GUI 更新都做高成本全扫描。
4. 收敛 profile 状态重新构建频率，优先增量更新。
5. 清理不必要的重复 JSON 序列化 / 反序列化 / 深拷贝路径。

交付标准：

- GUI 空闲 CPU 占用明显下降。
- 常规状态刷新不再引发明显卡顿。
- 高频 MCP 调用场景下状态查询成本可控。

### Phase C. Worker Policy Rework

目标：解决 worker 带来的冷启动感、频繁拉起关闭、不必要的终端闪烁和状态不稳。

实施项：

1. 重构 worker 生命周期：支持 warm worker、短时 hot retention，会话结束后不必立即销毁整个 worker。
2. 把动作路径改为尽量复用已就绪 worker，而不是每步都接近重新起一遍。
3. 收敛 worker 和 daemon 间的状态协议，减少冗余同步。
4. 统一 worker 异常上报和失联检测。
5. 明确强制回收机制：stale session、stale worker、orphan browser process。
6. 检查并修复可能导致命令行窗口一闪而过的子进程启动策略，尽量后台静默执行。

交付标准：

- 连续多步操作的体感延迟下降。
- worker 不再频繁无意义拉起/销毁。
- orphan 进程、残留窗口、会话归还不彻底的概率显著降低。

### Phase D. Default Engine Migration To Patchright

目标：把系统默认体验切到能力更强的引擎。

实施项：

1. 将默认 `browser_engine` 改为 `patchright`。
2. GUI 默认引擎选项同步更新。
3. MCP / daemon / worker / docs / skill 模板 / release 文档统一改口径。
4. 明确 `playwright_cli` 不再作为默认交互体验引擎。
5. 明确 `selenium_uc` 的定位为“特殊场景增强引擎”。

交付标准：

- 新装环境和默认配置下，主体验走 `patchright`。
- 文档、GUI、实际行为一致，不再出现默认引擎认知偏差。

### Phase E. Patchright-First Capability Upgrade

目标：以 `patchright` 为主线，补齐当前和官方高质量 Playwright MCP 的差距。

实施项：

1. 梳理当前 `patchright` 会话能力暴露面，补齐缺失或降级实现。
2. 优先增强以下能力：
   - 结构化提取
   - 稳定定位
   - snapshot / candidate / inspect / diagnose 质量
   - console / network / page error fidelity
   - tab / navigation / wait 语义一致性
3. 对复杂页面提取优化：
   - shadow DOM
   - custom elements
   - Polymer / SPA / lazy render 场景
4. 优化高层动作：减少 fallback 探测，减少“先试错再修正”的路径，更接近 locator-first 思路。
5. 为 challenge / 拖动 / gesture 场景预留更清晰的能力入口和结果语义。

交付标准：

- `patchright` 在 Gmail / YouTube Studio / GitHub 这类页面上的结构化提取能力明显优于当前版本。
- 默认场景更少依赖文本兜底和多轮试探。

### Phase F. Engine Strategy Layer

目标：把“三引擎共存”从历史包袱变成可理解、可选择、可调度的系统能力。

实施项：

1. 明确三引擎能力矩阵：
   - `patchright`: 默认、高保真、高能力、复杂前端优先
   - `selenium_uc`: 反自动化、challenge、gesture、风险站点
   - `playwright_cli`: 轻量、集成、低依赖补充路径
2. 增加引擎推荐与降级说明：GUI、skill 模板、README、release 文档统一说明。
3. 保留手动切换能力，并为未来“按场景自动切换”预留策略位。
4. 检查 session / status / error payload 中的 `engine_name` 语义一致性。

交付标准：

- 使用方知道什么时候该选哪个引擎。
- skill 模板不再含糊表述。
- 引擎切换不会破坏 profile 状态治理。

### Phase G. Documentation And Skill Synchronization

目标：实现完成后，所有对外说明材料必须和实际行为一致。

实施项：

1. 更新 `README`、中英文说明、release 文档。
2. 更新项目内 skill 模板：主 skill、WSL skill、相关辅助模板。
3. 更新本机系统内安装的 Codex skill 模板，确保实际使用和仓库保持一致。
4. 更新接入文档，明确默认引擎、引擎切换方式、并发模型、profile 占用规则、适合 challenge 的引擎选择、无痕 / 并发 / GUI / 脚本共用边界。

交付标准：

- 仓库文档、release 文档、skill 模板、系统 skill 同步一致。
- 后续业务线程和 AI 使用时，不会再因 skill 误导而产生明显偏差。

## Detailed Technical Targets

1. `patchright` 成为默认主体验引擎。
2. GUI 空闲时 CPU 占用明显下降，常规交互无明显卡顿。
3. `can_start_session`、`get_server_status`、profile 状态查询明显提速。
4. worker 冷启动 / 销毁抖动显著收敛。
5. 复杂页面结构化提取能力提升，减少仅依赖页面文本的情况。
6. 同一 profile 的锁状态、归还状态、人工占用、MCP 占用、keepalive 占用表达一致。
7. 多引擎在错误语义、日志字段、trace 字段上达到统一标准。
8. 技术文档和实际系统行为保持一致。

## Testing Plan

### 1. Unit And Module Tests

覆盖：runtime state cache / sampling、lock / occupancy / stale recovery、worker lifecycle policy、engine default resolution、engine capability matrix / normalization、error payload normalization、log/trace field completeness。

### 2. Integration Tests

覆盖：GUI -> daemon 状态刷新链路、daemon -> worker 会话启动链路、same-profile 冲突阻断、different-profile 并发启动、manual browser / keepalive / MCP 三种占用状态切换、engine switch without profile corruption。

### 3. Real Scenario Tests

至少验证以下真实链路：

1. `Profile 1` YouTube Studio 评论汇总
2. `Profile 1` Gmail 前 3 封标题提取
3. `Profile 1` GitHub 登录态页面操作
4. 一个 challenge / anti-bot 场景，优先用 `selenium_uc`
5. 一个复杂前端结构化提取场景，对比 `patchright` 和 `playwright_cli`
6. 一次并发场景，验证不同 profile 并行调用
7. 一次无痕模式场景，验证与常规登录态隔离

### 4. Performance Regression Tests

对比改造前后：首次 `get_server_status` latency、`can_start_profile_session` latency、`start_profile_session` latency、连续多步操作平均 action latency、会话释放 latency、GUI 空闲 CPU、GUI 常规刷新 CPU、worker cold-start 次数、orphan process 数量。

## Validation Plan

### Validation Principle

验收以真实行为为准，不以“代码看起来合理”为准。

### Validation Steps

1. 本地单元和集成测试全部通过。
2. GUI / daemon / worker 本地编译运行通过。
3. 桌面替换版进行真实业务型验证。
4. 按真实场景完成多引擎、多 profile、多模式验证。
5. 对照本计划逐项验收，不允许只做部分实现就视为完成。

### Validation Record Requirement

需要留下：实测结论、性能前后对比、引擎策略验证结果、已知剩余限制、文档/skill 是否同步完成。

## Acceptance Criteria

满足以下条件，才算本轮完成：

1. 默认引擎已切到 `patchright`。
2. `selenium_uc`、`playwright_cli` 定位和文档已重写清楚。
3. GUI 卡顿和空闲 CPU 问题有实质改善。
4. 状态查询和会话管理的高频路径已经瘦身。
5. worker 生命周期策略已优化，不再频繁无意义拉起/销毁。
6. 复杂页面结构化提取能力明显提升。
7. 并发、同 profile 冲突、人工占用、keepalive 占用状态一致。
8. 文档、skill 模板、系统 skill 已更新到最新事实。
9. 完成真实场景验证，并形成可说明的结果。

## Risks

1. 运行时瘦身后，可能引入状态刷新延迟或 GUI 观感不同步。
2. 默认切到 `patchright` 后，部分此前依赖 `playwright_cli` 的边界行为可能变化。
3. worker 热复用若处理不当，可能带来 stale context 或资源泄漏。
4. 能力层增强过程中，可能暴露更多跨引擎差异。
5. 文档和 skill 若未同步，实际使用会继续出现认知偏差。

## Risk Controls

1. 所有状态缓存必须带时间戳和强制刷新路径。
2. 引擎切换行为必须通过真实场景回归验证。
3. worker 热复用需要配套 stale recovery 和强制清理机制。
4. 明确每个引擎“不支持什么”，避免伪造一致能力。
5. 文档、skill、系统 skill 更新作为验收强制项，而不是附加项。

## Documentation Deliverables

本轮完成后必须同步更新：

- `README.md`
- 中英文 release 文档
- 项目内 skill 模板
- `browser-identity-mcp-wsl` 模板
- 其他相关模板
- 系统内安装的 Codex skill 文件
- 若有必要，补充新的接入说明或性能说明文档

## Execution Notes

1. 本轮按长任务执行，不以“下一轮修补”作为默认结束方式。
2. 开发完成后，必须按测试计划和验证计划严格走完。
3. 验证时要优先看真实并发、多 profile、复杂页面、复杂交互，而不是只看 demo。
4. 若中途出现新问题，优先判断是否属于本轮目标范围；属于则一并收口，不留明显遗留。
5. 最终汇报应以“是否达到本计划目标”为主，而不是罗列零散改动。


## Additional Delivery Constraints

### 1. Official `playwright-mcp` Reference Baseline

This iteration must not only mention the official `playwright-mcp` as inspiration. It must explicitly borrow mature patterns that have already been validated in the official implementation and public project materials.

Required reference directions:

- prioritize structured page understanding over raw HTML dumping
- keep tab, wait, and action semantics stable and predictable
- control tool response size so large pages do not flood context
- reduce dependence on arbitrary script fallback for mainstream tasks
- preserve strong Unicode and text-safety behavior in extracted results and docs

Reference sources for this iteration:

- official repository: https://github.com/microsoft/playwright-mcp
- official releases: https://github.com/microsoft/playwright-mcp/releases
- issue discussions relevant to response-size and text-handling maturity: https://github.com/microsoft/playwright-mcp/issues

The goal is not to clone the official project, but to absorb the mature parts of its design and bring this system closer to that standard while preserving real-profile identity management.

### 2. No Partial Stop Rule

This long task must not stop at partial progress milestones. It only ends when the whole delivery chain is complete.

It is not acceptable to stop at any of the following states:

- code changed but not self-reviewed
- source-tree tests passed but packaged runtime not validated
- binaries built but not replaced into install root
- install root replaced but real-world validation not completed
- runtime behavior updated but docs / skill templates / system skills not synchronized

The only acceptable stop condition is:

- implementation complete
- self-review complete
- source tests complete
- package build complete
- install-root replacement complete
- runtime restart complete
- real validation complete
- docs and skills synchronized complete

### 3. Mandatory Review Stage

Before final delivery, the iteration must include an explicit review pass that checks:

- engine contract consistency
- session lifecycle consistency
- occupancy and recovery paths
- GUI / daemon / worker state agreement
- error semantics and trace shape
- packaging completeness
- Windows encoding safety for all changed Chinese-facing files

### 4. Mandatory Installed-Runtime Validation

This iteration is not complete until installed binaries are validated from the real install root.

Required installed-runtime steps:

1. build GUI, daemon, and worker packages
2. replace the install-root binaries
3. restart GUI, daemon, and worker as needed
4. validate default engine behavior from installed runtime
5. validate at least one real workflow from installed runtime
6. validate release / cleanup / session return from installed runtime

### 5. Mandatory Validation Script Set

Validation must be fixed and reusable, not described ad hoc each time.

Required validation layers:

- large-version release validation script / skill
- smaller smoke validation script
- default `patchright` validation
- `selenium_uc` special-scenario validation
- challenge-like scenario validation
- incognito validation
- concurrent multi-profile validation
- installed-runtime validation

### 6. Documentation And Skill Delivery Is Part Of The Core Scope

Documentation and skill synchronization are not optional tail work. They are part of the core delivery target for this iteration.

Required synchronized outputs:

- `README`
- `README_zh`
- release docs
- integration docs
- in-repo skill templates
- WSL skill template
- installed system Codex skill

The completion condition is not satisfied unless these materials are updated to reflect:

- `patchright` as default engine
- engine strengths and boundaries
- task-scoped session reuse expectations
- installed-runtime behavior
- concurrency and occupancy rules
- challenge / incognito / special-engine usage guidance

### 7. Encoding Governance Is Mandatory

Because this repository already has a history of Chinese text corruption and mojibake on Windows, every Chinese-facing file changed in this iteration must pass explicit encoding verification.

At minimum, this applies to:

- Chinese README
- Chinese release docs
- Chinese plan documents
- Chinese skill templates
- any GUI translation resources touched during the iteration

Encoding validation is a blocking gate, not a best-effort step.
