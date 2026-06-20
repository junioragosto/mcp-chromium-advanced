import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import urllib.parse
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chromium_advanced.version import get_app_version


DIST_ROOT = PROJECT_ROOT / "dist"
DIST_RELEASE_ROOT = PROJECT_ROOT / "dist_release"
OUT_ROOT = PROJECT_ROOT / "out"
BUILD_STAGE_ROOT = PROJECT_ROOT / "build_stage_release"
FINGERPRINT_LATEST_API = "https://api.github.com/repos/omegaee/my-fingerprint/releases/latest"
FINGERPRINT_LATEST_PAGE = "https://github.com/omegaee/my-fingerprint/releases/latest"
FINGERPRINT_ASSET_PATTERN = re.compile(r"^my-fingerprint-chrome-.*\.zip$", re.IGNORECASE)
FINGERPRINT_EXPANDED_ASSETS_PATTERN = re.compile(
    r'(?:href|src)="(?P<href>https://github\.com/omegaee/my-fingerprint/releases/expanded_assets/[^"]+|/omegaee/my-fingerprint/releases/expanded_assets/[^"]+)"',
    re.IGNORECASE,
)
FINGERPRINT_DOWNLOAD_HREF_PATTERN = re.compile(
    r'href="(?P<href>/omegaee/my-fingerprint/releases/download/[^"]+/my-fingerprint-chrome-[^"]+\.zip)"',
    re.IGNORECASE,
)
LOCAL_FINGERPRINT_FALLBACKS = [
    PROJECT_ROOT / "extensions",
    PROJECT_ROOT / "local_extensions",
]


def run(command, *, cwd=None):
    completed = subprocess.run(command, cwd=cwd or PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(str(item) for item in command)}")


def ensure_clean_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def pyinstaller_data_separator() -> str:
    return ";" if sys.platform.startswith("win") else ":"


def common_hidden_imports() -> list[str]:
    return [
        "selenium.webdriver.common.action_chains",
        "selenium.webdriver.common.actions.action_builder",
        "selenium.webdriver.common.actions.pointer_input",
        "selenium.webdriver.common.actions.mouse_button",
    ]


