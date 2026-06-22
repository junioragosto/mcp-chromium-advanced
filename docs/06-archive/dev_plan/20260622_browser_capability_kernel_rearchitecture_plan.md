# 浏览器能力内核重构计划

日期：2026-06-22  
范围：浏览器能力层 / 动作编排层 / 引擎原生能力释放 / 官方 `playwright-mcp` 对齐  
状态：待执行

## 1. 背景

当前项目已经具备成熟的本地浏览器身份治理能力：

- `GUI + daemon + control API + automation API`
- `SessionManager + occupancy + runtime + keepalive + mirror`
- 多引擎接入：`official_playwright_mcp`、`patchright`、`selenium_uc`、`playwright_cli`

但浏览器能力层仍然存在明显结构性短板：

1. 当前 `BrowserSession` 抽象偏扁平，强引擎进入系统后会被统一接口压缩。
2. `ActionPipeline` 仍更接近“统一转发器”，而不是“能力编排器”。
3. 原生强能力没有成为一等公民，很多动作仍被包裹成标准方法后再下发。
4. `official_playwright_mcp` 虽已成为默认主引擎，但其原生能力仍未完全释放。
5. 新增引擎或未来接入新的 MCP 内核时，当前框架存在“能接入，但能力保真度不足”的风险。

本次重构目标不是修改 GUI、会话治理或 Profile 管控逻辑，而是升级浏览器能力内核，使系统从：

- “统一治理层 + 统一方法接口”

升级为：

- “统一治理层 + 能力编排层 + 引擎原生能力层”

## 2. 总目标

本轮重构完成后，系统应达到以下目标：

1. 保留现有身份治理、Profile 占用、并发、keepalive、mirror、控制 API 架构不变。
2. 解除当前统一接口对强引擎能力的压制。
3. 让 `official_playwright_mcp` 成为真正的一等原生后端，而不是被适配后再使用的能力来源。
4. 让 `patchright`、`selenium_uc`、`playwright_cli` 也能按各自强项暴露原生能力。
5. 引擎接入方式从“实现一整套扁平方法”升级为“声明能力 + 注册动作 + 提供原生实现”。
6. 标准动作执行路径可观测、可比较、可回退。
7. 新引擎 / 新 MCP 后端接入时，不再要求先被压缩成弱抽象。

## 3. 非目标

本轮不做以下事项：

1. 不重写 GUI 产品交互。
2. 不改动 Profile 数据目录、镜像、keepalive 插件体系的基础产品逻辑。
3. 不移除旧引擎。
4. 不以“删减能力”换取架构整洁。
5. 不引入新的外部浏览器后端作为本轮主任务。

## 4. 问题定义

### 4.1 当前结构性问题

1. `BrowserSession` 既承载最小生命周期接口，又承载大量高层动作，职责过宽。
2. `ActionPipeline` 对动作的选择逻辑过薄，缺少“能力解析 -> 执行计划 -> 路径选择”。
3. 引擎能力描述仍主要停留在布尔 capability，无法精细表达：
   - 原生支持哪些动作
   - 哪些动作更适合 native path
   - 哪些动作需要 fallback
   - 哪些动作不建议强行统一
4. `official_playwright_mcp` 这类强引擎在进入系统后，仍有多层额外包装与重复 roundtrip。
5. 新能力新增时，往往需要扩张统一接口，而不是注册扩展能力。

### 4.2 直接风险

1. 官方后端能力无法完整释放。
2. 后续再接入强引擎时，仍会重复“接入成功，但体验不如原生”的问题。
3. 高层动作数量继续膨胀后，维护成本急剧上升。
4. 浏览器动作层很难形成可验证的性能和稳定性基线。

## 5. 目标架构

### 5.1 总体分层

保留原有：

- GUI 层
- daemon / control / automation API 层
- `SessionManager`
- occupancy / runtime / keepalive / mirror

重构浏览器能力内核为三层：

1. `Base Session Layer`
2. `Capability Orchestration Layer`
3. `Native Engine Capability Layer`

