# 编译打包与分发体系成熟化规划

## 1. 目标

本规划定义 `mcp-chromium-advanced` 的正式编译、打包、分发、版本治理与发布契约。

本规划生效后，相关开发、打包、发布、文档、验收、GitHub Actions 配置必须以本文件为实施基线。

本规划覆盖以下目标：

- 本地安装树构建结构扁平、清晰、可替换
- GUI、daemon、worker 共享同一套安装态 runtime 载荷
- Node 相关 runtime 只保留一套受治理共享副本
- 打包、发布、版本、外部依赖治理存在单一事实源
- Windows、macOS、Linux 三个平台遵守同一逻辑 release contract
- GitHub Actions 输出正式受治理的发布产物，而不是临时工程构建物

## 2. 事实源

打包发布体系的事实源定义如下：

- 版本事实源：`pyproject.toml`
- 运行时版本回退事实源：`chromium_advanced/version.py`
- 外部 runtime / 资产事实源：`release-manifest.json`
- 发布契约校验入口：`scripts/validate_release_contract.py`
- Windows 本地安装树构建入口：`build_chromium_manage_gui_exe.ps1`
- 当前平台 release 包构建入口：`scripts/build_release.py`
- CI 校验入口：`.github/workflows/ci.yml`
- 候选发布入口：`.github/workflows/release-candidate.yml`
- 正式发布入口：`.github/workflows/release-publish.yml`
- 主动生效的打包操作文档：`docs/04-operations/BUILD_AND_RELEASE_PLAYBOOK.md`

任何线程修改打包、版本、release、workflow、runtime 治理逻辑时，必须同时检查以上文件。

## 3. 当前问题定义

当前体系存在以下正式问题：

1. GUI、daemon、worker 曾长期以多套冻结载荷并列存在，产物结构不清晰。
2. Node runtime 与 MCP runtime 容易在多个入口之间重复打包。
3. 打包入口、本地构建入口、CI 构建入口之间存在表达分散问题。
4. 版本号、artifact 命名、release metadata、tag 之间缺少统一校验。
5. 正式构建阶段存在“构建时拉最新外部依赖”的风险路径。
6. GitHub Actions 曾只做到 artifact 上传，未形成正式发布契约。

本规划的实施目标就是消除以上问题。

## 4. 正式产品布局契约

### 4.1 Windows 安装树契约

Windows 安装树采用以下逻辑布局：

```text
dist/
  ChromiumProfileManager.exe
  ChromiumMcpDaemon.exe
  ChromiumMcpWorker.exe
  README.md
  README_zh.md
  AI_INSTALLATION_RUNBOOK.md
  RELEASE_README.md
  RELEASE_README_zh.md
  release-manifest.json
  chromium_profiles.example.json
  app/
    bin/
      ChromiumProfileManager/
        ChromiumProfileManager.exe
        _internal/...
  resources/
    runtime/
      node/
      official_playwright_mcp/
  skill_templates/
```

该布局的正式约束如下：

1. 根目录 `ChromiumProfileManager.exe` 是用户主入口。
2. 根目录 `ChromiumMcpDaemon.exe` 与 `ChromiumMcpWorker.exe` 是薄启动器。
3. daemon / worker 不得再各自携带完整冻结 runtime。
4. `resources/runtime/node/` 只允许存在一份共享 Node runtime。
5. `resources/runtime/official_playwright_mcp/` 只允许存在一份共享 MCP runtime。
6. 产物中必须携带 README、安装说明、release 说明、skill 模板、`release-manifest.json`。

### 4.2 通用安装态布局契约

三个平台统一遵守以下逻辑产品布局：

```text
<install_root>/
  ChromiumProfileManager[.exe]
  ChromiumMcpDaemon[.exe]
  ChromiumMcpWorker[.exe]
  app/
    runtime/
      python/
      node/
      node_apps/
        official_playwright_mcp/
        patchright/
        playwright_cli/
    package/
      chromium_advanced/
    resources/
    skill_templates/
    release-info.txt
    chromium_profiles.example.json
```

平台差异只允许体现在外层包装，不允许体现在核心 runtime 重复携带上。

### 4.3 平台发布形态契约

- Windows 发布形态：install-root zip 包
- macOS 发布形态：`.app` + zip 包
- Linux 发布形态：共享 app 目录 + `tar.gz`

三个平台的 release contract 保持统一：

