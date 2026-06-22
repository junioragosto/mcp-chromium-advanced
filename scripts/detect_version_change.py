from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
VERSION_HELPER_PATH = PROJECT_ROOT / "chromium_advanced" / "version.py"

PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)
FALLBACK_VERSION_RE = re.compile(r'^\s*FALLBACK_APP_VERSION\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_pyproject_version() -> str:
    match = PYPROJECT_VERSION_RE.search(read_text(PYPROJECT_PATH))
    if match is None:
        raise RuntimeError("pyproject.toml is missing project.version")
    return str(match.group("version") or "").strip()


def parse_fallback_version() -> str:
    match = FALLBACK_VERSION_RE.search(read_text(VERSION_HELPER_PATH))
    if match is None:
        raise RuntimeError("chromium_advanced/version.py is missing FALLBACK_APP_VERSION")
    return str(match.group("version") or "").strip()


def git_diff_name_only() -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD^", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return []
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def main() -> None:
    pyproject_version = parse_pyproject_version()
    fallback_version = parse_fallback_version()
    version_files_changed = set(git_diff_name_only())
    version_changed = bool(
        {"pyproject.toml", "chromium_advanced/version.py"}.intersection(version_files_changed)
    )
    if pyproject_version != fallback_version:
        raise RuntimeError(
            f"version mismatch: pyproject.toml={pyproject_version} "
            f"but chromium_advanced/version.py fallback={fallback_version}"
        )
    payload = {
        "version_changed": version_changed,
        "app_version": pyproject_version,
        "changed_files": sorted(version_files_changed),
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
