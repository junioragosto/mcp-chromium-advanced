import argparse
import hashlib
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
from datetime import datetime, timezone


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chromium_advanced.version import get_app_version


DIST_ROOT = PROJECT_ROOT / "dist"
OUT_ROOT = PROJECT_ROOT / "out"
OUT_STAGE_ROOT = OUT_ROOT / "_stage"
BUILD_STAGE_ROOT = PROJECT_ROOT / "build_stage_release"
RELEASE_MANIFEST_PATH = PROJECT_ROOT / "release-manifest.json"
RELEASE_DOC_EN_PATH = PROJECT_ROOT / "docs" / "05-reference" / "RELEASE_README.md"
RELEASE_DOC_ZH_PATH = PROJECT_ROOT / "docs" / "05-reference" / "RELEASE_README_zh.md"
AI_INSTALLATION_RUNBOOK_PATH = PROJECT_ROOT / "docs" / "01-getting-started" / "AI_INSTALLATION_RUNBOOK.md"
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
DEFAULT_UPDATE_CHANNEL = "stable"


def run(command, *, cwd=None):
    completed = subprocess.run(command, cwd=cwd or PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(str(item) for item in command)}")


def ensure_clean_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def load_release_manifest() -> dict:
    return json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))


def now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_version_for_artifact(version_text: str) -> str:
    text = str(version_text or "").strip()
    if not text:
        return "0.0.0"
    return re.sub(r"[^A-Za-z0-9.+_-]+", "-", text)


def detect_release_channel(version_text: str) -> str:
    lowered = str(version_text or "").strip().lower()
    if "-rc." in lowered or lowered.endswith("-rc"):
        return "rc"
    if "-beta." in lowered or lowered.endswith("-beta"):
        return "beta"
    if "-dev." in lowered or lowered.endswith("-dev"):
        return "dev"
    return DEFAULT_UPDATE_CHANNEL


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


def resolve_manifest_fingerprint_metadata() -> dict:
    manifest = load_release_manifest()
    assets = manifest.get("assets", {})
    fingerprint = assets.get("fingerprint_extension", {})
    if not isinstance(fingerprint, dict):
        raise RuntimeError("release-manifest.json is missing assets.fingerprint_extension")
    return {
        "source_repo": str(fingerprint.get("source_repo", "") or "omegaee/my-fingerprint"),
        "tag_name": str(fingerprint.get("tag_name", "") or "").strip(),
        "release_name": str(fingerprint.get("release_name", "") or str(fingerprint.get("tag_name", "") or "")).strip(),
        "release_url": str(fingerprint.get("release_url", "") or "").strip(),
        "asset_name": str(fingerprint.get("asset_name", "") or "fingerprint-extension.zip").strip(),
        "asset_download_url": str(fingerprint.get("asset_download_url", "") or "").strip(),
        "source_mode": str(fingerprint.get("source_mode", "") or "local-cache").strip(),
    }


def download_fingerprint_zip(target_dir: Path) -> dict:
    target_dir.mkdir(parents=True, exist_ok=True)
    zip_path = target_dir / "fingerprint-extension.zip"
    metadata_path = target_dir / "fingerprint-extension.release.json"
    manifest_meta = resolve_manifest_fingerprint_metadata()
    source_mode = str(manifest_meta.get("source_mode", "") or "").strip().lower()
    if source_mode == "network":
        download_url = str(manifest_meta.get("asset_download_url", "") or "").strip()
        if not download_url:
            raise RuntimeError("release-manifest.json requires fingerprint asset_download_url for network mode")
        with urllib.request.urlopen(
            urllib.request.Request(download_url, headers=github_headers()),
            timeout=120,
        ) as response:
            zip_path.write_bytes(response.read())
        metadata = dict(manifest_meta)
    else:
        fallback = load_local_fingerprint_fallback()
        shutil.copy2(fallback["zip_path"], zip_path)
        metadata = dict(manifest_meta)
        metadata.setdefault("source_path", str(fallback["zip_path"]))

    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return metadata


