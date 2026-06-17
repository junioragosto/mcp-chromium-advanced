# 官方 Playwright MCP 能力对齐优化计划

日期：2026-06-17

## 1. 目标

本轮迭代目标不是继续扩展“有多少引擎”，而是把当前系统在默认 `patchright` 路径下的实际体验，尽可能向官方 `playwright-mcp` 靠齐。

重点目标：

1. 提升复杂站点下的首发命中率，减少 fallback 探测。
2. 提升高阶动作的一致性和语义稳定性，减少“动作成功但行为不够像官方”的情况。
3. 强化复杂动态页面的结构化提取能力，而不是过度依赖页面全文文本。
4. 优化默认执行链路的性能细节，降低多层封装带来的额外抖动。
5. 保持当前项目已有优势不丢失：
   - 真实身份/真实登录态
   - profile 治理与并发控制
   - 多引擎可切换
   - GUI / daemon / worker 管理能力

## 2. 当前与官方 MCP 的主要差距

### 2.1 动作语义差距

当前系统已经有 `navigate`、`click`、`type`、`wait_for_text`、`hover`、`navigate_back`、`navigate_forward` 等动作，但动作语义仍存在不完全收敛的问题。

表现：

- 某些历史栈边界下，`navigate_back` 可能回到 `about:blank`
- 某些页面动作成功依赖后置补救，而不是一次命中
- 不同引擎间相同行为的结果一致性仍不够强

### 2.2 复杂页面命中率差距

在 Gmail、YouTube Studio、复杂 React/Polymer/shadow-heavy 页面上，当前系统仍比官方更容易走到：

- 先探测
- 再 snapshot/ref
- 再 fallback
- 最后才完成动作

而官方更接近“直接定位并执行”。

### 2.3 结构化提取能力差距

当前系统虽然已经有：

- snapshot/ref
- resolution trace
- page diagnostics
- anti-bot signals
- post-action context

但对复杂动态 DOM 的稳定建模还不够，导致：

- `run_script` 在复杂站点上不总是稳定
- 结构化读取时常要退化为页面文本
- 页面对象模型和动作模型还没有完全形成统一闭环

### 2.4 性能与丝滑度差距

当前短板不在“能不能打开浏览器”，而在“拿到浏览器后的交互成本”：

- 部分动作前置探测偏多
- 一些上下文采集默认偏重
- 同一类问题有时会重复做诊断
- 动作执行后上下文回填还可以更轻量、更智能

## 3. 总体策略

本轮不引入新的外部引擎栈，也不推翻现有三引擎体系，而是做“能力分层 + 默认路径强化”：

1. `patchright` 作为默认主路径，优先对齐官方 `playwright-mcp` 的动作层体验。
2. `selenium_uc` 保持为第二优先级，主打 stealth / challenge / 手势 / 坐标类增强场景。
3. `playwright_cli` 保持为第三路径，作为轻量兼容和低开销集成能力，不再承担默认高能力职责。

## 4. 实现范围

### 4.1 动作层收敛

目标：让主流浏览动作先通过高层动作完成，而不是依赖 `run_script`。

实施项：

1. 继续扩展高层动作集合，优先覆盖官方常见动作语义：
   - 更稳定的 wait / hover / select / drag
   - 更一致的 tab / history / dialog / upload / download 相关能力
2. 给高层动作增加统一动作结果结构：
   - 是否直接命中
   - 是否进入 fallback
   - 失败分类
   - 恢复建议
3. 修正历史导航等边界语义，使其更接近官方预期。

### 4.2 结构化页面模型强化

目标：减少“只能读全文文本”的情况。

实施项：

1. 强化 snapshot 构建策略：
   - 减少无价值节点
   - 提高 interactive / semantic 节点保留率
   - 优先保留表单、菜单、列表、按钮、链接、输入区
2. 强化复杂前端页面 DOM 抽取：
   - shadow root 递归读取
   - custom element 内容抽样
   - aria / role / data-* / accessible name 优先
3. 引入更稳定的结构化候选发现逻辑：
   - list candidates
   - describe target
   - inspect elements
   - diagnose target
4. 把结构化抽取与动作定位统一到同一套 resolution 流程。

### 4.3 默认执行链路性能优化

目标：减少不必要的探测与重诊断。

实施项：

