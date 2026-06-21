# 官方 MCP 对齐迭代二期计划

日期：2026-06-18

## 1. 目标

本轮不是继续堆叠新引擎，也不是做站点专用适配，而是继续把默认 `patchright` 主路径的体验向官方 `microsoft/playwright-mcp` 靠齐。

本轮完成标准：

1. 默认高层动作链路结果语义更统一，MCP、内核、daemon automation 三层更一致。
2. `diagnose_page` / `diagnose_target` 的结构化输出更通用，不再偏向少数测试站点。
3. 复杂动态页面下，优先返回高信号结构化读结果，进一步降低对整页文本的依赖。
4. 文档、skill 模板、系统 skill 与当前事实保持同步。
5. 针对本轮新增能力补齐测试，并完成针对性回归。

## 2. 重点差距

### 2.1 动作结果语义仍有零散差异

当前很多动作已经可用，但不同入口返回结构还不够收敛：

- 有的动作明确返回 `verified` / `matched`
- 有的动作只返回底层字段，不利于上层 agent 做稳定判断
- 某些读动作有高信号结构化字段，另一些没有

本轮目标是让高频读/验/诊断动作更接近统一契约。

### 2.2 结构化提取还不够泛化

目前已经有：

- `structured_page`
- `structured_region`
- `list_candidates`
- `describe_target`
- `watch_target_state`

但在复杂前端页面上，结构化结果还偏“摘要”，缺少更适合自动化推理的字段，例如：

- 当前页面主交互区域判断
- 主要动作候选
- 过滤/搜索/导航控件线索
- 列表/表格/弹层/菜单信号

### 2.3 复杂页面下仍有整页文本兜底倾向

目标不是消灭 fallback，而是让 fallback 本身也尽量返回结构化、可推理的数据，而不是纯文本堆积。

## 3. 实现范围

### 3.1 结果语义统一

重点收敛：

- `verify_text`
- `verify_dialog`
- `verify_element`
- `describe_target`
- `list_candidates`
- `diagnose_page`
- `diagnose_target`

统一补充或稳定以下字段：

- `verified`
- `matched`
- `target`
- `by`
- `target_summary`
- `structured_region`
- `managed_diagnostics`

### 3.2 结构化页面模型增强

增强 `structured_page`：

- `primary_actions`
- `search_controls`
- `filter_controls`
- `navigation_controls`
- `collection_signals`
- `table_signals`
- `role_counts`
- `interactive_labels_preview`

保持通用，不绑定任何具体网站。

### 3.3 结构化区域模型增强

增强 `structured_region`：

- `region_kind`
- `interactive_controls`
- `primary_actions`
- `search_like_controls`
- `status_controls`
- `role_counts`

目标是让复杂 target 周边的可操作上下文更接近官方 MCP 的“高信号局部视图”。

### 3.4 候选质量继续提升

在不引入站点 adapter 的前提下，继续优化：

- transient popup / listbox / menuitem 的命中优先级
- 搜索/筛选/提交类控件的语义打分
- overlay/dialog 场景下的局部上下文信号

## 4. 代码改造点

重点文件：

1. `chromium_advanced/browser_session_kernel.py`
2. `chromium_advanced/browser_session_kernel_diagnostics.py`
3. `chromium_advanced/browser_engines/patchright_engine.py`
4. `chromium_advanced/mcp_daemon.py`
5. `chromium_advanced/mcp_server.py`
6. `tests/test_browser_session_kernel.py`
7. `tests/test_daemon_automation_routes.py`
8. `README.md`
9. `README_zh.md`
10. `docs/skill_templates/browser-identity-mcp.SKILL.md`
11. `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`

## 5. 测试计划

### 5.1 单元测试

补充并通过以下方向：

1. `structured_page` 新字段
2. `structured_region` 新字段
3. `verify_*` 结果归一化
4. `diagnose_*` 输出包含增强结构字段
5. daemon automation 返回结构不退化

### 5.2 回归测试

至少覆盖：

1. `tests/test_action_pipeline.py`
2. `tests/test_browser_session_kernel.py`
3. `tests/test_daemon_automation_routes.py`
4. `tests/test_runtime_integration_local.py`

## 6. 文档与 skill 同步

完成后同步更新：

1. `README.md`
2. `README_zh.md`
3. `docs/skill_templates/browser-identity-mcp.SKILL.md`
4. `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
5. 系统 skill：
   - `$CODEX_HOME/skills/browser-identity-mcp/SKILL.md`
   - `$CODEX_HOME/skills/browser-identity-mcp-wsl/SKILL.md`

更新重点：

1. 默认主路径仍是 `patchright`
2. 三引擎角色分工
3. 复杂页面优先使用高层读/验/诊断动作
4. `watch_target_state`、`list_candidates`、`diagnose_target` 的推荐使用方式

## 7. 非目标

本轮不做：

1. 新增第四套浏览器引擎
2. 站点专用 adapter
3. GUI 大规模视觉重做
4. 彻底重写 daemon / worker 架构

## 8. 验收

满足以下条件才算本轮完成：

1. 新增测试全部通过
2. 原有关键测试不回退
3. 结构化诊断输出明显增强且保持通用
4. 文档与 skill 已同步
5. 本地代码达到可继续发布验证的状态

