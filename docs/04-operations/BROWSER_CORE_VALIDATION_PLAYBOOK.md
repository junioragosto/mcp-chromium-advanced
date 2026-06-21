# Browser Core Validation Playbook

## 目的

本文件定义浏览器核心升级、结构重构和发布前收口时统一使用的验证口径。

目标不是“跑几个临时脚本看起来没坏”，而是让每轮变更都能落到固定的测试层、固定的真实场景和固定的发布前验收清单上。

## 当前测试分层

仓库内默认测试结构：

- `tmp/tests/unit/`
  pure helpers、view model、shared-core 纯逻辑
- `tmp/tests/integration/`
  `SessionManager`、daemon route、managed kernel 等跨模块集成
- `tmp/tests/contract/`
  engine / managed capability contract
- `tmp/tests/smoke/`
  快速 smoke
- `tmp/tests/slow/`
  本地重运行时验证，不进入默认回归
- `tmp/tests/manual/`
  发布前人工验证脚本、一次性验证脚本

默认规则：

- `pytest -q` 只跑 `unit + integration + contract + smoke`
- `slow` 和 `manual` 不得混入默认回归

## 默认回归口径

每次结构调整、核心能力升级或治理逻辑变更后，最少要完成：

1. 语法/导入检查
- 关键模块 `py_compile` 通过

2. 默认 pytest
- `pytest -q` 通过

3. 关键集成检查
- `tmp/tests/integration/test_session_manager.py`
- `tmp/tests/integration/test_browser_session_kernel.py`
- 任何本轮改动直接涉及的 contract/integration 用例

## 引擎定位验证

验证时按当前引擎定位理解结果：

- `official_playwright_mcp`
  默认高层路径，应作为主验证基线
- `patchright`
  兼容/回退引擎，重点看复杂交互与旧行为兼容
- `selenium_uc`
  重点看 stealth / challenge / gesture / XY 操作
- `playwright_cli`
  重点看轻量兼容、tab/page、低保真场景兜底

验证结果不应拿错引擎去判断系统能力上限。

## 大版本发布验证

大版本发布前至少覆盖以下项目：

1. 打包入口
- GUI 从安装目录正常启动
- daemon / worker 能从打包路径启动
- GUI 启动后不闪退

2. 默认引擎确认
- `get_server_status()` 返回的默认引擎与配置一致
- 当前预期默认值：`official_playwright_mcp`

3. 真实登录态场景
- 使用有登录态的真实 profile
- 至少覆盖一个复杂动态站点
- 至少覆盖一次结构化读取 + 一次高层动作 + 一次释放

4. 并行验证
- 至少两个不同 profile 并行运行
- 不得出现占用泄漏、状态错位、错误归属混乱

5. 无痕验证
- 验证 `runtime_options.incognito=true` 的隔离能力
- 不假设 session start 直接暴露所有 runtime option，而是按当前治理入口验证

6. challenge / gesture / stealth 验证
- 至少一条 `selenium_uc` 路径
- 包含以下之一：
  - challenge-heavy 页面
  - stealth-sensitive 页面
  - drag / gesture / XY interaction
- 对手势类场景优先验证高层能力：
  - `browser_detect_gesture_grid(...)`
  - `browser_unlock_gesture_pattern(...)`

7. 清理验证
- 所有 session 能正确归还
- daemon 状态回到空闲
- 没有残留 profile 占用

## 小版本 smoke 口径

小迭代至少完成：

1. GUI/daemon 可达
2. 一个 profile 可启动
3. 一个简单页面导航
4. 一个结构化读取
5. 一个高层动作
6. 一个正常释放

## 结构重构专项验证

如果本轮属于结构化重构，而不是新功能开发，额外关注：

1. 主入口文件是否真的变薄
- 不是只新增 helper 再把逻辑继续堆回入口

2. 测试是否和架构边界一致
- 新 helper 应该有对应 unit/integration 覆盖
- 默认 pytest 不应被重运行时脚本拖慢

3. 文档是否同步到新事实
- 架构文档写清新的层次和入口
- 验证文档写清新的测试目录和默认运行口径

## 复杂页面读取验证指导

对于 Gmail、YouTube Studio、GitHub 这类复杂页面：

- 优先看 `structured_page`
- 优先看 `browser_get_interaction_context(...)`
- 优先看 `browser_list_candidates(...)`
- 优先看 action trace / session health / resolution trace
- `run_script(...)` 只作为补充，不应是唯一真相来源

如果 `run_script(...)` 返回：

- `result=null`
  应视为运行时边界或页面渲染窗口，不应直接认定页面异常
- `script_result_state="stringified"`
  应视为序列化边界，而不是结构化读取成功

## 性能基线

每次核心升级至少记录：

- daemon idle CPU
- 复杂页面高层动作往返延迟
- 是否减少了 exploratory fallback
- 日志/诊断是否保持可读，没有爆量
- 高成功率场景是否走轻量路径而不是隐式触发重探测

## 发布记录要求

每次发布验证至少记录：

- commit 或 build id
- 验证日期
- 安装根目录
- 默认引擎
- 真实登录态场景结果
- 并行结果
- 无痕结果
- stealth / challenge / gesture 结果
- 清理结果
- 剩余风险

本地验证笔记和临时产物继续放在仓库外或 `tmp/` 下，不进入正式文档提交面。
