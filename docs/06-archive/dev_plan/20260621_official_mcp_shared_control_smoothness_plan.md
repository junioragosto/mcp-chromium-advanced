# 官方 MCP 共享控制顺滑度对齐计划

## 背景

当前项目在复杂站点上已经具备较强能力，但在“AI 与人工共享同一浏览器会话”的体验上，仍明显弱于官方 `playwright-mcp`。

真实问题不是“浏览器请求被拦截”，而是：

1. 成功路径过重  
   动作成功后默认还会继续附加 `post_action_context`、`snapshot`、`page_text`、`structured_page`、`session_health`、`interaction_hints` 等采集。

2. Patchright 会话是单线程串行调度  
   这本身没有问题，但一旦单次动作后的附加采集过重，就会放大“人和 AI 在同一页面同时操作”的卡顿感。

3. 诊断/恢复能力默认挂在主链路上  
   这些能力本身有价值，但不应在“正常成功路径”中持续干扰页面。

因此本轮目标不是增加新的“人工模式/暂停模式/状态机”，而是把默认成功路径做薄，接近官方 `playwright-mcp` 的交互体验，同时保留现有诊断与恢复能力，改为按需触发。

## 总目标

在不削弱现有能力、不破坏多引擎架构的前提下，实现以下结果：

1. 默认成功路径接近官方 `playwright-mcp`
   - 常见动作执行后不再默认附带重型诊断链路
   - AI 与人工可以更自然地交替接管同一页面

2. 能力不削弱
   - `snapshot`
   - `diagnose_page`
   - `diagnose_target`
   - `wait_for_page_stable`
   - `structured_page`
   - `session_health`
   - `recovery hints`
   全部保留

3. 能力分层更清晰
   - 默认快路径只做最小必要动作确认
   - 重型诊断改为失败时、显式请求时、或明确恢复路径时再执行

4. 文档、skill、发布验证一并同步

## 设计原则

### 1. 不新增用户负担

不要求用户理解“人工模式”“接管模式”“暂停模式”等新概念。

目标是默认就更顺滑，而不是靠用户手动切换状态换取顺滑。

### 2. 能力保留但不默认打满

现有诊断/恢复能力不是要删除，而是从“默认总是执行”改为“按需执行”。

### 3. Patchright 优先对齐官方体验

本轮优先对象是默认引擎 `patchright`。  
`selenium_uc` 和 `playwright_cli` 跟进保持一致的链路策略，但不要求它们达到完全相同的体验上限。

### 4. 内核分层清晰

把：

- 动作执行
- 成功确认
- 失败恢复
- 重型诊断
- 观测增强

从当前的耦合状态中拆开，形成清晰边界。

## 现状问题拆解

### A. 动作后附加上下文过重

当前 `browser_session_kernel_diagnostics.py` 会在大量动作结果上自动附加：

- `post_action_context`
- `structured_page`
- `session_health`
- `recent_actions`
- `next_steps`

而底层 `patchright_engine.py` 的 `_post_action_context()` 又会拉：

- 当前 URL
- tabs 摘要
- active element
- modal state
- `aria_snapshot`
- page text preview

这会在复杂页面上显著放大一次动作的真实成本。

### B. 快路径和诊断路径未分离

当前很多高频动作虽然业务语义是“点击一下即可”，但框架层最后仍会走统一的重包装结果标准化。

这导致：

- 成功动作不够轻
- 人工操作期间页面仍被持续读取与分析
- 复杂站点更容易表现为“转圈”“迟滞”“提交不顺”

### C. 失败恢复粒度过粗

当前恢复能力存在价值，但需要更精确地只在“失败/漂移/定位异常”时触发，而不应影响每次成功动作。

## 本轮实现范围

### 一、引入“默认快路径 + 按需增强路径”分层

目标：

- 不改外部 MCP 工具接口
- 不改业务调用方式
- 只改内部执行策略

实现要求：

1. 为高频动作定义“默认快路径”
   - `click`
   - `click_target`
   - `type_text`
   - `type_target`
   - `press_key`
   - `select_option`
   - `navigate`
   - `navigate_back`
   - `navigate_forward`
   - `open_tab`
   - `activate_tab`
   - `close_tab`

2. 默认快路径只保留：
   - 动作执行结果
   - 最小页面确认信息
   - 必要的 tab/url/title 更新
   - 轻量 `page_drift` 信息

3. 默认快路径不再自动附带重型：
   - 完整 `post_action_context`
   - 大型 `snapshot`
   - `structured_page`
   - `session_health`
   - `recent_actions`
   - `next_steps`

### 二、保留完整诊断能力，但改为按需触发

以下能力必须保留：

- `snapshot`
- `diagnose_page`
- `diagnose_target`
- `wait_for_page_stable`
- `watch_page_state`
- `watch_target_state`
- 失败场景下的恢复提示

实现要求：

1. 成功结果默认走轻量返回
2. 失败结果保留增强诊断
3. 显式诊断工具继续输出完整上下文
4. 内核 fallback/recovery 在真正需要时仍可触发

### 三、重构 `post_action_context` 附加策略

目标：

把“是否附加重上下文”的判断做成统一内核规则，而不是散落在各处。

实现要求：

1. 统一梳理 `_should_attach_post_action_context()`
2. 明确区分：
   - 成功快路径动作
   - 失败动作
   - 显式诊断动作
   - 显式观测动作
3. 对成功快路径动作默认只附加最小上下文，或完全不附加重上下文
4. 对失败、诊断、恢复动作保留增强上下文

### 四、瘦身 Patchright 底层成功链路

