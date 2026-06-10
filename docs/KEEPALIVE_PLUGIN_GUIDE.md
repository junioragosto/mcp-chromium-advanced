# Keepalive Plugin Guide

This project supports a lightweight keepalive plugin runtime for adding new
site-specific keepalive logic without rebuilding the packaged application.

中文说明：保活站点已经按插件化方向设计。内置站点包括 `chatgpt`、`google`、`gmail`、`github`；未来新增 YouTube、YouTube Studio 或其他站点时，可以把 Python 插件放到插件目录，让 GUI 和保活任务自动发现。

## Plugin Directory

The default user plugin directory is:

```text
%APPDATA%\ChromiumProfileManager\workstates\keepalive_plugins
```

Additional directories can be configured in:

```json
{
  "keepalive": {
    "plugin_dirs": [
      "D:/my-keepalive-plugins"
    ]
  }
}
```

The GUI also exposes this setting in the keepalive settings panel. Separate
multiple directories with semicolons.

Only load plugins from trusted local directories. A keepalive plugin is Python
code and has the same local permissions as the application process.

## Minimal Plugin

Create a file such as:

```text
%APPDATA%\ChromiumProfileManager\workstates\keepalive_plugins\youtube_studio.py
```

Example using the class-style contract:

```python
class KeepalivePlugin:
    metadata = {
        "site_id": "youtube_studio",
        "display_name": "YouTube Studio",
        "home_url": "https://studio.youtube.com/",
        "icon_url": "https://www.youtube.com/s/desktop/favicon.ico",
    }

    def keepalive(self, context):
        browser = context["browser"]
        results = context["results"]
        log = context["log"]

        browser.goto("https://studio.youtube.com/")
        browser.wait_ready()
        log("opened YouTube Studio")
        return results.success("YouTube Studio opened")
```

The older function-style contract is still supported:

```python
def get_plugin():
    return {"site_id": "youtube_studio", "display_name": "YouTube Studio"}


def keepalive(context):
    return context["results"].success("ok")
```

## Plugin Metadata

Plugin metadata should return:

- `site_id`: stable lowercase identifier, for example `youtube_studio`.
- `display_name`: label shown in the GUI.
- `home_url`: site home URL, used as a fallback for icon discovery.
- `icon_url`: optional direct favicon/logo URL.

If `icon_url` is omitted and `home_url` is available, the runtime tries
`<scheme>://<host>/favicon.ico`.

## Logo Handling

There is no manual upload workflow.

The runtime automatically:

- reads `icon_url` or derives a favicon URL from `home_url`
- downloads the icon into the local cache
- reuses the cached file in future GUI renders
- falls back to text if the icon cannot be fetched

Icon cache directory:

```text
%APPDATA%\ChromiumProfileManager\workstates\keepalive_site_icons
```

## Runtime Context

The `keepalive(context)` function or class method receives:

- `context["site_id"]`
- `context["metadata"]`
- `context["driver"]`
- `context["browser"]`
- `context["results"]`
- `context["settings"]`
- `context["profile"]`
- `context["logger"]`
- `context["stop_controller"]`
- `context["log"]`

Return a dictionary:

```python
{
    "status": "success",      # success, signed_out, attention, failed, skipped
    "message": "short result",
    "signed_in": True,
}
```

Recommended status semantics:

- `success`: the site was clearly signed in and keepalive completed.
- `signed_out`: the site clearly showed login/sign-in UI.
- `attention`: manual handling may be needed, such as CAPTCHA, 2FA, rate limit, or ambiguous page state.
- `failed`: plugin execution failed unexpectedly.
- `skipped`: plugin intentionally skipped the run.

## Browser Helper API

`context["browser"]` wraps the Selenium driver with a smaller plugin-facing API:

- `goto(url, wait_ready=True, timeout=None)`
- `wait_ready(timeout=None)`
- `sleep(seconds)`
- `current_url()`
- `title()`
- `execute(script, *args)`
- `find(selector, by="css", timeout=None)`
- `find_all(selector, by="css")`
- `exists(selector, by="css", timeout=0)`
- `click(selector, by="css", timeout=None)`
- `fill(selector, text, by="css", timeout=None, clear=True)`
- `press(keys, selector="", by="css", timeout=None)`
- `text(selector="", by="css", timeout=None)`
- `html(selector="", by="css", timeout=None)`

Locator `by` values:

- `css`
- `xpath`
- `id`
- `name`
- `class`
- `tag`
- `link_text`
- `partial_link_text`

## Result Helper API

`context["results"]` provides a standard result factory:

- `success(message, **extra)`
- `signed_out(message, **extra)`
- `attention(message, **extra)`
- `failed(message, **extra)`
- `skipped(message, **extra)`

## Profile Association

Profiles and keepalive sites are associated through `profile.keepalive_sites`:

```json
{
  "profile_name": "Profile 4",
  "keepalive_sites": {
    "google": true,
    "youtube_studio": true
  }
}
```

This remains JSON-backed in v1. SQLite is intentionally deferred until the
project needs historical run records, trend queries, queues, or richer
profile-site account mappings.
