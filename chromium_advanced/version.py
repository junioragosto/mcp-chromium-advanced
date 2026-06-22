from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


PACKAGE_NAME = "mcp-chromium-advanced"
FALLBACK_APP_VERSION = "0.1.0"
_VERSION_PATTERN = re.compile(r'^\s*version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_declared_version_from_pyproject() -> str:
    pyproject_path = _project_root() / "pyproject.toml"
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except Exception:
        return FALLBACK_APP_VERSION
    match = _VERSION_PATTERN.search(text)
    if match is None:
        return FALLBACK_APP_VERSION
    return str(match.group("version") or FALLBACK_APP_VERSION).strip() or FALLBACK_APP_VERSION


def get_declared_app_version() -> str:
    return _read_declared_version_from_pyproject()


def get_app_version() -> str:
    try:
        resolved = str(version(PACKAGE_NAME) or "").strip()
        if resolved:
            return resolved
    except PackageNotFoundError:
        pass
    return get_declared_app_version()