def github_headers(*, accept: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": "mcp-chromium-advanced-build-release",
    }
    token = str(os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    return headers


def fetch_latest_fingerprint_release() -> dict:
    request = urllib.request.Request(
        FINGERPRINT_LATEST_API,
        headers=github_headers(accept="application/vnd.github+json"),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_latest_fingerprint_release_from_html() -> dict:
    request = urllib.request.Request(
        FINGERPRINT_LATEST_PAGE,
        headers=github_headers(accept="text/html,application/xhtml+xml"),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        html = response.read().decode("utf-8", errors="replace")

    href_match = FINGERPRINT_DOWNLOAD_HREF_PATTERN.search(html)
    if href_match is None:
        expanded_match = FINGERPRINT_EXPANDED_ASSETS_PATTERN.search(html)
        if expanded_match is None:
            raise RuntimeError("latest my-fingerprint release page does not expose expanded assets")
        expanded_url = urllib.parse.urljoin("https://github.com", expanded_match.group("href"))
        expanded_request = urllib.request.Request(
            expanded_url,
            headers=github_headers(accept="text/html,application/xhtml+xml"),
        )
        with urllib.request.urlopen(expanded_request, timeout=30) as expanded_response:
            expanded_html = expanded_response.read().decode("utf-8", errors="replace")
        href_match = FINGERPRINT_DOWNLOAD_HREF_PATTERN.search(expanded_html)
        if href_match is None:
            raise RuntimeError("latest my-fingerprint release page does not contain a chrome zip asset")

    download_url = urllib.parse.urljoin("https://github.com", href_match.group("href"))
    asset_name = Path(urllib.parse.urlparse(download_url).path).name
    tag_name = final_url.rstrip("/").split("/")[-1] if final_url.rstrip("/") else ""
    return {
        "tag_name": tag_name,
        "name": tag_name,
        "html_url": final_url,
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": download_url,
            }
        ],
    }


def resolve_latest_fingerprint_release() -> dict:
    try:
        return fetch_latest_fingerprint_release()
    except Exception:
        return fetch_latest_fingerprint_release_from_html()


def load_local_fingerprint_fallback() -> dict:
    for base_dir in LOCAL_FINGERPRINT_FALLBACKS:
        if not base_dir.exists():
            continue
        zip_path = base_dir / "fingerprint-extension.zip"
        metadata_path = base_dir / "fingerprint-extension.release.json"
        if not zip_path.exists():
            continue
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        return {
            "zip_path": zip_path,
            "metadata": {
                "source_repo": str(metadata.get("source_repo", "") or "omegaee/my-fingerprint"),
                "tag_name": str(metadata.get("tag_name", "") or "local-cache"),
                "release_name": str(metadata.get("release_name", "") or "local-cache"),
                "release_url": str(metadata.get("release_url", "") or ""),
                "asset_name": str(metadata.get("asset_name", "") or zip_path.name),
                "asset_download_url": str(metadata.get("asset_download_url", "") or ""),
                "source_mode": "local-cache",
                "source_path": str(zip_path),
            },
        }
    raise RuntimeError("no local fingerprint extension fallback asset is available")


def download_latest_fingerprint_zip(target_dir: Path) -> dict:
    target_dir.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir / "fingerprint-extension.zip"
    metadata_path = target_dir / "fingerprint-extension.release.json"
    try:
        release = resolve_latest_fingerprint_release()
        assets = release.get("assets", [])
        asset = None
        for candidate in assets:
            name = str(candidate.get("name", "")).strip()
            if FINGERPRINT_ASSET_PATTERN.match(name):
                asset = candidate
                break
        if asset is None:
            raise RuntimeError("latest my-fingerprint release does not contain a chrome zip asset")

        download_url = str(asset.get("browser_download_url", "")).strip()
        asset_name = str(asset.get("name", "")).strip() or "fingerprint-extension.zip"
        if not download_url:
            raise RuntimeError("latest my-fingerprint asset is missing browser_download_url")

        with urllib.request.urlopen(
            urllib.request.Request(download_url, headers=github_headers()),
            timeout=120,
        ) as response:
            zip_path.write_bytes(response.read())

        metadata = {
            "source_repo": "omegaee/my-fingerprint",
            "tag_name": release.get("tag_name", ""),
            "release_name": release.get("name", ""),
            "release_url": release.get("html_url", ""),
            "asset_name": asset_name,
            "asset_download_url": download_url,
            "source_mode": "network",
        }
    except Exception:
        fallback = load_local_fingerprint_fallback()
        shutil.copy2(fallback["zip_path"], zip_path)
        metadata = dict(fallback["metadata"])

    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def write_release_info(target_dir: Path, artifact_name: str, fingerprint_meta: dict):
    target_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            f"version={get_app_version()}",
            f"platform={sys.platform}",
            f"artifact={artifact_name}",
            "note=Chromium and ChromeDriver are not bundled in this release. Configure them in the GUI after startup.",
            "skills=skill_templates/browser-identity-mcp.SKILL.md ; skill_templates/browser-identity-mcp-wsl.SKILL.md",
            f"fingerprint_tag={fingerprint_meta.get('tag_name', '')}",
            f"fingerprint_asset={fingerprint_meta.get('asset_name', '')}",
            f"fingerprint_release_url={fingerprint_meta.get('release_url', '')}",
        ]
    )
    (target_dir / "release-info.txt").write_text(text + "\n", encoding="utf-8")


def copy_release_documents(target_dir: Path):
    shutil.copy2(PROJECT_ROOT / "docs" / "release_readme.md", target_dir / "release_readme.md")
    shutil.copy2(PROJECT_ROOT / "docs" / "release_zh.md", target_dir / "release_zh.md")


def copy_common_release_assets(target_dir: Path):
    shutil.copytree(PROJECT_ROOT / "docs" / "skill_templates", target_dir / "skill_templates")
    shutil.copytree(PROJECT_ROOT / "resources", target_dir / "resources")
    shutil.copy2(PROJECT_ROOT / "chromium_profiles.example.json", target_dir / "chromium_profiles.example.json")
    copy_release_documents(target_dir)
    extensions_dir = target_dir / "extensions"
    fingerprint_meta = download_latest_fingerprint_zip(extensions_dir)
    return fingerprint_meta


def build_windows():
    run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(PROJECT_ROOT / "build_chromium_manage_gui_exe.ps1")])
    return DIST_ROOT


