# 官方 Playwright MCP 浏览器操作内核完整对齐计划

日期：2026-06-19

## 1. 版本目标

本次版本只有一个目标：

`把当前项目在“浏览器实际操作能力”上的核心短板一次性补齐，向官方 playwright-mcp 的成熟体验完整对齐。`

这里的“完整对齐”指的是浏览器操作内核层，不包含以下非主线范围：

- GUI 视觉改版
- keepalive 站点扩展
- profile 管理新功能
- 新增浏览器引擎种类
- 站点专用 adapter

本版本只聚焦浏览器操作内核本身，包括：

- 页面结构化理解
- 目标发现与排序
- 高层动作语义
- 复杂动态页面默认命中率
- 调试信息信噪比
- 动作执行效率与 round-trip 成本

本计划中的所有内容都属于本版本必须全部实现的范围，不存在一期、二期、后置项。

## 2. 当前差距

当前项目与官方 playwright-mcp 的差距，主要不在“能不能操作”，而在“复杂页面下是否天然精准、天然稳定、天然省探测”。

当前真实短板如下：

### 2.1 目标发现与排序不够强

当前在复杂页面上仍容易出现：

- 候选过多
- 候选排序不够语义化
- 菜单、弹层、组合框、对话框中的真实目标优先级不够高
- 刚发生交互后，下一步仍在全页面泛扫，而不是优先看局部上下文

### 2.2 结构化提取还不够像“页面语义模型”

当前虽然已有：

- `structured_page`
- `interaction_hints`
- richer `post_action_context`

但在复杂动态前端下仍然偏向：

- DOM + 文本 fallback
- 信息噪音偏多
- 缺少稳定的交互区域模型
- 缺少更强的可访问性语义抽象

### 2.3 高层动作还不够意图驱动

当前很多动作已经可用，但与官方相比仍存在：

- 动作前缺少更强的 target resolve
- 动作后上下文回传不够天然可继续推理
- `click / select / wait / verify` 在复杂控件上的策略不够统一
- popup/dialog/overlay/listbox 等场景缺少明确模式化执行策略

### 2.4 复杂控件的通用处理能力不足

薄弱区包括：

- custom combobox
- menu / menuitem / popup
- overlay / dialog
- shadow DOM / custom element
- React / Polymer 动态更新后的 stale target 恢复

### 2.5 调试输出和效率仍可提升

当前仍存在：

- 输出有时过宽、过散，不够聚焦
- 一次任务需要多次小动作探测
- 缺少更好的批量读取/局部抽取策略
- 复杂页面下 round-trip 成本偏高

## 3. 实现原则

### 3.1 不重复造轮子

本版本以官方 playwright-mcp 源码为核心参照对象，直接研究并吸收其成熟设计：

- 目标发现模型
- 候选排序策略
- 高层动作 contract
- 结构化页面表达
- 调试输出组织方式
- 回归验证方法

原则不是照抄代码，而是：

- 抽取可复用设计
- 识别必须适配重写的部分
- 在当前三引擎架构中落地统一能力层

### 3.2 统一能力语义层

虽然三个引擎已经拆开实现，但本版本必须把以下统一能力层彻底补齐：

- unified target model
- unified structured page model
- unified structured region model
- unified action contract
- unified diagnostics contract
- unified post-action context contract

### 3.3 三引擎统一承载，不再各自松散实现

本版本完成后，三引擎必须基于同一套高质量能力语义工作：

- `patchright`
  - 作为默认主力引擎
  - 吃满最完整的复杂页面能力

- `selenium_uc`
  - 保持反检测优势
  - 同步统一动作语义与关键复杂控件策略

- `playwright_cli`
  - 保持轻量路径
  - 同步统一动作 contract、统一结果语义和基础复杂页面能力

## 4. 本版本完整实现范围

### 4.1 官方源码结构化对照落地

必须完成一轮系统性的官方源码对照，并形成项目内开发依据，至少明确：

- 官方哪些模块负责页面结构化理解
- 官方哪些模块负责目标发现与排序
- 官方哪些模块负责高层动作语义
- 官方哪些模块负责复杂控件策略
- 官方哪些模块负责 diagnostics 与调试上下文
- 哪些设计可直接借鉴
- 哪些实现必须适配重写

