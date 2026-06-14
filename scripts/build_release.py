import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chromium_advanced.version import get_app_version


DIST_ROOT = PROJECT_ROOT / "dist"
DIST_RELEASE_ROOT = PROJECT_ROOT / "dist_release"
OUT_ROOT = PROJECT_ROOT / "out"


def run(command, *, cwd=None):
    completed = subprocess.run(command, cwd=cwd or PROJECT_ROOT, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(str(item) for item in command)}")


def ensure_clean_dir(path: Path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def write_release_info(target_dir: Path, artifact_name: str):
    target_dir.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        [
            f"version={get_app_version()}",
            f"platform={sys.platform}",
            f"artifact={artifact_name}",
            "note=Chromium and ChromeDriver are not bundled in this release. Configure them in the GUI after startup.",
            "skills=docs/skill_templates/browser-identity-mcp.SKILL.md ; docs/skill_templates/browser-identity-mcp-wsl.SKILL.md",
        ]
    )
    (target_dir / "release-info.txt").write_text(text + "\n", encoding="utf-8")


def build_windows():
    run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(PROJECT_ROOT / "build_chromium_manage_gui_exe.ps1")])
    return DIST_ROOT


def build_portable_source_bundle():
    ensure_clean_dir(DIST_RELEASE_ROOT)
    (DIST_RELEASE_ROOT / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "run_gui.py", DIST_RELEASE_ROOT / "bin" / "run_gui.py")
    shutil.copytree(PROJECT_ROOT / "chromium_advanced", DIST_RELEASE_ROOT / "chromium_advanced")
    shutil.copytree(PROJECT_ROOT / "resources", DIST_RELEASE_ROOT / "resources")
    shutil.copytree(PROJECT_ROOT / "docs" / "skill_templates", DIST_RELEASE_ROOT / "docs" / "skill_templates")
    for name in ("pyproject.toml", "README.md", "requirements.txt"):
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


def main():
    parser = argparse.ArgumentParser(description="Build release artifacts for the current platform.")
    parser.add_argument("--artifact-name", required=True)
    args = parser.parse_args()

    ensure_clean_dir(OUT_ROOT)
    artifact_name = str(args.artifact_name).strip()
    write_release_info(OUT_ROOT, artifact_name)

    if sys.platform.startswith("win"):
        source_dir = build_windows()
        shutil.copytree(PROJECT_ROOT / "docs" / "skill_templates", OUT_ROOT / "skill_templates")
        package_zip(source_dir, OUT_ROOT / f"{artifact_name}.zip")
        return

    source_dir = build_portable_source_bundle()
    if sys.platform == "darwin":
        package_zip(source_dir, OUT_ROOT / f"{artifact_name}.zip")
        return

    package_targz(source_dir, OUT_ROOT / f"{artifact_name}.tar.gz")


if __name__ == "__main__":
    main()
