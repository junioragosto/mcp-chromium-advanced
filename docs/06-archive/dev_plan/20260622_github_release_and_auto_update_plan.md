# GitHub Release 与自动更新体系规划

## 1. 目标

本规划定义 `mcp-chromium-advanced` 的 GitHub 发布、更新元数据、应用内更新检查、通知与自动升级体系。

本规划生效后，GitHub workflow、版本通道、release 资产、更新检查代码、GUI 更新交互、Windows 自动升级执行器，统一以本文件为实施基线。

本规划覆盖：

- 版本变更触发的跨平台构建
- GitHub Release 资产发布
- release metadata 与 update manifest 输出
- 应用内检查更新
- 更新通知
- Windows 自动下载、校验、替换、重启升级

## 2. 两条链路的正式边界

系统正式拆分为两条链路：

### 2.1 发布链路

- 仓库版本变更
- CI 构建
- 平台发布包生成
- checksums / release metadata / update manifest 生成
- GitHub Release 发布

### 2.2 客户端更新链路

- 获取远端 update manifest
- 版本与通道判断
- GUI / 托盘通知
- 下载更新包
- 校验
- 退出旧版本
- 替换安装目录
- 重启新版本

发布链路与客户端更新链路分离实施、分离校验、分离文档、分离产物。

## 3. 触发模型

### 3.1 Workflow A：CI 校验

触发：

- PR
- 普通 push

职责：

- 发布契约校验
- 基础构建
- 基础测试
- 产物结构校验

禁止事项：

- GitHub Release 发布

### 3.2 Workflow B：版本变更候选发布

触发：

- 默认分支 push
- 且版本号有变化

职责：

- 构建 Windows / macOS / Linux 平台包
- 生成 checksums
- 生成 `release-metadata.json`
- 生成 `update-manifest-stable.json`
- 生成 `update-manifest-rc.json`
- 创建 GitHub draft release 或 prerelease

### 3.3 Workflow C：正式发布

触发：

- `workflow_dispatch`
- 或 push `v*` tag

职责：

- 校验 tag 与版本一致
- 发布正式 GitHub Release
- 将 stable 通道指向该版本

### 3.4 正式触发结论

正式 stable release 不以“任意版本变化提交自动对外发布”为准，而以：

- 版本变化触发候选发布
- tag 或手工发布动作触发 stable release

作为正式规则。

## 4. 版本与通道模型

### 4.1 版本号规则

版本号采用 SemVer：

- `MAJOR.MINOR.PATCH`
- `MAJOR.MINOR.PATCH-rc.N`
- `MAJOR.MINOR.PATCH-beta.N`
- `MAJOR.MINOR.PATCH-dev.N`

### 4.2 通道规则

正式通道定义如下：

- `stable`
- `rc`
- `beta`
- `dev`

### 4.3 通道消费规则

- `stable` 客户端只消费正式 release
- `rc` 客户端消费 rc 与 stable
- `beta` 客户端消费 beta、rc、stable
- `dev` 客户端消费 dev、beta、rc、stable

客户端判断更新时必须同时检查：

- 版本号
- 通道
- prerelease 状态
- 是否允许降级

## 5. 版本变更检测契约

版本变更检测以以下文件为准：

- `pyproject.toml`
- `chromium_advanced/version.py`

正式规则：

1. `pyproject.toml` 的 `project.version` 相比上一提交变化时，视为版本变化。
2. `chromium_advanced/version.py` 的 `FALLBACK_APP_VERSION` 必须同步变化。
3. 两者不一致时 workflow 直接失败。
4. 同版本 tag / release 已存在时，正式发布流程直接失败。

## 6. GitHub Release 资产契约

正式 release assets 定义如下：

- `chromium-profile-manager-<version>-windows-x64.zip`
- `chromium-profile-manager-<version>-macos-x64.zip`
- `chromium-profile-manager-<version>-macos-arm64.zip`
- `chromium-profile-manager-<version>-linux-x64.tar.gz`
- `sha256sums.txt`
- `release-metadata.json`
- `update-manifest-stable.json`
- `update-manifest-rc.json`

release asset 缺失任一必选项时，正式发布失败。

## 7. 发布元数据契约

### 7.1 `release-metadata.json`

该文件用于发布治理与审计，必须包含：

- `version`
- `channel`
- `git_tag`
- `git_commit`
- `published_at`
- `release_notes_url`
- `release_manifest_version`
- `runtime`
- `assets`
- `checksums`

### 7.2 `update-manifest-<channel>.json`

该文件用于客户端更新判断，必须包含：

- `channel`
- `version`
- `published_at`
- `notes_url`
- `mandatory`
- `min_supported_version`
- `rollout_percentage`
- `assets`

`assets` 必须包含：

- `platform`
- `arch`
- `file_name`
- `download_url`
- `sha256`
- `size`

### 7.3 元数据边界

- `release-metadata.json` 用于发布侧
- `update-manifest-*.json` 用于客户端更新侧
- `release-manifest.json` 用于构建依赖与运行时治理

三者职责固定，不允许混用。

## 8. 客户端更新配置契约

客户端配置新增 `update` 根配置块：

