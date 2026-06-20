# 浏览器核心差距一次性收口总计划
日期：2026-06-20

## 1. 目标

本计划的目标只有一个：

`把当前项目在浏览器实际操作层面相对官方 playwright-mcp 的主要差距一次性收口，不再做零散修补，而是形成一轮完整的能力、性能、引擎分工、验证体系和发布标准升级。`

本计划覆盖 5 条主线，并要求在同一轮开发中整体完成：

1. `run_script` 读回能力对齐官方
2. 高层浏览器能力继续向官方 MCP 靠齐
3. 三引擎角色与产品化边界彻底明确
4. 发布后真实性能治理
5. 真实业务回放式验证体系

不做“必须 / 应该 / 后置”分期。本计划中的内容都属于本轮目标。

## 2. 当前问题归纳

### 2.1 `run_script` 读回仍有真实差距

当前已知边界：

- 在复杂动态页面上，`run_script(...)` 可能执行成功但返回 `result=null`
- 调用方很难直接区分：
  - 页面仍在正常运行，但结果序列化失败
  - 返回值本身不可序列化
  - 页面节点短暂失效
  - 脚本命中了运行时边界
- 这会让脚本读取体验仍弱于官方 `playwright-mcp`

### 2.2 高层动作虽然已增强，但仍不够“天然顺手”

当前虽然已有：

- `structured_page`
- `browser_list_candidates(...)`
- `browser_get_interaction_context(...)`
- richer `post_action_context`

但仍存在：

- 复杂页面下，高层动作和结构化读回还不够天然联动
- 调用方仍可能需要多次探测才能完成一条动作链
- 复杂控件、局部弹层、动态区域切换的默认策略还可以更强

### 2.3 三引擎虽然已拆分，但产品层表达还不够彻底

当前事实已经很明确：

- `patchright` 应是默认主力
- `selenium_uc` 应是 challenge / stealth / gesture 主力
- `playwright_cli` 应是轻量兼容与诊断路径

但还需要进一步彻底化：

- 让调用建议、文档、skill、接口使用习惯完全一致
- 让不同任务类型更容易自动选择正确引擎
- 让调用方不会继续把三个引擎理解成完全等价的“随便选一个”

### 2.4 真实性能仍需按产品标准治理

虽然功能链路已通，但从产品体验看还要继续压实：

- 空闲 CPU 占用
- 会话频繁启动/关闭成本
- GUI 状态刷新成本
- 诊断与 trace 输出的信噪比
- 复杂任务里的额外 round-trip 成本

### 2.5 验证体系还不够工程化

当前已经有真实场景验证，但还缺：

- 固定的大版本验证剧本
- 固定的小版本 smoke 剧本
- 固定的发布前验证门槛
- 固定的验证记录格式

## 3. 本轮实现范围

### 3.1 `run_script` / `run_script_batch` 读回升级

本轮必须完成：

- 统一脚本返回值封装协议
- 明确区分以下几类状态：
  - `ok + structured result`
  - `ok + null result`
  - `ok + non-serializable result`
  - `script threw`
  - `page context unstable`
- 对常见返回值类型做统一高保真序列化：
  - primitive
  - object
  - array
  - nested object/array
  - DOM-like extraction result
- 对失败状态补结构化诊断字段，而不是只给异常文本
- `run_script_batch(...)` 继续补强聚合结果和逐项诊断

交付要求：

- 调用方可以明确知道“脚本执行失败”和“脚本执行成功但未拿到有效结果”的区别
- 复杂页面上的脚本读回稳定性明显提升
- 文档和 skill 明确写清楚新的返回语义

### 3.2 高层动作与结构化上下文联动升级

本轮必须完成：

- `click / type / select / wait / verify` 与结构化上下文更强耦合
- 动作前优先使用局部上下文而不是全页泛扫
- 动作后返回更可继续推理的 `post_action_context`
- 对弹层、对话框、菜单、组合框、列表框做更稳定的默认策略
- 对复杂控件失败场景给出更结构化的恢复建议

交付要求：

- 普通复杂页面任务更少依赖 raw script 探测
- 高层动作链更接近官方 `playwright-mcp` 的“自然连续感”

### 3.3 复杂页面结构化提取继续增强

本轮必须完成：

- 强化复杂动态页面下的结构化摘要质量
- 优先保证以下信息更稳定：
  - 当前交互热点区域
  - 局部弹层/菜单/对话框
  - 表单控件和可交互控件
  - 候选项排序理由
  - 下一步动作建议
