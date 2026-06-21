# 官方 MCP 对齐迭代三期计划

日期：2026-06-18

## 1. 目标

本轮目标不是继续扩展引擎种类，而是把当前系统默认 `patchright` 主路径的体验进一步向官方 `microsoft/playwright-mcp` 靠齐，并补足上层 agent 最容易感知到的差距。

本轮完成后，应达到以下结果：

1. 调用方不需要先知道本项目的自定义工具命名，能通过一组更接近官方风格的别名进入主能力。
2. 高频真实任务里，不再因为缺少 `dialog`、`file upload` 这类主能力而退回大量 `run_script`。
3. 默认 `patchright` 主路径在复杂页面下更接近“先试高层动作，尽量一次命中”的体验。
4. 文档、skill、系统 skill 能明确告诉调用方：
   - 默认用什么
   - 什么时候切引擎
   - 什么时候用高层动作而不是 `run_script`

## 2. 与官方 `playwright-mcp` 的当前差距

### 2.1 工具面差距

当前系统虽然功能已经很多，但上层暴露仍然更偏项目内部命名，而不是官方风格。差距主要在：

1. 官方生态更强调一组稳定、高层、直觉化的浏览器工具入口。
2. 当前 MCP 虽然具备：
   - `browser_snapshot`
   - `browser_list_candidates`
   - `browser_describe_target`
   - `browser_diagnose_page`
   - `browser_diagnose_target`
   - `wait_for_*`
   - `watch_*`
   
   但对外缺少一层“官方风格兼容面”，会让 agent 侧提示词、tool routing、跨项目迁移成本更高。
3. 某些官方常见工作流相关能力还没有一等工具化暴露，例如：
   - dialog 处理
   - file upload
   - 更官方风格的 screenshot / tabs / close 风格别名

### 2.2 交互语义差距

虽然本项目已有很强的状态治理和多引擎能力，但官方 MCP 更强的地方在于：

1. 上层更少感知底层差异。
2. 高层动作默认就是第一选择，而不是调用方自己拼接行为。
3. 某些动作是“完成一个交互意图”，而不仅仅是调一个底层 API。

本项目目前还存在：

- 一部分动作需要调用方了解 `snapshot ref`、候选枚举、target 诊断链路
- 某些能力虽然能做，但不是一等动作，需要绕一层

### 2.3 主路径丝滑度差距

当前最明显的差距仍然集中在默认主路径：

1. `patchright` 的高层结构化能力已经比之前强很多，但还可以更主动。
2. 复杂页面下候选排序和目标命中率在继续提升，但与官方“默认就比较顺手”的体验还有差距。
3. 当前系统仍然比官方更容易让 agent 退回：
   - `run_script`
   - 全页文本读
   - 多次探测

### 2.4 验证基线差距

当前已经有：

- 单元测试
- daemon route 测试
- runtime integration local 测试

但还缺少一套更明确围绕“官方 MCP 对齐”的验证基线，例如：

- 官方风格别名是否正确映射
- dialog / upload 这些主能力是否能直接走高层工具
- 默认引擎路径是否优先命中这些动作

## 3. 本轮策略

### 3.1 不推翻现有工具面，增加官方风格兼容层

做法：

1. 保留现有工具，避免破坏已有业务。
2. 在 `mcp_server.py` 增加一组官方风格别名工具。
3. 这些别名工具底层仍然走现有 `ManagedBrowserSession` 契约，不引入平行实现。

这样做的价值：

- 老调用不坏
- 新 agent 更容易上手
- 更接近官方 MCP 的提示词和使用习惯

### 3.2 补齐高价值能力缺口

优先补：

1. `handle_dialog`
2. `file_upload`
3. `close` / `tabs` / `take_screenshot` 风格兼容入口

暂不补低价值或高破坏面工具。

### 3.3 继续强化默认 `patchright` 主路径

重点不是再写更多 fallback，而是：

1. 让默认路径优先命中更强的高层语义动作
2. 让 `patchright` 更像真正的一等主路径
3. `selenium_uc` 保持 stealth / challenge / gesture 专项定位
4. `playwright_cli` 保持轻量兼容定位

## 4. 本轮实施项

### 4.1 官方风格工具兼容层

在 `chromium_advanced/mcp_server.py` 新增兼容工具，建议包括：

1. `browser_tabs(session_id)`
   - 映射到 `browser_list_tabs`
2. `browser_take_screenshot(session_id, filename="", tab_id="")`
   - 映射到 `screenshot`