交付物：

- 项目内对照开发说明文档
- 对照结论映射到本项目模块责任

### 4.2 Unified Target Model

新增或重构统一目标模型，至少包含：

- `selector`
- `role`
- `accessible_name`
- `text_preview`
- `control_type`
- `placeholder`
- `title`
- `value_state`
- `selected/checked/expanded/disabled`
- `dialog/popup/overlay` 归属
- `visibility/interactability`
- `shadow/custom-element` 线索
- `recent_interaction_affinity`
- `viewport/geometry` 基础信息
- `ranking_reason`

交付要求：

- 上层动作不再主要依赖“字符串 target + by”
- 候选解析结果可缓存、可复用、可解释
- target resolve 结果可在动作链中直接传递

### 4.3 候选发现与排序内核升级

必须重构 `list_candidates` 与相关 fallback candidate 生成逻辑，至少做到：

- 结合 DOM、可访问性语义、可见性、交互性和当前上下文做统一候选生成
- 对以下目标加高权重：
  - 当前弹层内控件
  - 当前对话框内控件
  - 菜单项 / 列表项 / 组合框选项
  - 最近一次交互区域附近目标
  - 主动作按钮 / 提交按钮 / 主要 CTA
- 对以下目标降权：
  - 不可见节点
  - 装饰性文本
  - 重复无交互节点
  - 大面积容器节点
  - 对当前上下文无关节点

交付要求：

- 返回结果带排序解释
- 返回结果带命中原因摘要
- 动作后支持局部重排，不再总是全页面泛扫

### 4.4 Structured Page / Structured Region 升级

必须把当前结构化提取升级为真正可执行的页面语义摘要系统，覆盖：

- 页面级摘要
- 区域级摘要
- 当前交互热点区域摘要
- dialog / popup / menu / listbox / toolbar / form 的局部语义摘要

必须具备：

- 大页面不只返回整页摘要
- 优先返回与当前动作最相关的局部结构
- 动作后自动刷新最相关局部区域语义
- 对复杂前端优先走更强语义提取，再 fallback 到 DOM/text

### 4.5 高层动作策略化

本版本必须统一并升级以下动作：

- `click`
- `type_text`
- `press_key`
- `select_option`
- `wait_for`
- `wait_for_text`
- `wait_for_text_gone`
- `verify_target_visible`
- `verify_target_value`
- `verify_text`
- `open_tab`
- `activate_tab`
- `run_script_batch`

交付要求：

- 动作前统一 resolve target
- 动作过程中统一做 interactability 判定
- 动作后统一回传 post-action context
- 失败时回传结构化失败原因，而不只是异常文本
- 结果语义稳定，可直接继续推理

### 4.6 复杂控件通用策略

本版本必须完整补齐以下通用控件策略：

- popup / overlay 模式
- dialog 模式
- combobox / listbox 模式
- menu / menuitem 模式
- toolbar / menu-button 模式
- shadow DOM 基础穿透与定位恢复
- stale target 恢复
- 动态内容刷新后的热点区域更新

原则：

- 只做通用控件策略
- 不做站点 adapter

### 4.7 批量读取与降 round-trip

本版本必须补齐以下能力：

- 强化 `run_script_batch`
- 常见复合读取改为批量执行
- 优先返回局部结构化数据，而不是多次零碎读取
- 对复杂场景建立少轮次读取策略

目标：

- 减少复杂场景下的探测步数
- 降低“为了确认页面状态而做多个来回请求”的成本

### 4.8 Patchright / Selenium UC / Playwright CLI 三引擎统一对齐

本版本必须完成三引擎的统一能力落地：

- `patchright` 吃满本轮完整能力
- `selenium_uc` 同步统一动作语义、失败语义、关键复杂控件策略
- `playwright_cli` 同步统一动作 contract、统一结果语义和基础复杂页面策略

本版本完成后，不允许出现“只有 patchright 看起来像一套成熟内核，其它引擎只是勉强能跑”的状态。