目标：

让 `patchright` 的默认成功动作本身更薄，减少动作后立即拉取大量页面状态。

实现要求：

1. 梳理 `_post_action_context()` 的调用位置
2. 将 `_post_action_context()` 保留为“显式重型上下文构建器”
3. 为成功快路径提供更轻的页面确认机制
4. 对 `aria_snapshot()` 的默认触发严格收口

### 五、保持恢复与诊断质量

本轮不能为了提速而把诊断系统做坏。

必须保证：

1. 失败时仍能提供足够上下文
2. `diagnose_page` / `diagnose_target` 不退化
3. 快路径失败后可升级到诊断路径
4. 内核中已有的：
   - `page_drift`
   - `recovery_actions`
   - `structured_page`
   - `session_health`
   仍然可用

### 六、跨引擎策略一致化

目标：

虽然本轮重点是 `patchright`，但上层内核策略要一致，不要形成三套完全不同的结果语义。

实现要求：

1. `patchright` 作为第一优先
2. `playwright_cli` 跟随同样的“快路径/重诊断分离”策略
3. `selenium_uc` 至少在内核输出层保持一致

## 代码落点

### 核心改造文件

1. `chromium_advanced/browser_session_kernel.py`
   - 动作分发策略
   - fallback/恢复路径衔接
   - 成功快路径与失败增强路径的边界

2. `chromium_advanced/browser_session_kernel_diagnostics.py`
   - `post_action_context` 附加策略
   - 成功/失败标准化输出
   - 轻量/重型上下文分层

3. `chromium_advanced/browser_engines/patchright_engine.py`
   - `_post_action_context()` 使用边界
   - 成功动作后的重采集收口

4. `chromium_advanced/browser_engines/playwright_cli_engine.py`
   - 跟进轻量成功路径策略

5. 如有必要：
   - `README.md`
   - `README_zh.md`
   - `docs/skill_templates/browser-identity-mcp.SKILL.md`
   - `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
   - 系统 skill 同步说明

## 实现步骤

### Step 1. 梳理动作分类表

把所有工具动作分成：

1. 快路径动作
2. 观测动作
3. 诊断动作
4. 失败恢复相关动作

形成统一分类常量，避免后续规则散落。

### Step 2. 重构诊断附加策略

重写/收敛：

- `_should_attach_post_action_context()`
- `_attach_post_action_context()`
- `_normalize_result()`
- `_normalize_failure()`

实现：

- 成功快路径默认轻量返回
- 失败/诊断路径保留完整增强输出

### Step 3. 收口 Patchright 底层重采集

对 `patchright_engine.py`：

- 成功点击/输入/按键/导航后，不再直接默认走完整重型上下文
- 保留显式诊断和失败上下文构建能力

### Step 4. 校准 Playwright CLI / Selenium UC 的上层语义

即使底层能力不同，也要确保上层：

- 快路径结果轻量
- 失败结果增强
- 诊断动作完整

### Step 5. 回归测试与真实场景验证

重点验证：

1. GitHub 登录 + 2FA 人工输入后提交
2. Gmail 打开与轻量操作
3. YouTube Studio 评论后台切换与读取
4. AI 操作后人工接管、人工操作后 AI 接管

## 测试计划

### A. 单元/静态测试

1. 语法编译通过
2. 关键结果结构不破坏
3. 快路径动作仍返回必要字段
4. 失败结果仍包含诊断信息

### B. 内核行为验证

1. `click` 成功时不再默认返回重型上下文
2. `type_text` 成功时不再拉完整 snapshot
3. `navigate` 成功后仍能正确更新 URL/title/tab
4. `diagnose_page` 明确仍返回完整信息
5. 失败动作仍可生成增强上下文

### C. 真实交互验证

1. GitHub 2FA 场景
   - AI 打开登录流程
   - 用户手动输入 2FA
   - 用户手动点击提交
   - 页面应能正常提交，不表现为明显“持续转圈卡住”

2. AI/人工交替控制
   - AI 执行动作
   - 用户手动继续点选/输入
   - AI 再继续读取页面/执行动作
   - 不要求绝对零竞争，但要显著优于当前版本

3. 复杂页面稳定性
   - YouTube Studio
   - Gmail
   - GitHub

### D. 性能对比

1. 快路径动作平均耗时下降
2. 成功动作后的附加负担明显减少
3. 空闲时 CPU 不因这轮改造上升

## 验收标准

本轮视为达成的标准：

1. 默认快路径明显变薄
2. 人工与 AI 共用同一页面时，交互迟滞明显下降
3. GitHub 2FA 这类手动接管场景显著改善
4. `snapshot/diagnose_page/diagnose_target` 等能力没有消失
5. 文档与 skill 描述更新到最新事实

## 风险与约束

1. 不能为了顺滑而破坏失败诊断质量
2. 不能为了提速而删除结构化能力
3. 不能新增一套让用户理解成本更高的“模式切换”
4. 不能让不同引擎的结果语义进一步分裂
5. 后续若把官方 `@playwright/mcp` 正式接入主链路，发布包必须内置其运行时依赖，尤其是 `Node.js` 运行时；最终安装包和桌面版运行不得依赖用户机器预先安装系统 `node`

## 预期结果

本轮完成后，系统在浏览器实际操作层会更接近官方 `playwright-mcp` 的使用感受：

- 默认更薄
- 成功路径更快
- 人工/AI 更容易交替接管
- 复杂诊断能力仍完整保留

这不是削弱能力，而是把“重能力”从默认主链路中解耦出来。