3. `browser_close(session_id)`
   - 关闭整个 session
4. `browser_handle_dialog(session_id, accept=True, prompt_text="")`
   - 处理 alert / confirm / prompt
5. `browser_file_upload(session_id, target, files, by="css")`
   - 处理 input[type=file]

这些工具要保持：

- 官方风格名称
- 结果语义统一
- trace / diagnostics 仍然保留

### 4.2 BrowserSession 契约补充

在 `base.py`、`browser_session_kernel.py` 和各引擎里补：

1. `handle_dialog(...)`
2. `file_upload(...)`

引擎要求：

1. `patchright`
   - 一等实现
2. `selenium_uc`
   - 一等实现
3. `playwright_cli`
   - 能可靠实现则实现
   - 不能可靠实现则显式降级，不伪装

### 4.3 结果语义继续统一

新增/补强动作要统一结果字段：

- `handled`
- `accepted`
- `dismissed`
- `uploaded`
- `file_count`
- `target`
- `by`

### 4.4 测试补齐

新增测试：

1. `tests/test_mcp_server_alias_tools.py`
   - 官方风格别名工具是否映射正确
2. `tests/test_browser_session_dialog_upload.py`
   - dialog / upload 结果语义
3. 扩充现有 daemon route / kernel 测试

### 4.5 文档与 skill

明确补充：

1. 当前支持官方风格兼容工具
2. 默认优先 `patchright`
3. 何时使用：
   - 高层动作
   - 诊断动作
   - `run_script`
4. 何时切：
   - `selenium_uc`
   - `playwright_cli`

## 5. 测试与验证计划

### 5.1 单元测试

至少通过：

1. `tests/test_action_pipeline.py`
2. `tests/test_browser_session_kernel.py`
3. `tests/test_daemon_automation_routes.py`
4. `tests/test_patchright_engine.py`
5. `tests/test_mcp_server_alias_tools.py`
6. `tests/test_browser_session_dialog_upload.py`

### 5.2 本地集成测试

通过：

1. `tests/test_runtime_integration_local.py`

必要时提高该测试超时，不再误把慢启动当失败。

### 5.3 验收验证

验收关注点：

1. 官方风格别名工具可直接调用
2. dialog / upload 不依赖脚本绕路
3. 默认 `patchright` 路径仍稳定
4. 文档与 skill 已同步

## 6. 非目标

本轮不做：

1. 新增第四引擎
2. 站点专用适配器
3. GUI 大改
4. 完全复刻官方项目结构

## 7. 交付标准

达到以下条件才算本轮完成：

1. 差距清单中的高优先级项已落实到代码
2. 单元测试与本地集成测试通过
3. 文档、skill、系统 skill 已同步
4. 本地代码达到下一步发布验证的状态

## 8. 当前完成情况补记

截至当前节点，已经完成：

1. 官方风格兼容工具：
   - `browser_tabs`
   - `browser_take_screenshot`
   - `browser_close`
   - `browser_handle_dialog`
   - `browser_file_upload`
   - `browser_resize`
   - `browser_network_request`
2. `browser_tabs` 已从简单 list 别名升级为统一动作入口：
   - `action="list"`
   - `action="new"`
   - `action="select"`
   - `action="close"`
3. 受管契约已补：
   - `handle_dialog(...)`
   - `file_upload(...)`
   - `resize(...)`
4. 引擎实现状态：
   - `patchright`：dialog / upload / resize 已实现
   - `selenium_uc`：dialog / upload / resize 已实现
   - `playwright_cli`：dialog / upload / resize 已实现，但后续仍要继续做真实复杂页语义验证
5. 当前测试基线已覆盖：
   - alias tool 行为
   - dialog/upload pipeline/kernel/daemon 路由
   - resize pipeline 与 alias tool 行为
   - single network request detail 行为

## 9. 当前仍存在的高优先级差距

与官方 `playwright-mcp` 相比，当前这一轮之后仍值得继续推进：

1. screenshot 结果语义仍更偏本项目现状，后续还可以继续向官方 target/filename 习惯靠拢。
2. `playwright_cli` 虽已补上 dialog/upload/resize，但在复杂动态页上的真实稳定性仍不能与 `patchright` 画等号。
3. 更高阶的“默认先命中高层动作”体验，还应继续围绕 `patchright` 主路径深化，而不是继续堆更多弱别名。
4. network detail 当前先基于现有受管 request 列表实现兼容读取，后续仍可继续向更完整的 headers/body 细节深化。