def write_release_info(target_dir: Path, artifact_name: str, fingerprint_meta: dict):
    target_dir.mkdir(parents=True, exist_ok=True)
    app_version = get_app_version()
    text = "\n".join(
        [
            f"version={app_version}",
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_release_documents(target_dir: Path):
    shutil.copy2(PROJECT_ROOT / "README.md", target_dir / "README.md")
    shutil.copy2(PROJECT_ROOT / "README_zh.md", target_dir / "README_zh.md")
    shutil.copy2(AI_INSTALLATION_RUNBOOK_PATH, target_dir / "AI_INSTALLATION_RUNBOOK.md")
    shutil.copy2(RELEASE_DOC_EN_PATH, target_dir / "RELEASE_README.md")
    shutil.copy2(RELEASE_DOC_ZH_PATH, target_dir / "RELEASE_README_zh.md")


def build_asset_entry(*, file_name: str, download_url: str, sha256: str, size: int, platform_name: str, arch: str) -> dict:
    return {
        "platform": platform_name,
        "arch": arch,
        "file_name": file_name,
        "download_url": download_url,
        "sha256": sha256,
        "size": int(size),
    }


def copy_common_release_assets(target_dir: Path):
    shutil.copytree(PROJECT_ROOT / "docs" / "skill_templates", target_dir / "skill_templates")
    shutil.copytree(PROJECT_ROOT / "resources", target_dir / "resources")
    shutil.copy2(PROJECT_ROOT / "chromium_profiles.example.json", target_dir / "chromium_profiles.example.json")
    shutil.copy2(RELEASE_MANIFEST_PATH, target_dir / "release-manifest.json")
    copy_release_documents(target_dir)
    extensions_dir = target_dir / "extensions"
    fingerprint_meta = download_fingerprint_zip(extensions_dir)
    return fingerprint_meta


def build_windows():
    run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(PROJECT_ROOT / "build_chromium_manage_gui_exe.ps1")])
    return DIST_ROOT