### 5.2 Base Session Layer

该层只保留任何引擎都必须提供的最小公约数能力：

- 生命周期：创建、关闭、summary
- 会话元数据：engine name、runtime mode、basic capabilities
- 基础页面能力：tabs、navigate、screenshot、current summary

约束：

1. 该层不再承载大量高层业务动作。
2. 该层是治理层依赖的最低接口，不直接决定高层动作如何执行。

### 5.3 Capability Orchestration Layer

这是本轮重构的核心。

职责：

1. 接收上层动作请求。
2. 读取引擎能力描述。
3. 生成动作执行计划。
4. 决定该动作：
   - 走原生实现
   - 走标准组合实现
   - 走 fallback
   - 拒绝执行
5. 输出统一的执行结果和 trace。

该层替代当前“简单分发表”式 `ActionPipeline`。

### 5.4 Native Engine Capability Layer

每个引擎必须允许暴露：

1. 原生动作集
2. 引擎扩展动作集
3. 推荐执行路径
4. fallback 策略
5. 交互模型声明

该层允许：

- `official_playwright_mcp` 保留原生工具风格能力
- `patchright` 保留页面对象/交互强能力
- `selenium_uc` 保留反检测、人工协作强项
- `playwright_cli` 保留其轻量集成路径

## 6. 核心设计

### 6.1 能力描述模型升级

新增统一能力描述模型，至少包含：

- `base_capabilities`
- `standard_actions`
- `native_actions`
- `preferred_paths`
- `fallback_policies`
- `interaction_model`
- `human_takeover_model`
- `performance_profile`

示例能力维度：

- 页面上下文提取
- 结构化 snapshot
- ref-based targeting
- native click/type/hover
- dialog / console / requests
- mouse / drag / gesture
- batch evaluate
- shared human control
- stealth / anti-detection profile

### 6.2 动作注册机制

动作不再只通过 `BrowserSession` 方法名隐式暴露，而是通过显式注册：

- 标准动作注册
- 原生动作注册
- 扩展动作注册

每个动作注册项至少描述：

- `action_name`
- `path_type`: `native` / `standard` / `fallback`
- `engine_scope`
- `required_capabilities`
- `cost_profile`
- `supports_shared_human_control`

### 6.3 动作编排器

重构 `ActionPipeline` 为动作编排器：

输入：

- 动作名
- 动作参数
- 当前引擎能力描述
- 运行时上下文

输出：

- 执行计划
- 执行路径
- 结果
- trace

执行顺序：

1. 解析动作语义
2. 查询引擎声明的 native action
3. 若存在，走 native path
4. 若不存在，查询 standard composition
5. 若仍不存在，再查询 fallback
6. 若不满足条件，返回清晰错误

### 6.4 原生能力直通

新增“原生动作直通”机制，使 daemon / automation API 在需要时可以显式调用：

- 原生动作
- 扩展动作
- 原生批处理动作

要求：

1. 标准调用方仍可只使用通用动作。
2. 高阶调用方可选择原生扩展。
3. GUI 和上层治理不需要理解每个原生动作的细节。

### 6.5 统一结果与 trace

所有动作最终仍要输出统一的结果结构，但保留路径信息：

- `action_name`
- `engine_name`
- `path_type`
- `native_action_name`
- `used_fallback`
- `duration_ms`
- `trace_id`
- `diagnostic_context`

这保证：

1. 上层接口稳定。
2. 下层能力不被压缩。
3. 性能与稳定性可观测。

## 7. `official_playwright_mcp` 专项目标

本轮要求 `official_playwright_mcp` 成为第一批完整接入新能力内核的引擎。

### 7.1 原生优先

以下高频动作优先走原生实现：

- page context
- current page summary
- inspect/list candidates
- target click / type / hover
- batch evaluate
- wait / watch
- structured snapshot
- dialog / console / requests
- gesture / drag

### 7.2 减少重复 roundtrip

重点压缩：

