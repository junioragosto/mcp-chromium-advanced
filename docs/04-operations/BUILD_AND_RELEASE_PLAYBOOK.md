# Build And Release Playbook

本文固化当前仓库的编译、打包、发布、版本治理和验收流程。

后续线程如果要“本地打包”“生成 release 包”“准备 GitHub Actions 分发”，都应先读这份文档，而不是直接猜脚本行为。

正式边界规则：

- `dist/` 只表示 Windows 本地安装树。
- `dist/` 不是正式发布产物。
- 正式发布产物只认 `out/` 根目录下的压缩包与发布元数据文件。
- `out/_stage/`、`out/package/`、`dist_stage/`、`build_stage/` 都是中间目录，不允许上传到 GitHub Release。

## 1. 事实源

当前打包链路以以下文件为准：

- `pyproject.toml`
  项目正式版本号事实源，当前由 `project.version` 提供。
- `chromium_advanced/version.py`
  运行时版本回退源，`FALLBACK_APP_VERSION` 必须与 `pyproject.toml` 保持一致。
- `release-manifest.json`
  发布时受治理的 runtime / 外部资产版本与来源事实源。
- `scripts/validate_release_contract.py`
  发布契约校验入口。
- `build_chromium_manage_gui_exe.ps1`
  Windows 本地安装树构建入口。
- `scripts/build_release.py`
  当前平台 release 包构建入口。
- `.github/workflows/ci.yml`
  CI 校验入口。
- `.github/workflows/release-candidate.yml`
  版本变更后的候选发布入口。
- `.github/workflows/release-publish.yml`
  tag / 手工正式发布入口。

结论：

- 版本号不是写在 README 里手工维护的。
- 外部 Node 相关 runtime 版本不是靠“构建时拉最新”临时决定的。
- 后续线程修改打包行为时，必须同时检查这几处是否仍一致。

## 2. 当前打包目标

当前产物目标不是把所有内容继续塞进 3 个巨大的单文件可执行程序，而是收敛为：

- 根目录薄启动器
- 共享的应用 runtime 目录
- 共享的 Node runtime 目录
- 共享的 MCP runtime 目录
- 打包自带的 README、release 说明、skill 模板、AI 安装说明

Windows 当前本地安装树的核心结构：

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

这套布局的约束是：

- 根目录 3 个 `.exe` 里，只有 GUI 入口是用户主入口。
- `ChromiumMcpDaemon.exe` 和 `ChromiumMcpWorker.exe` 必须保持为薄启动器，转发到共享 GUI runtime。
- Node runtime 只保留一套共享副本，放在 `resources/runtime/node/`。
- `official_playwright_mcp` runtime 只保留一套共享副本，放在 `resources/runtime/official_playwright_mcp/`。
- 打包后的应用不能依赖系统已安装的 Node 或 Python 才能运行。
- 允许内部自带冻结 Python runtime / 受治理 Node runtime，但不允许回退到系统环境。

## 3. 三类构建入口

### 3.1 Windows 本地安装树

用途：

- 本机验证
- 替换本地安装目录
- 给后续真实 MCP 验证提供可运行产物

入口：

```powershell
.\build_chromium_manage_gui_exe.ps1
```

结果：

- 产出 `dist/`
- 是“安装树”，不是最终发布压缩包

### 3.2 当前平台 release 包

用途：

- 生成可分发压缩包
- 为 GitHub Actions 同构复用

入口：

```powershell
python scripts/build_release.py --artifact-name-base chromium-profile-manager-windows-x64
```

常用 `artifact-name-base`：

- `chromium-profile-manager-windows-x64`
- `chromium-profile-manager-macos-x64`
- `chromium-profile-manager-macos-arm64`
- `chromium-profile-manager-linux-x64`

结果：

- 中间 staging 目录在 `out/_stage/`
- 正式发布文件只产出到 `out/` 根目录
- 正式发布文件为 `out/<artifact-name>-<version>.zip` 或 `.tar.gz`
- 同时产出：
  - `out/release-metadata.json`
  - `out/update-manifest-stable.json`
  - `out/update-manifest-rc.json`
  - `out/sha256sums.txt`

### 3.3 GitHub Actions 跨平台构建

入口：

- 手工触发 `workflow_dispatch`
- 或 push `v*` tag

定义文件：