1. 区分轻量动作与重诊断动作，避免默认每次都拉全量上下文。
2. 为常见动作增加分级上下文采集策略：
   - fast
   - balanced
   - deep
3. 优化 post-action context 触发规则：
   - 成功轻动作只采最小必要上下文
   - 失败或复杂动态页面再升高诊断等级
4. 缓存短周期可复用信息：
   - 最近 snapshot
   - 最近 tabs
   - 最近 active element
   - 最近 resolution trace

### 4.4 官方 MCP 设计对齐

目标：不是复制官方代码，而是吸收其成熟设计。

对齐方向：

1. 高层动作优先，而不是鼓励调用方大量写 JS。
2. 页面模型、动作模型、诊断模型统一。
3. 更明确的 capability surface。
4. 更一致的错误分类与恢复提示。
5. 更少暴露底层引擎差异给上层调用方。

### 4.5 三引擎角色固化

目标：让调用方知道何时该切哪一个，而不是盲用。

引擎定位：

1. `patchright`
   - 默认引擎
   - 主流 MCP 任务优先使用
   - 复杂前端、结构化提取、高阶动作优先
2. `selenium_uc`
   - stealth 优先
   - challenge / Cloudflare / slider / gesture / 坐标输入优先
3. `playwright_cli`
   - 轻量兼容
   - 低开销诊断
   - 不作为默认高能力路径

## 5. 代码改造点

重点模块：

1. `chromium_advanced/browser_session_kernel.py`
2. `chromium_advanced/browser_session_kernel_diagnostics.py`
3. `chromium_advanced/action_pipeline.py`
4. `chromium_advanced/browser_engines/patchright_engine.py`
5. `chromium_advanced/browser_engines/selenium_uc_engine.py`
6. `chromium_advanced/browser_engines/playwright_cli_engine.py`
7. `chromium_advanced/mcp_server.py`
8. `chromium_advanced/session_manager.py`
9. GUI / README / skill 模板 / 系统 skill

## 6. 测试计划

### 6.1 单元与接口测试

1. 扩展 action pipeline tests
2. 扩展 browser session kernel tests
3. 扩展 engine strategy tests
4. 扩展 diagnostics / fallback / capability tests

### 6.2 安装态真实验证

默认以 `patchright` 为主验证：

1. GitHub 登录态与多步操作
2. Gmail 前 3 封邮件标题读取
3. YouTube Studio 评论汇总
4. 复杂 tab / history / hover / select / drag 动作
5. 一轮无痕模式验证

### 6.3 多引擎对比验证

同一类任务对比：

1. `patchright`
2. `selenium_uc`
3. `playwright_cli`

验证项：

- 成功率
- 首发命中率
- fallback 次数
- 平均动作时延
- 错误可解释性

## 7. 文档与 Skill 更新要求

本轮完成后必须同步更新：

1. `README.md`
2. `README_zh.md`
3. `docs/skill_templates/browser-identity-mcp.SKILL.md`
4. `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
5. 系统 skill：
   - `C:\\Users\\Administrator\\.codex\\skills\\browser-identity-mcp\\SKILL.md`
   - `C:\\Users\\Administrator\\.codex\\skills\\browser-identity-mcp-wsl\\SKILL.md`

更新重点：

1. 默认引擎已经是 `patchright`
2. 三引擎定位和切换方式
3. 推荐的使用策略
4. 新的高层动作和能力边界
5. 与官方 `playwright-mcp` 的差距和收敛方向

## 8. 验收标准

达到以下条件，才视为本轮完成：

1. 默认 `patchright` 路径在安装态通过真实验证
2. 常见高层动作不再主要依赖临时 JS 探路
3. 复杂页面结构化提取成功率明显提升
4. 调试与诊断默认链路更轻量，动作体感更接近官方 MCP
5. 三引擎定位、切换方式、skill、README、系统 skill 全部同步完成
6. 编译、替换、重启、真实验证全部通过

## 9. 不在本轮范围

以下内容本轮不作为主目标：

1. 引入全新外部引擎栈替代现有三引擎体系
2. 做站点专用 adapter
3. 推翻现有 GUI / daemon / worker 架构
4. 做视觉层 UI 大改版

本轮目标很明确：在现有架构打通的基础上，把默认 `patchright` 路径的能力和使用体验，尽可能向官方 `playwright-mcp` 靠齐。
