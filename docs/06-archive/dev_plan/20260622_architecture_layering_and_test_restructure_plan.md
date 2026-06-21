# 20260622 架构分层与测试体系重整计划

## 1. 背景

根据审计报告 `tmp/review/audit_full_20260622_035126.md`，当前项目虽然功能可用、测试规模已扩大，但核心架构仍存在明显的“厚入口文件”和“编排/实现混杂”问题。

当前最突出的结构问题是：

- `chromium_advanced/chromium_manage_gui.py` 仍有 `3216` 行
- `chromium_advanced/browser_session_kernel.py` 仍有 `2885` 行
- `chromium_advanced/chromium_profile_lib.py` 仍有 `2812` 行
- `chromium_advanced/session_manager.py` 仍有 `1644` 行
- `chromium_advanced/browser_engines/patchright_engine.py` 仍有 `2161` 行
- `chromium_advanced/browser_engines/playwright_cli_engine.py` 仍有 `1941` 行

问题已经不只是“文件很大”，而是这些文件同时承担了：

- 状态编排
- 规则判断
- 底层执行
- 结果整形
- 调试/诊断兼容

如果继续在这些入口文件里堆逻辑，后续每一轮功能增加都会再次把复杂度吸回主入口，导致：

- 代码边界越来越模糊
- 回归影响面越来越大
- 测试无法有效保护架构分层
- 后续拆分成本继续升高

本计划不是做一次机械式“拆文件”，而是用一轮完整收口，把核心分层边界、能力域拆分规则、测试分层和验收标准一起立住。

## 2. 本轮目标

本轮目标只有一个：

把当前系统从“功能可用但核心入口仍偏厚”的状态，推进到“分层清晰、主入口持续可控、测试对架构边界有保护作用”的状态。

本计划中的内容全部属于本轮范围，不再拆成“必须/应该/后置”。

完成后应达到：

1. GUI 主入口不再继续吸收控制逻辑和状态整形逻辑。
2. `SessionManager` 只负责 session/profile 治理，不继续吸收 runtime 回收、状态视图拼装、结果派生等可独立模块职责。
3. `browser_session_kernel` 只负责受管能力编排，不继续直接堆叠大段 fallback 实现细节。
4. engine 文件按能力域拆分，而不是继续把 lifecycle / tab / action / diagnose / trace 横向堆在一个文件里。
5. `chromium_profile_lib` 中的 shared core 能力与 GUI/daemon/engine 专属逻辑边界更清晰。
6. 测试目录与测试命名能明确表达 `unit / integration / contract / smoke` 分层。
7. 后续开发再写新计划时，依然按已有规则进入 `docs/06-archive/dev_plan/`，但当前行为说明必须回写 active docs。

## 3. 分层原则

### 3.1 GUI 只做 UI 入口，不做业务核心

`chromium_manage_gui.py` 的职责应收敛为：

- 组装 UI
- 绑定事件
- 调用 GUI 子模块
- 调用 control API / GUI state

不应继续增长的内容：

- profile/runtime 状态派生逻辑
- keepalive/worker/插件控制的核心规则
- 配置归一化和共享业务工具
- 复杂结果拼装和多源状态合并

### 3.2 SessionManager 只做治理，不做杂项拼装

`SessionManager` 应只承担：

- profile/session 生命周期治理
- 启动前校验与互斥规则
- session 注册、释放、复用判定
- engine 工厂调度
- runtime mode 判定

应外移的内容：

- active session 视图拼装
- occupancy -> session summary 派生
- runtime process cleanup 细节
- server/runtime status 聚合辅助逻辑

### 3.3 Kernel 只做受管能力编排，不做大段实现细节堆叠

`browser_session_kernel.py` 应只承担：

- 对 raw session 能力做统一编排
- 提供统一错误语义
- 选择 fallback 路径
- 统一结果归一化

应外移的内容：

- 大段 fallback 实现
- 通用 watch/wait/polling helper
- 结构化派生的纯算法逻辑
- 重复的 result assembly helper

### 3.4 Engine 代码按能力域拆，不按文件长度被动拆

engine 层应按如下能力域拆分：

- `lifecycle`
- `tabs_pages`
- `targets_actions`
- `inputs_gestures`
- `diagnostics_traces`
- `fallbacks_or_serialization`

禁止继续把这些全部横向追加到单个 `*_engine.py` 中。

### 3.5 Shared Core 只保留共享能力

`chromium_profile_lib.py` 应逐步收敛为共享核心：

- 配置归一化
- 路径解析
- profile/root 规则
- 纯工具函数
- shared state model

不应继续吸收：

- GUI 专属逻辑
- daemon 路由拼装逻辑
- engine 专属兼容分支

## 4. 本轮实施范围

### 4.1 GUI 入口瘦身

本轮至少完成：

1. 识别 `chromium_manage_gui.py` 中仍然属于可迁出的区域。
2. 把 GUI 侧的状态读取、控制调用、视图辅助函数继续下沉到 `chromium_advanced/gui/` 子模块。
3. 建立明确规则：
   - GUI 文件只保留 UI 绑定和页面组装
   - 所有新状态派生逻辑不得直接落回主 GUI 文件

目标不是一次把 GUI 拆空，而是把它变成稳定入口，而不是新的复杂度汇聚点。

### 4.2 SessionManager 分解

本轮要从 `session_manager.py` 中继续抽出独立模块，例如：

- runtime session view helper
- runtime cleanup helper
- occupancy/session projection helper
- server status assembly helper

要求：