```json
"update": {
  "enabled": true,
  "channel": "stable",
  "check_on_startup": true,
  "check_interval_hours": 24,
  "auto_download": false,
  "auto_install": false,
  "last_checked_at": "",
  "last_notified_version": "",
  "skipped_version": "",
  "feed_url": ""
}
```

该配置块与 `app`、`mcp`、`control` 并列，不归属于任何现有配置子块。

## 9. 客户端检查更新契约

### 9.1 检查时机

客户端更新检查规则定义如下：

1. 启动后延迟执行第一次检查
2. 按 `check_interval_hours` 定时检查
3. 提供手动“检查更新”入口

### 9.2 更新判断规则

客户端更新服务执行以下判断：

1. 请求当前通道的 update manifest
2. 解析远端版本
3. 比较本地版本与远端版本
4. 校验通道是否允许升级
5. 判断是否被 `skipped_version` 屏蔽
6. 决定是否通知

### 9.3 失败处理

更新检查失败时必须满足：

- 不阻塞 GUI 启动
- 不影响 MCP、keepalive、profile 管理主功能
- 记录日志
- 保持静默降级

## 10. GUI 与托盘交互契约

### 10.1 GUI 设置项

GUI 必须提供：

- 自动检查更新
- 更新通道选择
- 启动时检查
- 手动检查更新
- 是否自动下载

### 10.2 GUI 展示项

GUI 必须展示：

- 当前版本
- 可用更新版本
- 发布时间
- 更新说明链接

### 10.3 通知行为

通知体系必须支持：

- GUI 内 banner 或状态提示
- 系统托盘通知
- 跳过此版本
- 稍后提醒
- 查看更新说明

同一版本通知去重由以下字段控制：

- `last_notified_version`
- `skipped_version`

## 11. 网络请求与缓存契约

客户端更新检查必须具备：

- 请求超时
- 重试上限
- 静默失败处理
- 可扩展的缓存能力

缓存字段允许使用：

- ETag
- Last-Modified

但缓存策略不得改变版本判断结果。

## 12. 自动下载与自动安装契约

### 12.1 平台范围

自动安装范围定义如下：

- Windows：纳入自动安装范围
- macOS：纳入更新检查与通知范围
- Linux：纳入更新检查与通知范围

### 12.2 Windows 自动升级执行器

Windows 自动升级必须使用独立 updater helper，不允许 GUI 主进程在运行时直接覆盖自身。

updater helper 职责定义如下：

1. 下载更新包
2. 校验 sha256
3. 请求旧实例退出
4. 等待 GUI / daemon / worker 全部退出
5. 备份当前安装目录
6. 解压到 staging 目录
7. 替换 install root
8. 失败时回滚
9. 重启新版本

### 12.3 安装升级状态机

Windows 自动升级状态机定义如下：

1. `check_available`
2. `download_started`
3. `download_verified`
4. `shutdown_requested`
5. `processes_stopped`
6. `backup_created`
7. `staging_ready`
8. `install_swapped`
9. `restart_started`
10. `completed`

任一阶段失败时进入：

- `rollback_started`
- `rollback_completed`
- `failed`

## 13. 安全与校验契约

自动更新链路必须满足：

1. 仅接受受信任仓库来源
2. 下载 URL 必须与 update manifest 一致
3. sha256 必须校验通过
4. 版本号必须高于当前版本，除非显式允许降级
5. 通道必须匹配
6. 下载失败或校验失败不得污染当前安装

## 14. 实施阶段

### Phase 1：发布侧打通

交付物：

- 版本变更检测
- 候选发布 workflow
- checksums
- `release-metadata.json`
- `update-manifest-*.json`
- GitHub draft release / prerelease

### Phase 2：客户端检查与通知

交付物：

- `update` 配置块
- 更新检查服务
- GUI 手动检查更新
- GUI / 托盘通知
- 跳过版本逻辑

### Phase 3：Windows 自动下载与升级

交付物：

- 下载到 staging
- sha256 校验
- updater helper
- 退出 / 替换 / 重启闭环
- 失败回滚

### Phase 4：跨平台发布增强

交付物：

- stable / rc / beta / dev 通道成熟化
- macOS 签名与 notarization 纳入发布体系
- Linux 分发策略纳入发布体系

## 15. 不纳入本规划首轮交付的事项

本规划首轮交付不包含：

- 全平台自动静默更新
- 无校验覆盖安装
- 直接解析 GitHub HTML 做版本判断
- 将 release metadata、update metadata、runtime manifest 混成单文件
- 未区分通道就让所有客户端消费 prerelease

## 16. 完成定义

当以下条件同时满足时，本规划定义的 GitHub 发布与自动更新体系视为第一阶段完成：

1. 版本变更可自动触发跨平台候选构建
2. 可自动生成 GitHub draft release 或 prerelease
3. release assets 包含 checksums、release metadata、update manifest
4. 客户端可按 channel 检查远端新版本
5. 客户端可进行 GUI / 托盘更新通知
6. 更新检查失败不影响当前应用正常运行

当以下条件同时满足时，本规划定义的自动升级体系视为完成：

1. Windows 可自动下载更新包
2. Windows 可完成 sha256 校验
3. Windows 可完成退出、替换、重启闭环
4. Windows 失败时可回滚
5. stable / rc / beta / dev 通道在客户端与发布侧保持一致
