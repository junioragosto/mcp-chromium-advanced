# 2026-06-18 空闲态 CPU 治理开发计划

## 1. 目标定义

本轮目标不是“尽量优化”，而是把当前 `ChromiumProfileManager + ChromiumMcpDaemon` 的空闲态资源占用，收敛到接近成熟系统后台服务的水平。

对标对象采用用户指定的 `mihomo` 级别标准：

- 后台常驻
- 长时间闲置
- 不做实际业务时接近静止
- 不因为 GUI 打开就持续打热核心进程

一句话目标：

- 把当前系统从“空闲仍持续忙碌”改造成“空闲接近静止”的后台服务。

## 2. 验收标准

### 2.1 核心 CPU 标准

以下测试必须基于 **本地真实打包安装态**，不是源码直跑态：

- GUI 已启动
- daemon 已启动
- 没有 MCP 会话
- 没有 keepalive 运行
- 没有镜像/备份任务运行
- 用户不操作 GUI

连续观测不少于 `60s`，必须满足：

1. `ChromiumMcpDaemon.exe`
   - 平均 CPU：`<= 0.5%`
   - 允许瞬时抖动，但不能持续高于 `1%`

2. `ChromiumProfileManager.exe`
   - 平均 CPU：`<= 0.5%`
   - 允许瞬时抖动，但不能持续高于 `1%`

3. 二者合计
   - 平均 CPU：`<= 1.0%`

只要有一项不满足，本轮不通过。

### 2.2 控制面接口标准

空闲态下，以下接口必须降到轻量级：

1. `GET /_control/ping`
   - 常态响应：`<= 10ms`

2. `GET /_control/status`
   - 常态响应：`<= 20ms`
   - 不能触发重型扫描、回收、事件聚合

3. `GET /_control/profiles?include_runtime_snapshot=false`
   - 常态响应：`<= 50ms`
   - 默认只能返回轻量摘要

4. `GET /_control/events`
   - 不允许成为高频空闲轮询的一部分
   - 仅按需或低频刷新

### 2.3 功能一致性标准

CPU 优化不能以状态失真为代价。以下行为必须保持正确：

1. GUI 状态
   - daemon running/stopped 正确
   - profile busy/idle 正确
   - keepalive 状态正确

2. 会话治理
   - 同一 profile 不允许错误复用
   - 多 profile 并发状态一致
   - 会话归还后状态及时回到 idle

3. 自恢复
   - daemon 异常后 watchdog 仍能恢复
   - 恢复判断不能依赖高频重型轮询

## 3. 问题判断

当前 idle CPU 偏高，不是单点 bug，而是结构性问题：

1. GUI 定时器太多，空闲时仍持续刷新
2. control 路由太重，查询状态会顺带做巡检
3. housekeeping 和状态读取耦合
4. daemon 缺少真正的轻量内存快照层
5. GUI 读取 profile/events/logs 时没有严格区分“轻量轮询”和“按需详情”

这意味着本轮不是调几个 timer 间隔，而是要改空闲路径架构。

## 4. 改造原则

### 4.1 轻重分离

把 control 面拆成两类：

- 轻量接口：供 GUI 空闲轮询，只读内存态
- 重量接口：用户主动查看详情时才调用

### 4.2 查询只读，不顺带打扫

状态查询不应默认触发：

- `reconcile_stale_profile_occupancy()`
- `reap_expired_profile_occupancy()`
- 外部 Chromium 全扫描
- mirror 校验
- recent events 聚合

### 4.3 后台治理异步化

巡检、回收、外部进程扫描等逻辑应改成：

- 独立低频任务
- 触发式任务
- 显式管理动作时执行

而不是绑定到每次 HTTP 查询。

### 4.4 GUI 只在需要时拉重数据

GUI 必须遵循：

- heartbeat 只做存活判断
- profiles 默认读轻量摘要
- occupancy/details/events 只在对应页面激活时刷新
- 日志和事件采用低频或手动刷新

## 5. 开发任务

### 任务 A：空闲调用链梳理

输出当前完整链路：

- GUI timers
- GUI -> control routes
- control routes -> session manager heavy paths
- heavy paths -> process scan / mirror / events / reconcile

产出物：

- 一张空闲态高频调用链地图

### 任务 B：daemon 轻量快照层

在 daemon 内建立轻量内存态：

- daemon 基础状态
- active sessions 摘要
- per-profile busy/idle 摘要
- keepalive 摘要
- 最近更新时间戳

要求：

- 普通状态读取不触发深度扫描
- snapshot 由事件增量更新

### 任务 C：control 路由分层

将以下路由改成默认轻量：

- `/_control/ping`
- `/_control/status`
- `/_control/profiles`

需要时再补充 heavy 参数或单独详情路由。

### 任务 D：housekeeping 解耦

把以下逻辑从读路径中移出：

- stale occupancy reconcile
- expired occupancy reap
- 外部进程治理

改为：

- 后台低频治理
- 显式触发治理
- 必要的节流与互斥

### 任务 E：GUI 空闲轮询收缩

重点治理：

- watchdog timer
- occupancy events timer
- profiles refresh timer
- log/events refresh timer
- startup refresh 链路

要求：

- 空闲态只保留最轻轮询
- 非当前可见 tab 不触发重详情刷新
- 失败重试也不能把 daemon 打热

### 任务 F：真实安装态性能验证

必须在真实安装目录替换后验证：

- 打包
- 替换安装态
- 启动 GUI + daemon
- 采样 idle CPU
- 验证 control latency
- 做一次真实 MCP 会话后再回 idle 复测

## 6. 测试计划

### 6.1 结构验证

验证点：

1. 轻量 control 路由不会触发重型扫描
2. GUI watchdog 仅走 ping
3. occupancy / events 只在可见页面刷新
4. housekeeping 被节流且不再绑到每次查询

### 6.2 性能验证

测试场景：

1. GUI 刚启动后的 idle 60s
2. GUI 最小化后的 idle 60s
3. 打开占用页但不操作 60s
4. 做一次真实 MCP 会话，归还后 idle 60s

采集指标：

- daemon CPU
- GUI CPU
- 合计 CPU
- `/_control/ping` 延迟
- `/_control/status` 延迟
- `/_control/profiles?include_runtime_snapshot=false` 延迟

### 6.3 回归验证

必须回归：

1. daemon 自动启动
2. GUI watchdog 恢复
3. busy-state 正确
4. profile 会话申请/归还正常
5. keepalive 基本状态不回退

## 7. 发布验收

发布验收按以下顺序进行：

1. 本地编译打包
2. 替换真实安装态
3. 启动 GUI + daemon
4. idle 60s CPU 验证
5. 打开 GUI 不操作再测 60s
6. 执行一次真实 MCP 会话后回 idle，再测 60s
7. 检查状态一致性是否回退

最终只输出两种结论：

- 通过：满足全部 CPU 和一致性标准
- 不通过：列出超标项和根因

## 8. 本轮边界

本轮不顺带处理以下主题，除非它们直接导致 idle CPU 异常：

- 引擎能力增强
- 站点适配
- GUI 视觉设计
- 新功能扩展
- 业务脚本能力增强

本轮只做一件事：

- 用工程化方式把 idle CPU 压到后台服务可接受标准。