- 提取后 `SessionManager` 的主类更偏“规则编排”
- 可纯函数化/可无状态化的部分不再塞回类里
- 外部行为保持不变

### 4.3 Kernel 分解

本轮要把 `browser_session_kernel.py` 中以下类型逻辑拆走：

- `watch_*` / `wait_*` 的 fallback 实现
- 轮询和稳定性检测 helper
- 纯结果整形 helper

建议目标结构：

- `browser_session_kernel.py`
  只做入口编排
- `browser_session_kernel_watchers.py`
  watch / wait fallback
- `browser_session_kernel_result_helpers.py`
  共用结果整形
- `browser_session_kernel_fallbacks.py`
  通用 fallback 实现

文件名可按实际代码组织微调，但边界必须成立。

### 4.4 Engine 分域拆分

本轮重点先处理最厚的两个 engine：

- `patchright_engine.py`
- `playwright_cli_engine.py`

至少要完成第一层分域：

- lifecycle/session
- tabs/page reads
- target/action
- diagnostics/trace

要求：

- 原始 `*_engine.py` 保留统一类定义和高层路由
- 具体能力落到同目录子模块
- 后续新能力必须进入对应能力域文件，而不是继续回堆主 engine 文件

### 4.5 Shared Core 收边界

本轮要检查 `chromium_profile_lib.py` 并抽出可独立模块，例如：

- runtime metadata helper
- process matching helper
- path normalization/model helper
- keepalive site status derivation helper

要求：

- 共享核心模块命名明确
- GUI/daemon/engine 不再把自己的专属逻辑继续塞入 shared core

### 4.6 测试体系重整

当前 `tmp/tests` 已可运行，但分层表达仍不够清晰。

本轮要建立明确的测试分层约定：

- `tmp/tests/unit/`
  纯函数和 shared core helper
- `tmp/tests/integration/`
  `SessionManager` / daemon route / kernel integration
- `tmp/tests/contract/`
  engine capability contract、managed result contract
- `tmp/tests/smoke/`
  启动、打包、端到端轻量验证
- `tmp/tests/manual/`
  `verify_*`、`validate_*` 这类人工/发布前脚本

至少要完成：

1. 目录分层落地
2. `pytest.ini` 与目录结构一致
3. 常用测试运行口径清晰
4. 文档中明确哪些测试属于提交前必跑、发布前必跑

## 5. 实施策略

### 5.1 小步提取，但按整体目标收口

虽然代码会分多次提交，但本轮完成定义不是“抽出几个 helper”，而是：

- 主入口真的变薄
- 新边界真的可执行
- 测试真的按新边界组织

### 5.2 先提取无行为变化模块

优先顺序：

1. 纯 helper / 纯 projection
2. fallback 实现
3. engine 能力域模块
4. GUI 组装与控制辅助
5. 测试目录重组

避免一开始就大范围改路由或改外部接口。

### 5.3 保持外部契约稳定

本轮默认不改：

- MCP tool 名称
- daemon 外部路由协议
- GUI 对用户暴露的操作模型
- engine 选择接口

重点是内部结构，不是对外重新发明接口。

## 6. 交付标准

本轮只有同时满足以下条件，才算完成：

1. 审计指出的四类核心问题都有结构化对应结果，而不是只写计划。
2. `chromium_manage_gui.py`、`browser_session_kernel.py`、`chromium_profile_lib.py`、`session_manager.py`、`patchright_engine.py`、`playwright_cli_engine.py` 至少有一部分真实职责被迁出。
3. 迁出后的模块职责命名清晰，边界可解释。
4. `tmp/tests` 形成清晰分层，不再把正式测试和手工验证脚本混在同一层。
5. 提交前测试口径清楚，并完成实际运行验证。
6. active docs 能说明新的测试口径和架构边界。
7. 最终本地提交时，工作树干净。

## 7. 测试计划

### 7.1 单元测试

覆盖：

- 新抽出的 pure helpers
- session/runtime projection helper
- process cleanup helper 中可测试的纯逻辑
- kernel watcher/fallback helper

### 7.2 集成测试

覆盖：

- `SessionManager` 状态聚合与 runtime status
- daemon status/control 相关行为
- kernel `watch_*` / `wait_*` 行为

### 7.3 contract 测试

覆盖：

- `patchright` / `playwright_cli` / `official_playwright_mcp` 的 managed capability contract
- 结果字段稳定性
- fallback 语义不回退

### 7.4 smoke 测试

覆盖：

- pytest 快速集
- daemon / GUI 基本启动
- 打包脚本至少通过一次静态或最小运行验证

### 7.5 手工验证脚本整理

把现有 `verify_*`、`validate_*` 归类到 `tmp/tests/manual/` 或同类目录，不再和主 pytest 测试平铺混放。

## 8. 验收与提交规则

执行过程中按以下节奏收口：

1. 先写计划
2. 按计划完成结构调整
3. 自己跑对应测试
4. 修完测试暴露的问题
5. 更新 active docs
6. 本地提交

最终汇报应围绕：

- 哪些核心模块职责被迁出
- 新的边界是什么
- 测试如何对应到架构层次
- 是否达到计划目标

而不是只罗列“改了几个文件”。

## 9. 非目标

本轮不做：

1. 重新设计用户交互
2. 重写对外 MCP 协议
3. 新增站点专用适配器
4. 引入新的浏览器引擎
5. 单纯为了压缩行数而做机械拆文件

本轮只做一件事：把核心结构真正理顺，让后续继续开发不再反复回到同样的结构问题。