- `.github/workflows/ci.yml`
- `.github/workflows/release-candidate.yml`
- `.github/workflows/release-publish.yml`

当前矩阵：

- `windows-latest`
- `macos-13`
- `macos-14`
- `ubuntu-latest`

## 4. 本地打包标准流程

### 4.1 先校验契约

每次准备打包前，先执行：

```powershell
python scripts/validate_release_contract.py
```

这一步至少校验：

- `pyproject.toml` 的 `project.version`
- `chromium_advanced/version.py` 的 `FALLBACK_APP_VERSION`
- workflow 中固定的 `@playwright/cli` 版本
- `release-manifest.json` 中的 runtime / 资产字段完整性

如果这一步不过，不要继续打包。

### 4.2 Windows 本地安装树构建

```powershell
.\build_chromium_manage_gui_exe.ps1
```

脚本行为要点：

- 会停止当前仓库路径下的旧 `ChromiumProfileManager` / `ChromiumMcpDaemon` / `ChromiumMcpWorker`
- 会清理并重建：
  - `build_stage/`
  - `dist_stage/`
  - `build/`
  - `dist/`
  - `tmp/packaging_wrappers/`
- 会构建：
  - 一套 `onedir` GUI runtime
  - 三个根目录入口 exe，其中 daemon / worker 为 wrapper
- 会把以下内容复制到 `dist/`：
  - `resources/`
  - `docs/skill_templates/`
  - `README*.md`
  - `AI_INSTALLATION_RUNBOOK.md`
  - `RELEASE_README*.md`
  - `release-manifest.json`
  - `chromium_profiles.example.json`

### 4.3 当前平台 release 包构建

在本地需要生成可分发压缩包时，再执行：

```powershell
python scripts/build_release.py --artifact-name-base chromium-profile-manager-windows-x64
```

脚本行为要点：

- Windows 下内部会调用 `build_chromium_manage_gui_exe.ps1`
- 会把 `dist/` 复制进 `out/_stage/package/`
- 会复制发布文档、skill 模板、`release-manifest.json`
- 会生成 `release-info.txt`
- 会根据平台打包为 `.zip` 或 `.tar.gz`
- 正式对外交付时，只认 `out/` 根目录下的压缩包和发布元数据文件

## 5. 版本号管理规则

当前版本治理规则必须保持为：

1. 正式版本号从 `pyproject.toml` 读取。
2. `chromium_advanced/version.py` 的回退版本必须与之完全一致。
3. 打包产物文件名中的版本，由 `scripts/build_release.py` 调 `get_app_version()` 派生。
4. UI、`release-info.txt`、artifact 文件名必须反映同一版本。

版本升级操作顺序：

1. 修改 `pyproject.toml` 的 `project.version`
2. 同步修改 `chromium_advanced/version.py` 的 `FALLBACK_APP_VERSION`
3. 如 runtime / CLI 版本变化，同步修改 `release-manifest.json`
4. 如 `@playwright/cli` 版本变化，同步修改 `.github/workflows/ci.yml`、`.github/workflows/release-candidate.yml`、`.github/workflows/release-publish.yml`
5. 运行 `python scripts/validate_release_contract.py`
6. 重新打包并验收

不要只改其中一处。

## 6. 外部依赖与 Node runtime 治理

### 6.1 当前治理原则

- 构建不能依赖系统 Node 或系统 Python 才能运行产物。
- Node 相关服务尽量共用一套受治理 runtime。
- 后续无论是 `official_playwright_mcp`、`patchright`、`playwright_cli`，还是未来 Node GUI 组件，都优先复用这套 shared Node runtime 思路。

### 6.2 当前事实源

`release-manifest.json` 当前至少治理：

- `runtime.node`
- `runtime.official_playwright_mcp`
- `runtime.playwright_cli`
- `assets.fingerprint_extension`

当前 manifest 里已经固定了：

- `playwright_core_version`
- `@playwright/mcp` 包版本
- `@modelcontextprotocol/sdk` 对应版本信息
- `@playwright/cli` 版本
- fingerprint 插件来源模式

### 6.3 fingerprint 资产规则

`scripts/build_release.py` 当前支持从本地缓存读取 fingerprint 资产。

当前正式约束：

