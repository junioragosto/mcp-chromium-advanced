# Chromium Profile Manager 发布说明

## 安装包内置内容

- 桌面 GUI 程序
- MCP daemon 与 browser worker 运行时
- 内置 `resources/bookmarks_template.html`
- 构建时自动从 `omegaee/my-fingerprint` 的 latest release 下载并内置的指纹插件 zip
- Skill 模板：
  - `skill_templates/browser-identity-mcp.SKILL.md`
  - `skill_templates/browser-identity-mcp-wsl.SKILL.md`
- 示例配置：
  - `chromium_profiles.example.json`
- 发布说明：
  - `release_readme.md`
  - `release_zh.md`

## 安装包不包含的内容

- Chromium / Chrome 浏览器本体
- ChromeDriver
- 真实用户登录态与用户数据

也就是说，用户安装后仍然需要在 GUI 中配置自己的浏览器和 driver 路径。

## 首次使用

1. 启动 GUI。
2. 进入路径/配置区域。
3. 配置：
   - Chromium 浏览器路径
   - ChromeDriver 路径
   - split UserData profiles root
4. 保存配置。

## MCP 接入

默认 daemon MCP 地址通常为：

- `http://127.0.0.1:28888/mcp`

如果在 GUI / 配置中设置了 `mcp.api_token`，那么所有 MCP 请求都必须带：

- `Authorization: Bearer <token>`

## Token 配置说明

- `mcp.api_token`
  普通 MCP / 浏览器业务请求使用
- `control.api_token`
  GUI / control 接口使用，例如 dashboard、日志、keepalive 状态、插件管理、worker 启停等

如果 `control.api_token` 为空，则 `/_control/*` 接口保持禁用。

## 浏览器与 Driver 要求

- 使用用户自己机器上的 Chromium 或 Chrome
- 使用匹配版本的 ChromeDriver
- 浏览器主版本号与 driver 主版本号尽量保持一致

## Skill 安装

安装包已经自带 skill 模板。把需要的模板复制到 Codex 或项目自己的 skill 目录即可：

- `skill_templates/browser-identity-mcp.SKILL.md`
- `skill_templates/browser-identity-mcp-wsl.SKILL.md`

如果使用 Codex 手动配置 MCP，通常还需要在 `~/.codex/config.toml` 或 Windows 对应的 Codex 配置目录里加入：

```toml
[mcp_servers.browserIdentity]
url = "http://127.0.0.1:28888/mcp"

[mcp_servers.browserIdentity.http_headers]
Authorization = "Bearer <token>"
```

## 新环境中新建 Profile 的来源逻辑

现在的新建 profile 逻辑并不依赖 mirror 快照。

新建流程是：

1. 创建一个独立的 split UserData 根目录，例如 `UserDataProfile1`
2. 在里面创建 Chromium profile 目录，例如 `Profile 1`
3. 用内置的 `resources/bookmarks_template.html` 初始化书签
4. 其余 Chromium 状态由浏览器在首次启动时自动生成

所以在一台全新机器上，只要用户已经配置好：

- split UserData 根目录
- Chromium 路径
- ChromeDriver 路径

就可以正常创建新的 profile，而不需要依赖已有 mirror 数据。

## 引擎说明

- `playwright_cli`
  默认高吞吐 MCP 引擎
- `selenium_uc`
  更适合反爬、challenge、拖动、手势等场景
- `patchright`
  更适合复杂前端诊断和结构化提取

## 备注

- 安装包已经内置书签模板、skill 模板、发布说明和最新指纹插件资源，方便开箱使用。
- 浏览器和 driver 仍然是机器相关资产，不适合直接强行内置到本项目发布包里。
