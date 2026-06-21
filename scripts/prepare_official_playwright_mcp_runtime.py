from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_ROOT = PROJECT_ROOT / "resources" / "runtime"
NODE_ROOT = RUNTIME_ROOT / "node"
OFFICIAL_ROOT = RUNTIME_ROOT / "official_playwright_mcp"


def run(command: list[str], *, cwd: Path | None = None) -> None:
    completed = subprocess.run(command, cwd=str(cwd or PROJECT_ROOT), check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}")


def clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def detect_node_source() -> Path:
    candidates = [
        Path(os.environ.get("OFFICIAL_MCP_NODE_SOURCE", "")).expanduser(),
        Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node",
        Path(sys.prefix) / "Lib" / "site-packages" / "patchright" / "driver",
        Path(sys.prefix) / "Lib" / "site-packages" / "playwright" / "driver",
    ]
    for candidate in candidates:
        if not str(candidate):
            continue
        if (candidate / "node.exe").exists() or (candidate / "bin" / "node").exists():
            return candidate
    raise FileNotFoundError(
        "No Node.js runtime source found. Set OFFICIAL_MCP_NODE_SOURCE or install a local runtime first."
    )


def resolve_node_executable(node_root: Path) -> Path:
    for candidate in (node_root / "node.exe", node_root / "bin" / "node", node_root / "node"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Bundled node executable not found under {node_root}")


def prepare_node_runtime(source: Path) -> None:
    clean_dir(NODE_ROOT)
    shutil.copytree(source, NODE_ROOT, dirs_exist_ok=True)


def prepare_official_runtime(node_executable: Path, package_version: str) -> None:
    template_bridge = OFFICIAL_ROOT / "bridge.mjs"
    bridge_text = template_bridge.read_text(encoding="utf-8") if template_bridge.exists() else ""
    clean_dir(OFFICIAL_ROOT)
    if bridge_text:
        (OFFICIAL_ROOT / "bridge.mjs").write_text(bridge_text, encoding="utf-8")
    package_json = {
        "name": "chromium-advanced-official-playwright-mcp-runtime",
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "dependencies": {
            "@modelcontextprotocol/sdk": "^1.29.0",
            "@playwright/mcp": package_version,
        },
    }
    (OFFICIAL_ROOT / "package.json").write_text(json.dumps(package_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run([str(node_executable), "-e", "console.log(process.version)"], cwd=OFFICIAL_ROOT)
    demo_runtime = PROJECT_ROOT / "tmp" / "demo" / "official_playwright_mcp_bridge_min" / "node_modules"
    if demo_runtime.exists():
        shutil.copytree(demo_runtime, OFFICIAL_ROOT / "node_modules", dirs_exist_ok=True)
    else:
        npm_cli = NODE_ROOT / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if npm_cli.exists():
            run([str(node_executable), str(npm_cli), "install", "--omit=dev"], cwd=OFFICIAL_ROOT)
        else:
            npm_path = shutil.which("npm")
            if npm_path:
                run([npm_path, "install", "--omit=dev"], cwd=OFFICIAL_ROOT)
            else:
                raise RuntimeError(
                    "Unable to prepare official_playwright_mcp runtime: no reusable demo node_modules, no bundled npm, and no system npm found."
                )
    required_paths = [
        OFFICIAL_ROOT / "bridge.mjs",
        OFFICIAL_ROOT / "node_modules" / "@playwright" / "mcp",
        OFFICIAL_ROOT / "node_modules" / "@modelcontextprotocol" / "sdk",
    ]
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise RuntimeError(f"official_playwright_mcp runtime is incomplete after prepare: missing={missing}")


def write_manifest(node_source: Path, package_version: str) -> None:
    manifest = {
        "prepared_at": subprocess.check_output(
            [sys.executable, "-c", "from datetime import datetime; print(datetime.now().isoformat())"],
            text=True,
        ).strip(),
        "node_source": str(node_source),
        "package_version": package_version,
        "runtime_root": str(RUNTIME_ROOT),
    }
    (RUNTIME_ROOT / "official_playwright_mcp.runtime.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare bundled Node.js and @playwright/mcp runtime under resources/runtime.")
    parser.add_argument("--package-version", default="^0.0.76")
    args = parser.parse_args()

    node_source = detect_node_source()
    prepare_node_runtime(node_source)
    node_executable = resolve_node_executable(NODE_ROOT)
    prepare_official_runtime(node_executable, str(args.package_version or "^0.0.76"))
    write_manifest(node_source, str(args.package_version or "^0.0.76"))
    print(str(RUNTIME_ROOT))


if __name__ == "__main__":
    main()