- 默认使用 `release-manifest.json` 里的 `assets.fingerprint_extension.source_mode`
- 现状应优先保持 `local-cache`
- 本地缓存查找路径为：
  - `extensions/fingerprint-extension.zip`
  - `local_extensions/fingerprint-extension.zip`

只有在明确调整发布契约时，才允许改成网络拉取模式。不要把“latest”重新引回正式 release 主路径。

## 7. GitHub Actions 固化流程

当前 workflow 的标准步骤是：

1. checkout
2. setup Python 3.10
3. setup Node 20
4. `python -m pip install -r requirements.txt pyinstaller build`
5. `npm install -g @playwright/cli@0.1.13`
6. `python -m build --wheel`
7. `python scripts/validate_release_contract.py`
8. `python scripts/build_release.py --artifact-name-base ...`
9. upload：
   - `out/*.zip`
   - `out/*.tar.gz`
   - `out/release-metadata.json`
   - `out/update-manifest-stable.json`
   - `out/update-manifest-rc.json`
   - `out/sha256sums.txt`

后续线程如果要改 workflow，必须保持以下原则：

- 继续先校验 release contract，再构建
- 继续固定 `@playwright/cli` 版本，不允许去掉 pin
- 继续只上传 `out/` 根目录下的正式发布文件
- 禁止上传 `out/_stage/`、`out/package/`、`dist/` 这类中间目录
- 继续沿用当前平台矩阵，除非有明确的平台策略变更

## 8. 打包后验收

### 8.1 结构验收

本地安装树验收至少确认以下内容存在：

- `dist/ChromiumProfileManager.exe`
- `dist/ChromiumMcpDaemon.exe`
- `dist/ChromiumMcpWorker.exe`
- `dist/app/bin/ChromiumProfileManager/`
- `dist/resources/runtime/node/`
- `dist/resources/runtime/official_playwright_mcp/`
- `dist/skill_templates/`
- `dist/README.md`
- `dist/README_zh.md`
- `dist/AI_INSTALLATION_RUNBOOK.md`
- `dist/RELEASE_README.md`
- `dist/RELEASE_README_zh.md`
- `dist/release-manifest.json`

正式发布文件验收至少确认以下内容存在：

- `out/*.zip` 或 `out/*.tar.gz`
- `out/release-metadata.json`
- `out/update-manifest-stable.json`
- `out/update-manifest-rc.json`
- `out/sha256sums.txt`

### 8.2 本地运行验收

至少完成一次真实运行：

1. 启动 `dist/ChromiumProfileManager.exe`
2. 确认 daemon 可起来
3. 确认 `http://127.0.0.1:28888/mcp` 可用
4. 用真实 `browserIdentity` MCP 跑一次最小 smoke test
5. 完成后关闭会话，再验证退出链路

真实 MCP smoke test 的最小闭环应包含：

1. `get_server_status`
2. `can_start_profile_session(profile_name=...)`
3. `start_profile_session(profile_name=...)`
4. `navigate(...)`
5. 页面实际证据校验
6. `close_profile_session(session_id)`

不要只做脚本级假验证。

### 8.3 替换安装目录验收

如果要替换本地已安装版本：

1. 显式关闭正在运行的旧实例
2. 用新的 `dist/` 覆盖安装目录
3. 从安装目录启动 `ChromiumProfileManager.exe`
4. 再做一次真实 MCP smoke test

## 9. 文档与产物约束

当前发布目录必须继续携带：

- `README.md`
- `README_zh.md`
- `AI_INSTALLATION_RUNBOOK.md`
- `RELEASE_README.md`
- `RELEASE_README_zh.md`
- `skill_templates/`

原因不是“好看”，而是为了保证：

- 用户拿到包就能看到安装说明
- AI / 代理线程拿到包就知道 skill 模板和接入方式
- 后续 workflow 分发产物具备自解释能力

## 10. 后续线程的最低操作准则

后续任何线程只要动到打包 / 版本 / workflow，最低要求都是：

1. 先读本文
2. 再读 `release-manifest.json`
3. 运行 `python scripts/validate_release_contract.py`
4. 本地打出 `dist/`
5. 本地打出 `out/` 下的正式压缩包
6. 做一次真实 MCP 验收
7. 如要改 CI，再同步更新 `.github/workflows/ci.yml`、`.github/workflows/release-candidate.yml`、`.github/workflows/release-publish.yml`

如果做不到这 7 步，不应宣称“打包已完成”。