def build_posix_wrapper_source(wrapper_name: str, mode_flag: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    script_path = target_dir / f"{wrapper_name}_wrapper.py"
    script_path.write_text(
        "\n".join(
            [
                "import os",
                "import subprocess",
                "import sys",
                "",
                "",
                "def main():",
                "    base_dir = os.path.dirname(os.path.abspath(sys.argv[0]))",
                "    program = os.path.join(base_dir, 'ChromiumProfileManager', 'ChromiumProfileManager')",
                "    if sys.platform == 'darwin':",
                "        app_candidate = os.path.join(base_dir, 'ChromiumProfileManager.app', 'Contents', 'MacOS', 'ChromiumProfileManager')",
                "        if os.path.isfile(app_candidate):",
                "            program = app_candidate",
                "    if not os.path.isfile(program):",
                "        raise SystemExit(f'ChromiumProfileManager runtime not found next to wrapper: {program}')",
                f"    args = [program, '{mode_flag}', *sys.argv[1:]]",
                "    raise SystemExit(subprocess.call(args))",
                "",
                "",
                "if __name__ == '__main__':",
                "    main()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return script_path


def build_posix_binaries():
    ensure_clean_dir(BUILD_STAGE_ROOT)
    ensure_clean_dir(DIST_ROOT)
    wrapper_root = BUILD_STAGE_ROOT / "packaging_wrappers"
    ensure_clean_dir(wrapper_root)

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
        "--collect-submodules",
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

    for entry_name, mode_flag in (
        ("ChromiumMcpDaemon", "--run-mcp-daemon"),
        ("ChromiumMcpWorker", "--run-mcp-worker"),
    ):
        script_path = build_posix_wrapper_source(entry_name, mode_flag, wrapper_root)
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
        command.append(str(script_path))
        run(command)
    return DIST_ROOT


def build_portable_source_bundle():
    ensure_clean_dir(OUT_STAGE_ROOT)
    (OUT_STAGE_ROOT / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "run_gui.py", OUT_STAGE_ROOT / "bin" / "run_gui.py")
    shutil.copytree(PROJECT_ROOT / "chromium_advanced", OUT_STAGE_ROOT / "chromium_advanced")
    shutil.copytree(PROJECT_ROOT / "resources", OUT_STAGE_ROOT / "resources")
    shutil.copytree(PROJECT_ROOT / "docs" / "skill_templates", OUT_STAGE_ROOT / "skill_templates")
    copy_release_documents(OUT_STAGE_ROOT)
    for name in ("pyproject.toml", "README.md", "README_zh.md", "requirements.txt", "chromium_profiles.example.json"):
        shutil.copy2(PROJECT_ROOT / name, OUT_STAGE_ROOT / name)
    return OUT_STAGE_ROOT


def package_zip(source_dir: Path, output_file: Path):
    with zipfile.ZipFile(output_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source_dir))


def package_targz(source_dir: Path, output_file: Path):
    with tarfile.open(output_file, "w:gz") as archive:
        archive.add(source_dir, arcname=".")


def build_release_root(artifact_name: str) -> tuple[Path, dict]:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    OUT_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    legacy_package_root = OUT_ROOT / "package"
    if legacy_package_root.exists():
        shutil.rmtree(legacy_package_root)
    package_root = OUT_STAGE_ROOT / "package"
    ensure_clean_dir(package_root)
    for suffix in (".zip", ".tar.gz"):
        artifact_path = OUT_ROOT / f"{artifact_name}{suffix}"
        if artifact_path.exists():
            artifact_path.unlink()
    for metadata_name in (
        "release-metadata.json",
        "update-manifest-stable.json",
        "update-manifest-rc.json",
        "sha256sums.txt",
    ):
        metadata_path = OUT_ROOT / metadata_name
        if metadata_path.exists():
            metadata_path.unlink()
    fingerprint_meta = copy_common_release_assets(package_root)
    write_release_info(package_root, artifact_name, fingerprint_meta)
    return package_root, fingerprint_meta


def write_release_metadata(
    *,
    output_dir: Path,
    version_text: str,
    channel: str,
    release_manifest: dict,
    assets: list[dict],
    checksums: list[dict],
    release_notes_url: str,
    git_tag: str,
    git_commit: str,
) -> None:
    payload = {
        "version": version_text,
        "channel": channel,
        "git_tag": git_tag,
        "git_commit": git_commit,
        "published_at": now_iso8601(),
        "release_notes_url": release_notes_url,
        "release_manifest_version": int(release_manifest.get("schema_version", 0) or 0),
        "runtime": dict(release_manifest.get("runtime", {}) if isinstance(release_manifest.get("runtime", {}), dict) else {}),
        "assets": assets,
        "checksums": checksums,
    }
    (output_dir / "release-metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_update_manifest(
    *,
    output_dir: Path,
    manifest_name: str,
    version_text: str,
    channel: str,
    assets: list[dict],
    release_notes_url: str,
    mandatory: bool = False,
    min_supported_version: str = "",
    rollout_percentage: int = 100,
) -> None:
    payload = {
        "channel": channel,
        "version": version_text,
        "published_at": now_iso8601(),
        "notes_url": release_notes_url,
        "mandatory": bool(mandatory),
        "min_supported_version": str(min_supported_version or "").strip(),
        "rollout_percentage": int(rollout_percentage),
        "assets": assets,
    }
    (output_dir / manifest_name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_checksums(output_dir: Path, checksum_items: list[dict]) -> None:
    lines = [f"{item['sha256']}  {item['file_name']}" for item in checksum_items]
    (output_dir / "sha256sums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def detect_target_platform_and_arch(artifact_name: str) -> tuple[str, str]:
    lowered = str(artifact_name or "").lower()
    platform_name = "portable"
    arch = "unknown"
    if "windows" in lowered:
        platform_name = "windows"
    elif "macos" in lowered:
        platform_name = "macos"
    elif "linux" in lowered:
        platform_name = "linux"
    if lowered.endswith("-x64") or "-x64-" in lowered:
        arch = "x64"
    elif lowered.endswith("-arm64") or "-arm64-" in lowered:
        arch = "arm64"
    return platform_name, arch


def infer_release_notes_url(version_text: str) -> str:
    repository_url = str(os.environ.get("GITHUB_SERVER_URL", "https://github.com")).rstrip("/")
    repository_name = str(os.environ.get("GITHUB_REPOSITORY", "")).strip()
    if repository_name:
        return f"{repository_url}/{repository_name}/releases/tag/v{version_text}"
    return ""


def current_git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return ""
    return str(completed.stdout or "").strip()


def current_git_tag(version_text: str) -> str:
    env_ref_type = str(os.environ.get("GITHUB_REF_TYPE", "")).strip()
    env_ref_name = str(os.environ.get("GITHUB_REF_NAME", "")).strip()
    if env_ref_type == "tag" and env_ref_name:
        return env_ref_name
    return f"v{version_text}"


def finalize_release_outputs(*, output_dir: Path, artifact_path: Path, artifact_name: str, version_text: str) -> None:
    release_manifest = load_release_manifest()
    channel = detect_release_channel(version_text)
    artifact_sha256 = sha256_file(artifact_path)
    platform_name, arch = detect_target_platform_and_arch(artifact_name)
    download_url = infer_release_notes_url(version_text).replace(f"/releases/tag/v{version_text}", f"/releases/download/v{version_text}/{artifact_path.name}") if infer_release_notes_url(version_text) else ""
    primary_asset = build_asset_entry(
        file_name=artifact_path.name,
        download_url=download_url,
        sha256=artifact_sha256,
        size=artifact_path.stat().st_size,
        platform_name=platform_name,
        arch=arch,
    )
    checksum_items = [
        {
            "file_name": artifact_path.name,
            "sha256": artifact_sha256,
        }
    ]
    write_checksums(output_dir, checksum_items)
    release_notes_url = infer_release_notes_url(version_text)
    write_release_metadata(
        output_dir=output_dir,
        version_text=version_text,
        channel=channel,
        release_manifest=release_manifest,
        assets=[primary_asset],
        checksums=checksum_items,
        release_notes_url=release_notes_url,
        git_tag=current_git_tag(version_text),
        git_commit=current_git_commit(),
    )
    write_update_manifest(
        output_dir=output_dir,
        manifest_name="update-manifest-stable.json",
        version_text=version_text,
        channel="stable",
        assets=[primary_asset],
        release_notes_url=release_notes_url,
        mandatory=False,
        min_supported_version="",
        rollout_percentage=100,
    )
    write_update_manifest(
        output_dir=output_dir,
        manifest_name="update-manifest-rc.json",
        version_text=version_text,
        channel="rc",
        assets=[primary_asset],
        release_notes_url=release_notes_url,
        mandatory=False,
        min_supported_version="",
        rollout_percentage=100,
    )


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
    parser.add_argument("--artifact-name-base", required=True)
    args = parser.parse_args()

    artifact_name_base = str(args.artifact_name_base or "").strip()
    if not artifact_name_base:
        raise RuntimeError("artifact-name-base is required")
    app_version = get_app_version()
    artifact_name = f"{artifact_name_base}-{normalize_version_for_artifact(app_version)}"
    package_root, _fingerprint_meta = build_release_root(artifact_name)

    if sys.platform.startswith("win"):
        source_dir = build_windows()
        copy_tree_contents(source_dir, package_root)
        artifact_path = OUT_ROOT / f"{artifact_name}.zip"
        package_zip(package_root, artifact_path)
        finalize_release_outputs(output_dir=OUT_ROOT, artifact_path=artifact_path, artifact_name=artifact_name, version_text=app_version)
        return

    if sys.platform == "darwin":
        source_dir = build_posix_binaries()
        shutil.copytree(source_dir, package_root / "app")
        artifact_path = OUT_ROOT / f"{artifact_name}.zip"
        package_zip(package_root, artifact_path)
        finalize_release_outputs(output_dir=OUT_ROOT, artifact_path=artifact_path, artifact_name=artifact_name, version_text=app_version)
        return

    if sys.platform.startswith("linux"):
        source_dir = build_posix_binaries()
        shutil.copytree(source_dir, package_root / "app")
        artifact_path = OUT_ROOT / f"{artifact_name}.tar.gz"
        package_targz(package_root, artifact_path)
        finalize_release_outputs(output_dir=OUT_ROOT, artifact_path=artifact_path, artifact_name=artifact_name, version_text=app_version)
        return

    source_dir = build_portable_source_bundle()
    shutil.copytree(source_dir, package_root / "app")
    artifact_path = OUT_ROOT / f"{artifact_name}.tar.gz"
    package_targz(package_root, artifact_path)
    finalize_release_outputs(output_dir=OUT_ROOT, artifact_path=artifact_path, artifact_name=artifact_name, version_text=app_version)


if __name__ == "__main__":
    main()