### 4.9 Diagnostics 与调试输出重构

本版本必须重构 diagnostics 输出，做到：

- 大幅减少低价值 HTML 噪音
- 增加“下一步建议”字段
- 增加“当前热点区域”字段
- 错误信息按可恢复 / 不可恢复分类
- 候选与结构化上下文天然可用于下一步动作推理

## 5. 开发实现组织

### 5.1 内核模型层

负责：

- unified target model
- structured page / region model
- diagnostics contract
- post-action context contract

### 5.2 目标解析与排序层

负责：

- candidate discovery
- semantic ranking
- context-aware re-ranking
- popup/dialog scoped resolution

### 5.3 高层动作策略层

负责：

- click / select / wait / verify / type 的统一策略
- complex control mode handling
- stale target recovery

### 5.4 引擎适配层

负责：

- `patchright`
- `selenium_uc`
- `playwright_cli`

分别把统一能力层落地到各自引擎实现中。

## 6. 测试计划

### 6.1 单元测试

必须新增或补齐以下方向测试：

- target ranking
- structured page generation
- structured region generation
- post-action context contract
- complex control mode switching
- action-level error normalization
- multi-engine contract consistency

### 6.2 集成测试

在 `tmp/tests` 增加或更新本地集成验证：

- patchright complex interaction smoke
- popup/dialog/menu flow
- listbox/combobox flow
- shadow DOM interaction flow
- run_script_batch efficiency flow
- multi-engine same-contract flow

### 6.3 对照指标

每个基准场景都必须记录：

- 首次命中率
- 平均动作步数
- fallback 次数
- 结构化结果可用率
- 平均动作 latency

## 7. 发布验证计划

安装态发布验证必须包含以下全部场景：

### 7.1 复杂真实场景

- `Profile 1` GitHub 登录态页面操作
- `Profile 1` Gmail 前几封标题读取
- `Profile 1` YouTube Studio 评论汇总

### 7.2 并行验证

- 两个不同 profile 同时执行复杂动作
- 验证动作成功率、状态一致性和清理回收

### 7.3 无痕验证

- `Profile 1` incognito 模式执行基础导航和结构化读取

### 7.4 清理验证

- 所有会话释放后：
  - `active_browser_session_count = 0`
  - `busy_profiles = []`
  - 无孤儿会话

### 7.5 三引擎验证

- `patchright`
- `selenium_uc`
- `playwright_cli`

三条路径都必须跑完至少一轮合同一致性验证，确保统一能力语义不是只在一个引擎上成立。

## 8. 文档与 Skill 更新

本版本必须同步更新：

- `README.md`
- `README_zh.md`
- `docs/skill_templates/browser-identity-mcp.SKILL.md`
- `docs/skill_templates/browser-identity-mcp-wsl.SKILL.md`
- 系统内已安装 skill

必须明确写清：

- 默认主力引擎是 `patchright`
- 三引擎各自适用场景
- 复杂任务优先使用 `patchright`
- `selenium_uc` 作为反检测增强路径
- `playwright_cli` 作为轻量集成路径

## 9. 验收标准

本版本验收不是“又补了几个动作”，而是以下结果必须全部实现：

- 复杂页面默认命中率明显提升
- fallback 探测次数明显下降
- structured page / region 对复杂前端显著更可用
- `click/select/wait/verify` 结果语义稳定一致
- 三引擎都能承载同一套高质量能力语义
- 默认 `patchright` 路径体感明显接近官方
- 安装态发布验证全部通过

量化验收要求：

- 复杂基准场景平均动作步数下降
- fallback 次数下降
- 关键复杂场景成功率稳定提升
- 复杂场景回传的结构化结果可直接继续推理，不再主要依赖大段 page text

## 10. 最终目标

本版本完成后，项目必须从：

`功能已经可用，但复杂页面体感仍弱于官方`

推进到：

`在默认 patchright 路径下，复杂页面操作、结构化提取、动作语义、复杂控件处理和调试体验都显著接近官方 playwright-mcp；同时 selenium_uc 与 playwright_cli 也具备统一高质量 contract，而不是能力割裂的辅助实现。`
