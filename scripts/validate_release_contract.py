from __future__ import annotations

import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
VERSION_HELPER_PATH = PROJECT_ROOT / "chromium_advanced" / "version.py"
RELEASE_MANIFEST_PATH = PROJECT_ROOT / "release-manifest.json"
WORKFLOW_DIR = PROJECT_ROOT / ".github" / "workflows"

PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)
FALLBACK_VERSION_RE = re.compile(r'^\s*FALLBACK_APP_VERSION\s*=\s*"(?P<version>[^"]+)"\s*$', re.MULTILINE)
PLAYWRIGHT_CLI_PIN_RE = re.compile(r"@playwright/cli@(?P<version>[A-Za-z0-9._+-]+)")


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


def parse_workflow_cli_versions() -> dict[str, str]:
    workflow_versions: dict[str, str] = {}
    required_workflows = {"ci.yml", "release-candidate.yml", "release-publish.yml"}
    missing = [name for name in required_workflows if not (WORKFLOW_DIR / name).exists()]
    if missing:
        raise RuntimeError(f"required workflow files are missing: {', '.join(sorted(missing))}")
    for workflow_path in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = read_text(workflow_path)
        match = PLAYWRIGHT_CLI_PIN_RE.search(text)
        if match is None:
            continue
        workflow_versions[workflow_path.name] = str(match.group("version") or "").strip()
    if not workflow_versions:
        raise RuntimeError("no workflow contains a pinned @playwright/cli version")
    return workflow_versions


def validate_manifest() -> dict:
    manifest = json.loads(read_text(RELEASE_MANIFEST_PATH))
    runtime = manifest.get("runtime", {})
    official = runtime.get("official_playwright_mcp", {})
    playwright_cli = runtime.get("playwright_cli", {})
    node = runtime.get("node", {})
    assets = manifest.get("assets", {})
    fingerprint = assets.get("fingerprint_extension", {})

    required_values = {
        "runtime.node.source": node.get("source"),
        "runtime.node.playwright_core_version": node.get("playwright_core_version"),
        "runtime.official_playwright_mcp.package_version": official.get("package_version"),
        "runtime.official_playwright_mcp.sdk_version": official.get("sdk_version"),
        "runtime.playwright_cli.package_version": playwright_cli.get("package_version"),
        "assets.fingerprint_extension.source_mode": fingerprint.get("source_mode"),
        "assets.fingerprint_extension.asset_name": fingerprint.get("asset_name"),
    }
    missing = [key for key, value in required_values.items() if not str(value or "").strip()]
    if missing:
        raise RuntimeError(f"release-manifest.json is missing required fields: {', '.join(missing)}")
    return manifest


def main() -> None:
    pyproject_version = parse_pyproject_version()
    fallback_version = parse_fallback_version()
    if pyproject_version != fallback_version:
        raise RuntimeError(
            f"version mismatch: pyproject.toml={pyproject_version} "
            f"but chromium_advanced/version.py fallback={fallback_version}"
        )

    manifest = validate_manifest()
    workflow_cli_versions = parse_workflow_cli_versions()
    manifest_cli_version = str(
        (((manifest.get("runtime", {}) or {}).get("playwright_cli", {}) or {}).get("package_version", "") or "")
    ).strip()
    mismatches = {
        workflow_name: workflow_version
        for workflow_name, workflow_version in workflow_cli_versions.items()
        if workflow_version != manifest_cli_version
    }
    if mismatches:
        rendered = ", ".join(f"{name}={version}" for name, version in sorted(mismatches.items()))
        raise RuntimeError(
            f"playwright-cli version mismatch: {rendered} manifest={manifest_cli_version}"
        )

    print(
        json.dumps(
            {
                "ok": True,
                "app_version": pyproject_version,
                "playwright_cli_version": manifest_cli_version,
                "workflow_count": len(workflow_cli_versions),
                "manifest_path": str(RELEASE_MANIFEST_PATH),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
