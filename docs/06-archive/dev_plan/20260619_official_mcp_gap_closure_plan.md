# 官方 MCP 差距收口计划

日期：2026-06-19

## 1. 目标

本轮目标不是继续扩展引擎数量，而是把当前项目默认 `patchright` 主路径的真实使用体验，进一步向官方 `microsoft/playwright-mcp` 收敛，同时保持本项目已经形成的 profile 治理、多引擎、GUI、daemon automation、keepalive 和脚本接入优势不退化。

本轮执行方式：

- 按长任务模式持续推进
- 先本地测试，再发布验证
- 文档、skill 模板、系统 skill 必须同步

## 2. 当前差距总览

当前与官方 MCP 的主要差距，不再是“缺少哪些工具名”，而是：

1. 默认高层动作后的可继续推理信息密度仍偏低
2. 复杂动态页面上仍较容易走到 snapshot / fallback / diagnose 补调用链
3. 高频 verify / describe / diagnose 结果语义虽已改善，但还不够完全收敛
4. 默认路径的交互成本仍高于官方主路径
5. 性能与稳定性细节还可进一步压缩

## 3. 分层规划

### 3.1 必须补

这些项必须在本轮完成，否则不达到可交付标准。

#### A. 默认 `patchright` 成功动作的高信号上下文增强

问题：

- 当前 `post_action_context` 已存在，但成功动作多数仍只返回轻量快照
- agent 常常还要额外调 `snapshot` / `diagnose_page` / `list_candidates`

目标：

- 对默认主路径高频动作，在成功后直接返回更适合继续推理的数据
- 尽量减少额外补调用

实施：

1. 收敛 `post_action_context` 的结构
2. 在默认 `patchright` 路径上补充轻量结构化信号：
   - `structured_page`
   - `interaction_hints`
   - `primary_actions`
   - `search_controls`
   - `navigation_controls`
3. 保持 fast-path 轻量，不回退到重诊断

验收：

- 常见 `click` / `click_target` / `type_*` / `select_option` / `press_key` 成功后，不需要立刻再补一个 `browser_diagnose_page(...)` 才能继续

#### B. 高频读/验/诊断结果契约进一步统一

问题：

- `verify_*`、`describe_target`、`list_candidates`、`diagnose_*` 虽然已有统一趋势，但仍存在字段不稳定

目标：

- 对上层 agent 暴露更稳定的统一契约

实施：

1. 高频结果统一补齐：
   - `verified`
   - `matched`
   - `target_summary`
   - `expected_*`
   - `resolved_target`
   - `by`
2. 让 `verify_*` 成功结果尽量带足够局部上下文
3. 让 `describe_target` / `list_candidates` 的 top-level 摘要更适合自动推理

验收：

- 同类读/验动作结果结构更一致
- agent 不再频繁分支判断“这个动作到底返回的是 found 还是 verified”

#### C. 文档与 skill 同步到最新事实

问题：

- 文档已较新，但本轮新增的差距收口点如果不写清楚，调用方仍会按旧心智使用

目标：

- README / skill / 系统 skill 与当前行为完全一致

实施：

1. 更新 `README.md`
2. 更新 `README_zh.md`
3. 更新 `docs/skill_templates/browser-identity-mcp.SKILL.md`
4. 更新 `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
5. 更新系统 skill

验收：

- 默认引擎定位、推荐调用方式、动作后上下文心智与代码一致

### 3.2 应该补

这些项应尽量在本轮完成，除非测试表明风险过高。

#### D. 复杂页面结构化诊断进一步泛化

目标：

- 在不做站点专用 adapter 的前提下，让 `structured_page` / `structured_region` 更适合 Gmail / YouTube Studio / React / shadow-heavy 页面

实施：

1. 增强 `structured_page` 中的控件分组、候选动作、交互热点概括
2. 增强 `structured_region` 的局部动作视图
3. 优化 target-local 诊断的上下文信号

#### E. 减少默认补探测和重复诊断

目标：

- 减少官方不太会出现的“动作后还要自己多补一层探测”的情况

实施：

1. 在高层动作成功路径里补可复用摘要
2. 对失败路径保留重诊断
3. 成功路径尽量不做大体积 HTML / 全量日志采集

#### F. 发布验证回到真实复杂场景

目标：

- 不只过单元或接口，还要在真实场景里验证“任务链路是否更顺”

实施：

1. GitHub 登录态操作
2. Gmail 前几封标题读取
3. YouTube Studio 评论场景
4. 至少一轮多步骤复杂交互

### 3.3 可后置

这些项很重要，但不作为本轮阻断项。

#### G. 更深层性能优化

- 更细的 CPU/轮询治理
- 事件流替代部分拉式上下文采集
- 更进一步压缩 GUI / daemon 占用

#### H. 更进一步对齐官方的 inline snapshot / 单步返回模型

- 如果后续要再贴近官方丝滑感，需要更深地重构动作返回面
- 这会牵涉 worker / MCP tool surface / trace 模型，不适合在本轮硬塞

#### I. 更系统的 challenge / anti-bot 专项模型

- 当前已有 `anti_bot` 信号
- 后续可以做成更成熟的 challenge-aware 策略层

## 4. 本轮代码改造点

优先模块：

1. `chromium_advanced/browser_session_kernel.py`
2. `chromium_advanced/browser_session_kernel_diagnostics.py`
3. `chromium_advanced/browser_engines/patchright_engine.py`
4. `chromium_advanced/mcp_server.py`
5. README / skill / 系统 skill

## 5. 测试计划

### 5.1 本地测试

1. 结果契约回归
2. 高频动作成功路径上下文增强验证
3. 复杂页面诊断结构字段验证
4. 多引擎不回退验证

### 5.2 发布验证

按真实任务链路验证：

1. GitHub 登录态页面操作
2. Gmail 前几封标题
3. YouTube Studio 评论场景

要求：

- 先本地测试
- 再发布验证

## 6. 验收标准

满足以下条件才算本轮完成：

1. 默认 `patchright` 主路径的高频动作成功后返回更高信号上下文
2. `verify_*` / `describe_target` / `list_candidates` / `diagnose_*` 结果契约进一步统一
3. 文档、skill 模板、系统 skill 已同步
4. 本地测试通过
5. 发布验证通过

## 7. 非目标

本轮不做：

1. 新增第四套浏览器引擎
2. 做站点专用适配器
3. 推翻 GUI / daemon / worker 架构
4. 为了追官方而牺牲当前 profile 治理和安全边界

## 8. 执行顺序

1. 先补默认 `patchright` 成功动作上下文
2. 再补高频读/验/诊断结果契约
3. 再跑测试修问题
4. 再同步 README / skill / 系统 skill
5. 最后做发布验证