- 统一版本来源
- 统一 artifact 命名逻辑
- 统一 release metadata 结构
- 统一 update metadata 结构
- 统一产物结构校验断言

## 5. 入口与共享 runtime 契约

### 5.1 入口必须薄

平台入口程序只负责拉起共享安装态 runtime，不承担重复携带完整依赖树的职责。

正式定义如下：

- `ChromiumProfileManager.exe`：GUI 主入口
- `ChromiumMcpDaemon.exe`：转发到 GUI runtime 的 daemon 模式
- `ChromiumMcpWorker.exe`：转发到 GUI runtime 的 worker 模式

### 5.2 共享 runtime 范围

以下资产属于共享 runtime 范围：

- Python 冻结载荷
- 项目 Python 包
- `resources/`
- Node runtime
- `official_playwright_mcp` runtime
- `patchright` 所需 Node 侧 runtime 资产
- `playwright_cli` 所需 Node / CLI 运行资产

### 5.3 Node runtime 平台层

Node runtime 定义为项目级共享平台层，不归属于单一后端。其治理覆盖：

- `official_playwright_mcp`
- `patchright`
- `playwright_cli`
- 任何进入安装态产品布局的 Node 侧新组件

所有进入该平台层的 Node 资产，都必须进入同一套 manifest 与发布治理模型。

## 6. 版本治理契约

### 6.1 单一版本来源

正式版本号来源定义如下：

1. `pyproject.toml` 的 `project.version` 是正式版本事实源。
2. `chromium_advanced/version.py` 的 `FALLBACK_APP_VERSION` 必须与之完全一致。
3. 打包 artifact 文件名、运行时版本显示、`release-info.txt`、release metadata 必须派生自同一版本解析结果。

### 6.2 版本通道

版本通道正式定义为：

- `stable`
- `rc`
- `beta`
- `dev`

### 6.3 artifact 命名契约

发布包命名采用以下格式：

- `chromium-profile-manager-<version>-windows-x64.zip`
- `chromium-profile-manager-<version>-macos-x64.zip`
- `chromium-profile-manager-<version>-macos-arm64.zip`
- `chromium-profile-manager-<version>-linux-x64.tar.gz`

本地构建允许附带本地标识，但正式 release asset 不允许省略版本号。

### 6.4 tag 一致性契约

正式发布时必须满足：

- git tag 与应用版本一致
- artifact 文件名与应用版本一致
- release metadata 与应用版本一致

不满足时构建直接失败。

## 7. 外部依赖治理契约

### 7.1 正式原则

正式构建必须遵守以下原则：

1. 不解析无边界“latest”依赖
2. 不使用宽范围版本作为正式发布依赖
3. 不允许同一版本因上游变化 silently 改包
4. 每个外部依赖都必须保留来源、版本、校验信息

### 7.2 事实源

`release-manifest.json` 是以下内容的正式事实源：

- 共享 Node runtime 来源信息
- `@playwright/mcp` 版本
- `@modelcontextprotocol/sdk` 版本
- `@playwright/cli` 版本
- fingerprint 插件来源与模式

### 7.3 构建与依赖刷新分离

体系正式拆分为两个流程：

1. 依赖刷新流程
- 显式更新外部依赖版本
- 更新 manifest / 校验信息
- 记录变更原因

2. 正式构建流程
- 只消费仓库中已声明好的依赖版本
- 不主动追最新
- 校验失败直接中止

### 7.4 fingerprint 资产契约

fingerprint 资产使用 `release-manifest.json` 驱动。

正式构建中：

- `source_mode=local-cache` 时，只消费本地缓存资产
- `source_mode=network` 时，只消费 manifest 指定 URL
- 不存在“构建时去 GitHub 自动找最新资产”的正式路径

## 8. 本地构建契约

### 8.1 Windows 本地安装树

本地 Windows 安装树入口：

```powershell
.\build_chromium_manage_gui_exe.ps1
```

结果定义：

- 产出 `dist/`
- `dist/` 用于本机验证与安装目录替换
- `dist/` 不是最终发布压缩包

### 8.2 当前平台 release 包

当前平台 release 包入口：

```powershell
python scripts/build_release.py --artifact-name-base <artifact-name-base>
```

结果定义：

- 中间 staging 目录在 `out/_stage/`
- 正式发布文件只产出到 `out/` 根目录
- 产出平台压缩包
- 生成 `release-info.txt`
- 携带正式 release 文档与 manifest

