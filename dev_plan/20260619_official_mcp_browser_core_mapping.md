# 官方 Playwright MCP 对照映射

日期：2026-06-19

## 1. 对照目标

本说明只服务于当前这次“浏览器操作内核对齐”开发。

目标不是照搬官方实现，而是明确官方 `playwright-mcp` 在浏览器操作层面的成熟设计，并把这些设计映射到本项目的统一能力层。

参照来源：

- `tmp/official_playwright_mcp/README.md`
- `tmp/official_playwright_mcp/tests/*.spec.ts`

## 2. 官方设计要点

### 2.1 结构化 snapshot 是第一交互媒介

官方 README 明确把 `browser_snapshot` 作为核心交互介质，强调：

- 优先使用结构化快照，而不是截图
- target 可以是 snapshot ref，也可以是唯一 selector
- snapshot 可以带 `depth`
- snapshot 可以带 `boxes`

这说明官方的核心假设不是“先截图再猜”，而是：

- 先拿到页面的结构化交互树
- 再在这棵树上引用目标
- 再执行动作

### 2.2 target contract 强调“精确引用”

官方工具描述里，`click / select_option / generate_locator / verify / screenshot` 都统一使用：

- snapshot ref
- 或唯一 selector

这意味着：

- target 不是松散字符串
- target 应该可解释、可复用、可继续传递
- target 解析结果要天然服务后续动作

### 2.3 verify / wait 是一等能力，不是附属工具

官方把以下能力作为正式工具公开：

- `browser_verify_text_visible`
- `browser_verify_element_visible`
- `browser_verify_value`
- `browser_wait_for`

其中 `browser_wait_for` 统一支持：

- 等时间
- 等文本出现
- 等文本消失

这说明官方非常重视：

- 高层动作的结果可验证
- 等待逻辑是能力 contract 的一部分
- 返回值应该能直接支持后续推理

### 2.4 tabs / dialog / select 是正式操作语义

官方把这些都作为高层动作公开：

- tabs 管理
- dialog 处理
- select option

这意味着复杂页面上常见的交互模式不是“脚本兜底”，而是：

- 有稳定 contract
- 有稳定参数模型
- 有稳定返回语义

### 2.5 测试以行为 contract 为中心

官方测试文件里大量验证的是：

- snapshot 输出是否稳定
- click 后 snapshot/状态是否可继续用
- tool 列表与 contract 是否稳定

这说明官方的成熟点不只是“能跑”，而是：

- 工具 contract 可回归
- 结构化输出可回归
- 动作后的上下文可回归

## 3. 本项目对应模块映射

### 3.1 统一 target model

本项目对应模块：

- `chromium_advanced/browser_session_kernel.py`
- `chromium_advanced/browser_session_kernel_diagnostics.py`

当前承接职责：

- DOM fallback candidate 生成
- snapshot ref 映射
- target ranking
- target 解释信息

本轮已对齐的方向：

- target 带 `role/accessibile_name/text_preview/control_type`
- target 带 `selected/checked/expanded/disabled`
- target 带 `dialog/overlay/popup/custom-element` 线索
- target 带 `ranking_reason/match_reason`

### 3.2 结构化页面表达

本项目对应模块：

- `ManagedSessionDiagnosticsMixin._extract_structured_page_data`
- `ManagedSessionDiagnosticsMixin._extract_structured_region_data`
- `ManagedSessionDiagnosticsMixin._build_interaction_hints`

官方对应理念：

- 用结构化快照表达当前页面
- 让动作后的结果天然可继续推理

本项目本轮对齐点：

- 页面级 structured summary
- 区域级 structured summary
- interaction hotspot / region summaries
- dialog/menu/listbox/form/toolbar 等区域线索

### 3.3 高层动作 contract

本项目对应模块：

- `ManagedBrowserSession` 的 `click/type/select/wait/verify/open_tab/activate_tab/run_script_batch`

官方对应理念：

- 动作是高层 contract
- verify/wait/dialog/tabs 是正式能力
- 结果语义稳定

本项目对齐原则：

- 动作前统一 resolve / normalize
- 动作后统一 normalize result
- 失败后统一 error code / failure classification
- post-action context 可直接继续推理

### 3.4 复杂控件策略

本项目对应模块：

- `_build_resolution_scope`
- `_rank_entries`
- `_candidate_scope_details`
- `_extract_structured_region_data`

官方没有单独暴露“站点 adapter”概念，而是靠统一结构化语义完成多数复杂控件处理。

本项目本轮采用同样原则：

- popup / overlay
- dialog
- menu / menuitem
- combobox / listbox
- recent interaction affinity

都进入统一语义层，而不是做站点专用分支。

### 3.5 diagnostics / output contract

本项目对应模块：

- `diagnose_page`
- `diagnose_target`
- `interaction_context`
- `session_health`

官方重点：

- 输出不是大量噪音
- 输出应服务下一步动作

本项目本轮要求：

- 减少纯 HTML 噪音依赖
- 强化 structured page / region
- 增加 candidate ranking reason
- 增加 interaction hints / top regions

## 4. 需要遵守的实现结论

本轮开发必须坚持以下结论：

1. 结构化页面模型优先于截图式思路。
2. target 必须是可解释、可传递、可复用的统一模型。
3. wait / verify / dialog / tabs 不是附属能力，而是核心 contract。
4. 复杂控件优先通过统一语义层解决，不做站点 adapter。
5. 三引擎差异只应存在于执行层，不应破坏统一动作语义。

## 5. 本轮验收对应关系

当以下条件成立时，可以认为当前版本在浏览器操作内核层更接近官方：

- `list_candidates` 返回的不只是候选列表，还有可解释的排序原因
- `structured_page / structured_region` 可以直接指导下一步动作
- `verify / wait / tabs / select` 返回稳定合同字段
- 复杂页面下更偏向局部语义推理，而不是反复读整页文本
- 非 `patchright` 引擎也能吃到统一能力语义，而不是只剩基础动作