def build_posix_binaries():
    ensure_clean_dir(BUILD_STAGE_ROOT)
    ensure_clean_dir(DIST_ROOT)

    data_sep = pyinstaller_data_separator()
    windows_icon_path = PROJECT_ROOT / "resources" / "chromium_profile_manager.ico"
    macos_icon_path = PROJECT_ROOT / "resources" / "chromium_profile_manager.icns"
    gui_common = [
        sys.executable,
        "-m",
        "PyInstaller",
        "-y",
        "--workpath",
        str(BUILD_STAGE_ROOT),
        "--distpath",
        str(DIST_ROOT),
        "--onedir",
        "--windowed",
        "--name",
        "ChromiumProfileManager",
        "--copy-metadata",
        "fastmcp",
        "--collect-all",
        "patchright",
        "--collect-data",
        "rich",
        "--collect-submodules",
        "rich._unicode_data",
        "--add-data",
        f"resources{data_sep}resources",
    ]
    if sys.platform == "darwin" and macos_icon_path.exists():
        gui_common.extend(["--icon", str(macos_icon_path)])
    elif sys.platform.startswith("win") and windows_icon_path.exists():
        gui_common.extend(["--icon", str(windows_icon_path)])
    for hidden_import in common_hidden_imports():
        gui_common.extend(["--hidden-import", hidden_import])
    gui_common.append(str(PROJECT_ROOT / "run_gui.py"))
    run(gui_common)

    for entry_name, script_path, collect_patchright in (
        ("ChromiumMcpDaemon", PROJECT_ROOT / "chromium_advanced" / "mcp_daemon.py", False),
        ("ChromiumMcpWorker", PROJECT_ROOT / "chromium_advanced" / "mcp_server.py", True),
    ):
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "-y",
            "--workpath",
            str(BUILD_STAGE_ROOT),
            "--distpath",
            str(DIST_ROOT),
            "--onedir",
            "--name",
            entry_name,
            "--copy-metadata",
            "fastmcp",
            "--collect-data",
            "rich",
            "--collect-submodules",
            "rich._unicode_data",
        ]
        if sys.platform == "darwin" and macos_icon_path.exists():
            command.extend(["--icon", str(macos_icon_path)])
        elif sys.platform.startswith("win") and windows_icon_path.exists():
            command.extend(["--icon", str(windows_icon_path)])
        if collect_patchright:
            command.extend(["--collect-all", "patchright"])
        for hidden_import in common_hidden_imports():
            command.extend(["--hidden-import", hidden_import])
        command.append(str(script_path))
        run(command)
    return DIST_ROOT


def build_portable_source_bundle():
    ensure_clean_dir(DIST_RELEASE_ROOT)
    (DIST_RELEASE_ROOT / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "run_gui.py", DIST_RELEASE_ROOT / "bin" / "run_gui.py")
    shutil.copytree(PROJECT_ROOT / "chromium_advanced", DIST_RELEASE_ROOT / "chromium_advanced")
    shutil.copytree(PROJECT_ROOT / "resources", DIST_RELEASE_ROOT / "resources")
    shutil.copytree(PROJECT_ROOT / "docs" / "skill_templates", DIST_RELEASE_ROOT / "skill_templates")
    copy_release_documents(DIST_RELEASE_ROOT)
    for name in ("pyproject.toml", "README.md", "README_zh.md", "requirements.txt", "chromium_profiles.example.json"):
        shutil.copy2(PROJECT_ROOT / name, DIST_RELEASE_ROOT / name)
    return DIST_RELEASE_ROOT


def package_zip(source_dir: Path, output_file: Path):
    with zipfile.ZipFile(output_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir))


def package_targz(source_dir: Path, output_file: Path):
    with tarfile.open(output_file, "w:gz") as archive:
        archive.add(source_dir, arcname=".")


def build_release_root(artifact_name: str) -> tuple[Path, dict]:
    ensure_clean_dir(OUT_ROOT)
    package_root = OUT_ROOT / "package"
    ensure_clean_dir(package_root)
    fingerprint_meta = copy_common_release_assets(package_root)
    write_release_info(package_root, artifact_name, fingerprint_meta)
    return package_root, fingerprint_meta


def copy_tree_contents(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        destination = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)


def main():
    parser = argparse.ArgumentParser(description="Build release artifacts for the current platform.")
    parser.add_argument("--artifact-name", required=True)
    args = parser.parse_args()

    artifact_name = str(args.artifact_name).strip()
    package_root, _fingerprint_meta = build_release_root(artifact_name)

    if sys.platform.startswith("win"):
        source_dir = build_windows()
        copy_tree_contents(source_dir, package_root)
        package_zip(package_root, OUT_ROOT / f"{artifact_name}.zip")
        return

    if sys.platform == "darwin":
        source_dir = build_posix_binaries()
        shutil.copytree(source_dir, package_root / "app")
        package_zip(package_root, OUT_ROOT / f"{artifact_name}.zip")
        return

    if sys.platform.startswith("linux"):
        source_dir = build_posix_binaries()
        shutil.copytree(source_dir, package_root / "app")
        package_targz(package_root, OUT_ROOT / f"{artifact_name}.tar.gz")
        return

    source_dir = build_portable_source_bundle()
    shutil.copytree(source_dir, package_root / "app")
    package_targz(package_root, OUT_ROOT / f"{artifact_name}.tar.gz")


if __name__ == "__main__":
    main()