### 8.3 构建前强制校验

每次正式打包前必须执行：

```powershell
python scripts/validate_release_contract.py
```

校验不过时，禁止继续打包。

## 9. CI 与分发契约

### 9.1 CI 目标

GitHub Actions 的正式目标定义如下：

- 统一平台矩阵构建
- 统一 artifact 结构校验
- 统一 checksums 输出
- 统一 release metadata 输出
- 统一 GitHub Release 资产发布

### 9.2 CI 必须校验的断言

每个平台构建必须校验：

1. GUI 入口存在
2. daemon 入口存在
3. worker 入口存在
4. 共享 runtime 只出现一份
5. `resources/runtime/node/` 只出现一份
6. `release-manifest.json` 存在
7. `release-info.txt` 存在
8. 顶层 README / runbook / release 文档 / skill 模板存在
9. archive 命名符合契约
10. 版本、tag、release metadata 一致
11. 外部依赖版本与 manifest 一致

### 9.3 CI 输出物

CI 正式输出物定义如下：

- 平台发布包
- `sha256sums.txt`
- `release-metadata.json`
- `update-manifest-stable.json`
- `update-manifest-rc.json`

## 10. 文档契约

### 10.1 主动生效文档

以下文档属于当前主动生效文档：

- `docs/04-operations/BUILD_AND_RELEASE_PLAYBOOK.md`
- `docs/05-reference/RELEASE_README.md`
- `docs/05-reference/RELEASE_README_zh.md`
- `docs/01-getting-started/AI_INSTALLATION_RUNBOOK.md`

### 10.2 文档同步规则

打包行为、产物布局、版本规则、发布规则发生变化时，必须同步更新主动生效文档。

归档计划文档用于实施基线与历史记录，不替代主动生效文档。

## 11. 验收契约

### 11.1 结构验收

至少确认以下内容存在：

- `ChromiumProfileManager`
- `ChromiumMcpDaemon`
- `ChromiumMcpWorker`
- 共享 runtime 目录
- `release-manifest.json`
- 文档与 skill 模板

### 11.2 本地运行验收

必须完成：

1. 启动新构建 GUI
2. 启动 daemon
3. 验证 `http://127.0.0.1:28888/mcp`
4. 完成真实 `browserIdentity` MCP smoke test
5. 关闭 session
6. 验证退出语义

### 11.3 替换安装目录验收

Windows 替换安装目录时必须完成：

1. 关闭旧实例
2. 覆盖安装目录
3. 从安装目录启动新版本
4. 再次完成真实 MCP smoke test

## 12. 实施阶段

### Phase 1：安装树共享 runtime 收敛

交付物：

- GUI、daemon、worker 共用同一套安装态 runtime
- 根目录仅保留薄入口
- Node runtime 与 MCP runtime 只保留单份共享副本

### Phase 2：版本与 metadata 契约落地

交付物：

- 版本来源统一
- artifact 命名统一
- `release-info.txt`
- `release-metadata.json`
- `sha256sums.txt`

### Phase 3：跨平台形态对齐

交付物：

- Windows / macOS / Linux 遵守同一逻辑产品布局
- 平台差异仅体现在包装形态

### Phase 4：CI 与 GitHub Release 成熟化

交付物：

- 统一 workflow 产物结构校验
- 统一 metadata 输出
- 统一 GitHub Release 资产发布

### Phase 5：更新消费准备完成

交付物：

- update manifest 进入正式发布产物
- 客户端更新链路拥有稳定消费基础

## 13. 不纳入本规划交付的事项

本规划不包含以下交付项：

- bundling Chromium / Chrome 浏览器本体
- bundling ChromeDriver
- 直接跳过治理契约进入全平台自动静默更新
- 在共享 runtime 契约未落地前引入平台特定复杂安装器差异

## 14. 完成定义

当以下条件同时满足时，本规划定义的打包分发体系视为完成：

- 三个进程入口共享同一套安装态 runtime
- bundled Node/runtime 不在多个入口间重复打包
- Windows 本地替换发布稳定
- macOS / Linux 产物形态清晰、可复现、已文档化
- release 文档与当前产品事实一致
- 版本、tag、artifact 命名、release metadata 自动一致
- 外部依赖不会因上游变化隐式改变正式发布包
- GitHub Actions 输出正式受治理发布产物
