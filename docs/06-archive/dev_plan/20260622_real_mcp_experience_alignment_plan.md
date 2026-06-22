# 2026-06-22 真实 MCP 体验对齐计划

## 1. 目标

本轮目标不是继续扩散功能面，而是把当前系统在“真实 MCP 使用体验”上继续向官方 `playwright-mcp` 对齐，并把三套主力引擎的职责边界进一步收紧。

本计划默认前提：

- 打包 / 发布 / workflow 由其他线程继续推进
- 本线程只关注内核能力、真实 MCP 体验、验证体系、文档/skill 对齐

本轮完成标准：

1. `official_playwright_mcp` 在真实 MCP 长链路下更稳定
2. `patchright` / `selenium_uc` 的专项定位更清晰
3. `playwright_cli` 不再被当成默认生产路径
4. 真实 MCP 验证脚本与验证流程形成固定基线
5. 文档和 skill 体现新的引擎分工与默认策略

## 2. 当前基线

基于本轮已经完成的真实 MCP 验证，当前结论是：

- `official_playwright_mcp`
  - 当前默认引擎
  - 最接近官方 `playwright-mcp`
  - 真实长链路最稳
- `patchright`
  - 复杂页面、live-root、结构化提取最强补强引擎
- `selenium_uc`
  - stealth / challenge / gesture / 坐标动作专项引擎
- `playwright_cli`
  - 轻量兼容 / 诊断路径
  - 不适合作为默认生产路径

当前短板不在“有没有这些工具”，而在：

1. 长链路稳定性
2. 少数脚本包装边界
3. 复杂页面结构化提取一致性
4. 多引擎真实验证基线还不够系统化

## 3. 核心方向

### 3.1 `official_playwright_mcp` 主引擎收口

目标：

- 继续把默认主引擎体验拉近官方

重点项：

1. 收口 `run_script` 包装边界
   - 消除这轮在 GitHub 首页出现的 `Unexpected token 'const'` 这类兼容问题
2. 提升真实 MCP 长链路稳定性
   - 减少 timeout
   - 减少 request cancelled / unknown message id 一类噪音
3. 收敛复杂页面上的结构化读写行为
   - Gmail
   - YouTube Studio
   - shadow-heavy / React / Polymer 页面
4. 把官方风格工具语义进一步统一
   - tabs
   - snapshot
   - diagnose
   - target-oriented actions

### 3.2 `patchright` 定位固化为复杂页面增强引擎

目标：

- 保留其 live-root 与复杂页面强项，不再和默认主引擎争角色定义

重点项：

1. 强化其在复杂后台站点下的结构化提取成功率
2. 确保它作为 fallback/增强引擎时，切换成本和行为说明足够清晰
3. 验证 patchright 在真实 MCP 工具流下的 page diagnosis / candidate / target action 一致性

### 3.3 `selenium_uc` 定位固化为 stealth / gesture 引擎

目标：

- 不再试图拿它和主引擎在所有后台站点结构化能力上做同类比较

重点项：

1. 把它的优势场景写清楚
   - anti-bot
   - challenge
   - slider / gesture / XY 路径
2. 把 gesture / coordinate 这类验证纳入固定基线
3. 确保真实 MCP 下对外暴露的能力与其实际强项一致

### 3.4 `playwright_cli` 明确降级为轻量兼容路径

目标：

- 不再让它承担默认生产角色

重点项：

1. 收紧默认文档、skill、说明中的推荐优先级
2. 排查真实 MCP 长链路超时与 SSE 异常
3. 如果短期无法收口稳定性，就明确把它限制在诊断/兼容场景

## 4. 实现任务

### 4.1 内核与桥接层

需要继续推进的代码面：

- `chromium_advanced/browser_session_kernel.py`
- `chromium_advanced/action_pipeline.py`
- `chromium_advanced/official_playwright_mcp_bridge.py`
- `chromium_advanced/browser_action_orchestrator.py`
- `chromium_advanced/browser_capability_kernel.py`

具体任务：

1. 统一 `run_script` / `run_script_batch` 的包装和错误归因
2. 继续压实 `native` 与 `standard` 路径的边界
3. 优化 MCP 长请求下的超时、取消、收尾行为
4. 保证失败时的 `resolution_trace` / `action_trace` / `diagnose_page` 对调用方更有用

### 4.2 引擎侧

需要继续推进的代码面：

- `official_playwright_mcp_engine.py`
- `patchright_engine.py`
- `selenium_uc_engine.py`
- `playwright_cli_engine.py`

具体任务：

1. `official_playwright_mcp`
   - 继续增强真实复杂页面下的稳定性
2. `patchright`
   - 保持复杂页面提取优势
3. `selenium_uc`
   - 强化 gesture / coordinate / anti-bot 验证支持
4. `playwright_cli`
   - 定位收缩
   - 稳定性问题单独治理

### 4.3 文档与 skill

需要继续更新：

- `README.md`
- `docs/04-operations/BROWSER_CORE_VALIDATION_PLAYBOOK.md`
- `docs/skill_templates/browser-identity-mcp.SKILL.md`
- `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`

具体任务：

1. 明确默认引擎策略
2. 明确三类专项引擎边界
3. 明确真实 MCP 验证口径
4. 让 skill 指导调用方优先用对引擎，而不是盲切

## 5. 验证计划

### 5.1 真实 MCP 核心验证

每一轮都必须走真实 MCP，而不是只走内部 automation：

1. `tools/list`
2. `start_profile_session`
3. 同一 `session_id` 内完成多步任务
4. `close_profile_session`

### 5.2 标准真实场景

固定验证矩阵：

1. GitHub 登录态 + 仓库页操作
2. Gmail 前 3 封邮件标题
3. YouTube Studio 评论后台读取
4. 手势/坐标页面
   - 用于 `selenium_uc`
5. challenge/反检测场景
   - 用于 `selenium_uc`

### 5.3 引擎分工验证

每次至少验证：

1. `official_playwright_mcp`
2. `patchright`
3. `selenium_uc`
4. `playwright_cli`

并区分两类样本：

1. 同一 profile 的公平样本
2. 不同 profile 的可用样本

### 5.4 稳定性验证

重点检查：

1. 长链路超时
2. session 关闭一致性
3. SSE / MCP 请求取消噪音
4. profile 占用状态是否一致

## 6. 验收标准

本轮要算完成，至少满足：

1. `official_playwright_mcp` 继续保持默认主引擎地位
2. GitHub / Gmail / YouTube Studio 三场景真实 MCP 验证稳定通过
3. `patchright` 在复杂页面场景继续证明增强价值
4. `selenium_uc` 的专项能力通过专门样本验证
5. `playwright_cli` 的定位与文档说明收敛，不再造成默认误用
6. 文档与 skill 与实际事实一致

## 7. 交付物

本轮交付应该包括：

1. 内核与引擎代码改动
2. 真实 MCP 验证脚本更新
3. 真实验证输出记录
4. 文档与 skill 更新
5. 新一轮真实 MCP 对比结论
