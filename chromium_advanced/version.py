from importlib.metadata import PackageNotFoundError, version


APP_VERSION = "0.1.0"


def get_app_version() -> str:
    try:
        return str(version("mcp-chromium-advanced") or APP_VERSION)
    except PackageNotFoundError:
        return APP_VERSION