- 同一动作内部重复获取 `current_url`
- 重复 DOM evaluate
- 仅为补 summary 再次请求页面
- 单次动作内多次无意义原生桥接往返

### 7.3 批量上下文能力

补齐统一页面上下文提取，例如：

- `page_context`
- `target_context`
- `structured_page_snapshot`
- `interaction_context`

这些能力需优先提供批量结构化返回，而不是多段零散 evaluate。

## 8. 其他引擎的迁移策略

### 8.1 `patchright`

目标：

- 迁移到新能力描述模型
- 释放复杂页面、交互能力、结构化提取强项
- 成为“复杂互动页面优先引擎”

### 8.2 `selenium_uc`

目标：

- 保留强反检测、强人工协同价值
- 明确其更适合 challenge / anti-bot / shared human control 场景
- 不强行让其承担所有统一高阶动作

### 8.3 `playwright_cli`

目标：

- 保留其作为轻量集成后端的角色
- 接入新能力描述模型
- 明确其能力边界和 fallback 策略

## 9. 兼容策略

本轮必须渐进迁移，不能大爆炸。

迁移顺序：

1. 保留现有 `BrowserSession` 路径
2. 引入新能力描述模型
3. 引入动作注册与编排器
4. 先接 `official_playwright_mcp`
5. 再迁 `patchright`
6. 最后迁 `selenium_uc` / `playwright_cli`

兼容要求：

1. 现有 automation API 路径不应中断。
2. 现有 control API / GUI 状态展示不应退化。
3. 未迁移完成的引擎仍可通过旧路径运行。
4. 迁移期间允许双路径共存，但必须能明确观测路径来源。

## 10. 代码结构调整目标

建议新增或拆分的模块方向：

- `browser_capabilities/`
- `browser_action_registry/`
- `browser_action_orchestrator/`
- `browser_native_actions/`
- `browser_action_trace/`

期望效果：

1. 引擎能力声明与动作注册分离。
2. 动作编排逻辑独立于具体引擎。
3. 原生动作实现不再堆在统一 session 类中。
4. 引擎扩展能力有明确归属。

## 11. 测试计划

### 11.1 单元测试

新增或强化：

1. 能力描述解析测试
2. 动作注册与选择测试
3. 原生优先 / fallback 路径选择测试
4. trace 输出测试
5. GUI 背景刷新与缓存行为测试

### 11.2 集成测试

覆盖：

1. daemon automation action 路径
2. 官方引擎高频动作
3. patchright / uc / cli 的能力声明与路径选择
4. 原生动作直通调用
5. 人机协同场景

### 11.3 性能测试

需要比较：

1. 本轮前后 `official_playwright_mcp` 的 acquire / action / release 耗时
2. 常见动作的 native path 与 fallback path 耗时
3. 状态查询、GUI 刷新、后台轮询的 CPU 影响

### 11.4 真实验证

验证场景至少包括：

1. GitHub 登录态页面交互
2. Gmail / YouTube Studio 复杂页面结构化读取
3. challenge / shared human control 场景
4. gesture / drag / target interaction 场景
5. 并发多 profile 使用

## 12. 验收标准

本轮完成的最低验收标准：

1. 新能力描述模型落地。
2. 动作编排器落地，并取代当前纯分发表逻辑。
3. `official_playwright_mcp` 完成第一批原生优先接入。
4. 至少一批高频动作去除重复 roundtrip。
5. automation API 继续稳定可用。
6. 全量测试通过。
7. 安装态构建、替换、启动、验证通过。
8. 文档与 skill 模板同步更新。

## 13. 交付产物

本轮交付应包含：

1. 重构后的能力内核代码
2. 测试用例
3. 相关文档更新
4. 发布验证记录
5. 成熟度评估与剩余差距说明

## 14. 执行原则

1. 优先保持系统连续可运行。
2. 不以简化架构为理由削弱能力。
3. 不把强引擎再次压缩回弱抽象。
4. 一切设计以“释放原生能力、提升上限、保持统一治理”为第一原则。

