# Chromium Profile Manager 发布说明

## 安装包内置内容

- 桌面 GUI 程序
- Windows 根启动器 `ChromiumProfileManager.exe`
- MCP daemon 与 browser worker 运行时
- 内置 `resources/bookmarks_template.html`
- 构建时自动从 `omegaee/my-fingerprint` 最新 release 下载并打包的指纹插件 zip
- Skill 模板：
  - `skill_templates/browser-identity-mcp.SKILL.md`
  - `skill_templates/browser-identity-mcp-wsl.SKILL.md`
- 示例配置：
  - `chromium_profiles.example.json`
- 发布说明：
  - `release_readme.md`
  - `release_zh.md`

## 安装包不包含的内容

- Chromium / Chrome 浏览器二进制
- ChromeDriver 二进制
- 真实用户 Profile 数据

安装后仍需在 GUI 中配置你自己的浏览器路径、ChromeDriver 路径和 UserData 根目录。

## 首次启动

1. 启动 GUI。
   Windows 下应直接运行安装根目录里的 `ChromiumProfileManager.exe`。
2. 打开路径/配置区域。
3. 配置：
   - Chromium 浏览器路径
   - ChromeDriver 路径
   - split UserData profiles root
4. 保存配置。

## Windows 启动器与退出语义

- Windows 安装目录的用户入口是 `ChromiumProfileManager.exe`。
- 这个根 exe 是启动器，会再解析并启动安装包内部的真实 GUI 运行时。
- 开机自启应指向根启动器，并带 `--start-minimized`。
- `ChromiumProfileManager.exe --exit-existing-instance` 会通知正在运行的 GUI 实例退出。
- 当前版本已验证显式退出会一起回收：
  - GUI 进程
  - daemon 进程
  - `28888` 监听端口

## MCP 接入

默认 daemon MCP 地址通常为：

- `http://127.0.0.1:28888/mcp`

如果在 GUI / 配置中设置了 `mcp.api_token`，所有 MCP 请求都必须带：

- `Authorization: Bearer <token>`

## Token 配置说明

- `mcp.api_token`
  用于普通 MCP / 浏览器业务调用
- `control.api_token`
  用于 GUI / control 接口，例如 dashboard、日志、keepalive 状态、插件管理和 worker 控制

如果 `control.api_token` 为空，则 `/_control/*` 接口保持禁用。

## 浏览器与 Driver 要求

- 使用用户机器上的 Chromium 或 Chrome
- 使用与浏览器版本匹配的 ChromeDriver
- 浏览器主版本号应尽量与 driver 主版本号一致

## 新环境中的 Profile 创建

新建 profile 现在不依赖 mirror 快照。

创建流程是：

1. 创建独立的 split UserData 根目录，例如 `UserDataProfile1`
2. 在其中创建 Chromium profile 目录，例如 `Profile 1`
3. 用内置 `resources/bookmarks_template.html` 初始化书签
4. 其余 Chromium 状态由浏览器首次启动时自动生成

因此在新机器上，只要已经配置好：

- split UserData 根目录
- Chromium 路径
- ChromeDriver 路径

应用就可以正常创建新的 profile，而不需要依赖既有 mirror 数据。

## 引擎说明

- `patchright`
  默认高能力 MCP 引擎
- `selenium_uc`
  更适合反自动化识别、challenge、拖动、手势等场景
- `playwright_cli`
  轻量集成兼容引擎，不再是默认高能力路径

## Skill 安装

安装包已自带 skill 模板。把需要的模板复制到 Codex 或项目自己的 skill 目录即可：

- `skill_templates/browser-identity-mcp.SKILL.md`
- `skill_templates/browser-identity-mcp-wsl.SKILL.md`

如果手动为 Codex 配置 MCP，通常还需要在 `~/.codex/config.toml` 或 Windows 对应的 Codex 配置目录里加入：

```toml
[mcp_servers.browserIdentity]
url = "http://127.0.0.1:28888/mcp"

[mcp_servers.browserIdentity.http_headers]
Authorization = "Bearer <token>"
```

## 备注

- 安装包已内置书签模板、skill 模板、发布说明和最新指纹插件资源。
- 浏览器和 driver 仍然是机器相关资产，当前版本不会强行内置到发布包中。