- 减少低价值 HTML / 大段文本型噪音输出

交付要求：

- Gmail / YouTube Studio / GitHub 这类复杂页面上，结构化输出对下一步动作更有指导性
- 复杂任务更少退回到“先读整页文本再猜”

### 3.4 三引擎角色彻底产品化

本轮必须完成：

- 在代码、README、skill 模板里统一三引擎定位
- 明确默认引擎和推荐切换场景
- 对以下任务给出清晰默认建议：
  - 结构化提取
  - 复杂前端操作
  - stealth / challenge
  - gesture / slider / pattern unlock
  - 轻量浏览与低开销诊断
- 统一 skill 中的引擎选择指引

目标定位固定为：

- `patchright`
  - 默认主力
  - 优先用于结构化提取、复杂前端、多步浏览任务
- `selenium_uc`
  - 优先用于 stealth、challenge、gesture、坐标级 fallback
- `playwright_cli`
  - 优先用于轻量兼容、低开销执行、基础诊断

交付要求：

- 使用方不再把三引擎理解成完全同级别、无差异的可互换实现
- skill 和文档能直接指导 agent 选引擎

### 3.5 真实性能治理

本轮必须完成：

- 复查空闲 CPU 占用路径
- 复查高频状态刷新和轮询路径
- 复查高频 trace / log / diagnostics 产生路径
- 复查动作链中的多余 round-trip
- 复查会话频繁起停场景的额外成本

交付要求：

- 形成明确的性能基线
- 至少压掉当前最明显的空闲和高频探测浪费
- 文档中给出当前可接受性能标准

### 3.6 发布验证体系工程化

本轮必须完成：

- 固化一套“大版本发布验证剧本”
- 固化一套“小迭代 smoke 剧本”
- 固化验证记录模板
- 让发布验证不再临时拼凑

大版本验证至少包含：

- 默认引擎 `patchright` 真实登录态复杂场景
- 至少一个并行场景
- 至少一个无痕场景
- 至少一个 challenge / stealth / gesture 类引擎切换场景
- cleanup 与占用状态回收验证

小版本 smoke 至少包含：

- 启动
- 基础导航
- 一个结构化读取
- 一个高层动作
- 一个会话释放

## 4. 测试计划

### 4.1 单元与内核测试

继续扩充并运行以下方向测试：

- `run_script` / `run_script_batch` 返回语义
- target resolution / candidate ranking
- structured page / structured region
- post-action context
- diagnostics contract
- 三引擎统一 contract

### 4.2 集成测试

必须覆盖：

- `patchright` 默认主路径
- `selenium_uc` stealth / gesture 路径
- `playwright_cli` 轻量路径
- daemon automation 的 `incognito` / `resource_only` 相关路径

### 4.3 真实性能测试

必须记录：

- 空闲 CPU
- 高频状态刷新 CPU
- 典型复杂流程的动作耗时
- 高层动作替代 raw probe 后的 round-trip 变化

## 5. 发布验证计划

### 5.1 大版本真实场景验证

至少执行以下真实场景：

- `Profile 1` Gmail 前 3 封标题
- `Profile 1` GitHub 登录态页面操作
- `Profile 1` 或可用登录态下的复杂动态页面结构化读取
- 并行双 profile 验证
- daemon automation 无痕验证
- 至少一个 `selenium_uc` 场景验证其 challenge / gesture / stealth 优势

### 5.2 验收门槛

只有全部满足才算本轮完成：

- 代码实现完成
- 文档与 skill 同步完成
- 单元 / 集成测试通过
- 打包编译通过
- 安装版启动验证通过
- 真实复杂场景验证通过
- 并行验证通过
- 无痕验证通过
- cleanup 验证通过
- 关键性能指标达到本轮标准

## 6. 文档更新范围

本轮必须同步更新：

- `README.md`
- `README_zh.md`
- `docs/skill_templates/browser-identity-mcp.SKILL.md`
- `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
- 系统内对应 skill
- 发布验证说明
- 若新增测试剧本，补充到本地验证说明中

## 7. 最终交付定义

本轮完成后的目标状态应是：

`默认 patchright 路径下，浏览器操作体验进一步接近官方 playwright-mcp：高层动作更自然，复杂页面结构化读回更强，run_script 读回语义更清晰，三引擎分工更成熟，性能和验证体系也更产品化。`

这轮不是“再补几个点”，而是一次完整的浏览器核心产品化升级。
