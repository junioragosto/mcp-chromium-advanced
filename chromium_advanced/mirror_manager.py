from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence

from chromium_advanced.chromium_profile_lib import (
    PROFILE_RUNTIME_LOCK_FILENAME,
    SPLIT_PROFILE_CACHE_DIRS,
    SPLIT_PROFILE_EXCLUDE_FILES,
    SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS,
    SPLIT_USER_DATA_ROOT_EXCLUDE_FILES,
    get_profile_directory_path,
    get_profile_user_data_root,
    get_user_data_profiles_root,
    normalize_config,
    now_text,
    write_json_atomic,
)


ROOT_SNAPSHOT_FILENAME = "template_root.zip"
ROOT_METADATA_FILENAME = "template_root.json"
PROFILE_METADATA_DIRNAME = "profiles"
RUNTIME_METADATA_FILENAME = "runtime_snapshot.json"
MIRROR_MANIFEST_FILENAME = "mirror_manifest.json"

def _slugify(text: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", str(text or "").strip()).strip("-").lower()
    return normalized or "profile"


def _atomic_replace(src_path: str, dst_path: str) -> None:
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    os.replace(src_path, dst_path)


def _walk_stats(path: str) -> Dict[str, int]:
    file_count = 0
    total_bytes = 0
    if not os.path.isdir(path):
        return {"file_count": 0, "bytes": 0}
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            file_count += 1
            file_path = os.path.join(dirpath, filename)
            try:
                total_bytes += os.path.getsize(file_path)
            except OSError:
                pass
    return {"file_count": file_count, "bytes": total_bytes}


def _archive_stats(zip_path: str) -> Dict[str, int]:
    if not os.path.exists(zip_path):
        return {"archive_bytes": 0}
    try:
        return {"archive_bytes": os.path.getsize(zip_path)}
    except OSError:
        return {"archive_bytes": 0}


@dataclass
class MirrorRuntimeInfo:
    profile_name: str
    runtime_root: str
    runtime_profile_dir: str
    runtime_id: str
    generated_at: str
    root_snapshot_generated_at: str
    profile_snapshot_generated_at: str


class MirrorManager:
    def __init__(self, config: Dict):
        self.config = normalize_config(config)
        self.paths = self.config.get("paths", {})
        self.mirror_settings = self.config.get("mirror", {})
        self.user_data_profiles_root = get_user_data_profiles_root(self.config)

    def disk_root(self) -> str:
        return os.path.join(
            self.user_data_profiles_root,
            str(self.mirror_settings.get("disk_dir_name", "mirror_disk")).strip() or "mirror_disk",
        )

    def runtime_root(self) -> str:
        return os.path.join(
            self.user_data_profiles_root,
            str(self.mirror_settings.get("runtime_dir_name", "runtime")).strip() or "runtime",
        )

    def root_snapshot_path(self) -> str:
        return os.path.join(self.disk_root(), ROOT_SNAPSHOT_FILENAME)

    def root_metadata_path(self) -> str:
        return os.path.join(self.disk_root(), ROOT_METADATA_FILENAME)

    def manifest_path(self) -> str:
        return os.path.join(self.disk_root(), MIRROR_MANIFEST_FILENAME)

    def profile_snapshot_dir(self) -> str:
        return os.path.join(self.disk_root(), PROFILE_METADATA_DIRNAME)

    def profile_snapshot_path(self, profile_name: str) -> str:
        return os.path.join(self.profile_snapshot_dir(), f"{_slugify(profile_name)}.zip")

    def profile_metadata_path(self, profile_name: str) -> str:
        return os.path.join(self.profile_snapshot_dir(), f"{_slugify(profile_name)}.json")

    def load_root_metadata(self) -> Dict:
        path = self.root_metadata_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def load_profile_metadata(self, profile_name: str) -> Dict:
        path = self.profile_metadata_path(profile_name)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def load_manifest(self) -> Dict:
        path = self.manifest_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def is_enabled(self) -> bool:
        return bool(self.mirror_settings.get("enabled", False))

    def _log(self, logger: Optional[Callable[[str], None]], message: str) -> None:
        if logger:
            logger(message)

    def _iter_root_items(self, source_profile_root: str) -> Iterable[str]:
        if not os.path.isdir(source_profile_root):
            return []
        results: List[str] = []
        for name in os.listdir(source_profile_root):
            if (
                name in SPLIT_USER_DATA_ROOT_EXCLUDE_FILES
                or name in SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS
                or re.match(r"^Profile\s+\d+$", name)
            ):
                continue
            results.append(name)
        return sorted(results)

    def _zip_root_snapshot(self, logger: Optional[Callable[[str], None]] = None) -> Dict:
        source_profile_root = ""
        for item in self.config.get("profiles", []):
            profile_name = str(item.get("profile_name", "")).strip()
            if not profile_name:
                continue
            candidate = get_profile_user_data_root(self.config, profile_name)
            if os.path.isdir(candidate):
                source_profile_root = candidate
                break
        if not source_profile_root:
            raise FileNotFoundError("no profile UserData root available for template snapshot")
        os.makedirs(self.disk_root(), exist_ok=True)
        fd, temp_zip_path = tempfile.mkstemp(prefix="chromium-root-snapshot-", suffix=".zip", dir=self.disk_root())
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for item_name in self._iter_root_items(source_profile_root):
                    source_path = os.path.join(source_profile_root, item_name)
                    if os.path.isfile(source_path):
                        archive.write(source_path, arcname=item_name)
                        continue
                    if not os.path.isdir(source_path):
                        continue
                    for dirpath, _dirnames, filenames in os.walk(source_path):
                        rel_dir = os.path.relpath(dirpath, source_profile_root)
                        for filename in filenames:
                            source_file = os.path.join(dirpath, filename)
                            arcname = os.path.join(rel_dir, filename)
                            archive.write(source_file, arcname=arcname)

            metadata = {
                "kind": "root",
                "generated_at": now_text(),
                "source_root": source_profile_root,
                "included_items": list(self._iter_root_items(source_profile_root)),
                **_archive_stats(temp_zip_path),
            }
            _atomic_replace(temp_zip_path, self.root_snapshot_path())
            write_json_atomic(self.root_metadata_path(), metadata)
            self._log(logger, f"mirror root snapshot ready: {self.root_snapshot_path()}")
            return metadata
        finally:
            if os.path.exists(temp_zip_path):
                try:
                    os.remove(temp_zip_path)
                except OSError:
                    pass

    def _zip_profile_snapshot(self, profile_name: str, logger: Optional[Callable[[str], None]] = None) -> Dict:
        profile_dir = get_profile_directory_path(self.config, profile_name)
        if not os.path.isdir(profile_dir):
            raise FileNotFoundError(f"profile directory not found: {profile_dir}")

        os.makedirs(self.profile_snapshot_dir(), exist_ok=True)
        fd, temp_zip_path = tempfile.mkstemp(
            prefix=f"chromium-profile-snapshot-{_slugify(profile_name)}-",
            suffix=".zip",
            dir=self.profile_snapshot_dir(),
        )
        os.close(fd)
        try:
            with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for dirpath, dirnames, filenames in os.walk(profile_dir):
                    dirnames[:] = [
                        name
                        for name in dirnames
                        if name not in SPLIT_PROFILE_CACHE_DIRS and name not in SPLIT_USER_DATA_ROOT_EXCLUDE_DIRS
                    ]
                    rel_dir = os.path.relpath(dirpath, profile_dir)
                    for filename in filenames:
                        if filename in SPLIT_PROFILE_EXCLUDE_FILES or filename in SPLIT_USER_DATA_ROOT_EXCLUDE_FILES:
                            continue
                        source_file = os.path.join(dirpath, filename)
                        arcname = os.path.join(rel_dir, filename) if rel_dir != "." else filename
                        archive.write(source_file, arcname=arcname)

            source_stats = _walk_stats(profile_dir)
            metadata = {
                "kind": "profile",
                "profile_name": profile_name,
                "generated_at": now_text(),
                "source_profile_dir": profile_dir,
                "excluded_cache_dirs": sorted(SPLIT_PROFILE_CACHE_DIRS),
                "excluded_files": sorted(set(SPLIT_PROFILE_EXCLUDE_FILES) | {PROFILE_RUNTIME_LOCK_FILENAME}),
                **source_stats,
                **_archive_stats(temp_zip_path),
            }
            _atomic_replace(temp_zip_path, self.profile_snapshot_path(profile_name))
            write_json_atomic(self.profile_metadata_path(profile_name), metadata)
            self._log(logger, f"mirror profile snapshot ready: profile={profile_name}")
            return metadata
        finally:
            if os.path.exists(temp_zip_path):
                try:
                    os.remove(temp_zip_path)
                except OSError:
                    pass

    def refresh_snapshots(
        self,
        profile_names: Optional[Sequence[str]] = None,
        logger: Optional[Callable[[str], None]] = None,
    ) -> Dict:
        started_at = now_text()
        if not self.is_enabled():
            return {
                "status": "disabled",
                "message": "mirror snapshots are disabled",
                "started_at": started_at,
                "finished_at": started_at,
                "profiles": [],
            }

        profile_names = list(profile_names or [item.get("profile_name", "") for item in self.config.get("profiles", []) if item.get("profile_name")])
        root_metadata = {}
        profile_results: List[Dict] = []
        try:
            root_metadata = self._zip_root_snapshot(logger=logger)
            for profile_name in profile_names:
                try:
                    metadata = self._zip_profile_snapshot(profile_name, logger=logger)
                    profile_results.append({"profile_name": profile_name, "status": "success", "metadata": metadata})
                except Exception as exc:
                    profile_results.append({"profile_name": profile_name, "status": "failed", "message": str(exc)})
            failed = [item for item in profile_results if item.get("status") != "success"]
            status = "success" if not failed else ("failed" if len(failed) == len(profile_results) else "partial")
            message = (
                "mirror snapshots updated"
                if status == "success"
                else ("mirror snapshots partially updated" if status == "partial" else "mirror snapshots failed")
            )
            finished_at = now_text()
            manifest = {
                "generated_at": finished_at,
                "started_at": started_at,
                "status": status,
                "message": message,
                "root": root_metadata,
                "profiles": {item.get("profile_name", ""): item for item in profile_results if item.get("profile_name")},
            }
            write_json_atomic(self.manifest_path(), manifest)
            return {
                "status": status,
                "message": message,
                "started_at": started_at,
                "finished_at": finished_at,
                "root": root_metadata,
                "profiles": profile_results,
            }
        except Exception as exc:
            finished_at = now_text()
            manifest = {
                "generated_at": finished_at,
                "started_at": started_at,
                "status": "failed",
                "message": str(exc),
                "root": root_metadata,
                "profiles": {item.get("profile_name", ""): item for item in profile_results if item.get("profile_name")},
            }
            write_json_atomic(self.manifest_path(), manifest)
            raise

    def validate_profile_snapshot(self, profile_name: str) -> Dict:
        root_metadata = self.load_root_metadata()
        profile_metadata = self.load_profile_metadata(profile_name)
        root_path = self.root_snapshot_path()
        profile_path = self.profile_snapshot_path(profile_name)
        root_available = os.path.exists(root_path) and bool(root_metadata)
        profile_available = os.path.exists(profile_path) and bool(profile_metadata)
        generated_at = str(profile_metadata.get("generated_at", "") or root_metadata.get("generated_at", "") or "")
        return {
            "profile_name": profile_name,
            "available": bool(profile_available),
            "root_available": bool(root_available),
            "profile_available": bool(profile_available),
            "generated_at": generated_at,
            "root_generated_at": str(root_metadata.get("generated_at", "") or ""),
            "profile_generated_at": str(profile_metadata.get("generated_at", "") or ""),
            "root_snapshot_path": root_path if root_available else "",
            "profile_snapshot_path": profile_path if profile_available else "",
            "root_metadata": root_metadata,
            "profile_metadata": profile_metadata,
        }

    def materialize_runtime(self, profile_name: str) -> MirrorRuntimeInfo:
        validation = self.validate_profile_snapshot(profile_name)
        if not validation.get("profile_available"):
            raise RuntimeError(f"mirror snapshot unavailable for profile: {profile_name}")

        runtime_id = f"{_slugify(profile_name)}-{uuid.uuid4().hex[:10]}"
        runtime_root = os.path.join(self.runtime_root(), runtime_id)
        runtime_profile_dir = os.path.join(runtime_root, profile_name)
        os.makedirs(runtime_root, exist_ok=True)
        try:
            if validation.get("root_snapshot_path"):
                with zipfile.ZipFile(validation["root_snapshot_path"], "r") as root_archive:
                    root_archive.extractall(runtime_root)
            os.makedirs(runtime_profile_dir, exist_ok=True)
            with zipfile.ZipFile(validation["profile_snapshot_path"], "r") as profile_archive:
                profile_archive.extractall(runtime_profile_dir)
            runtime_meta = {
                "runtime_id": runtime_id,
                "profile_name": profile_name,
                "runtime_root": runtime_root,
                "generated_at": now_text(),
                "root_snapshot_generated_at": validation.get("root_generated_at", ""),
                "profile_snapshot_generated_at": validation.get("profile_generated_at", ""),
            }
            write_json_atomic(os.path.join(runtime_root, RUNTIME_METADATA_FILENAME), runtime_meta)
            return MirrorRuntimeInfo(
                profile_name=profile_name,
                runtime_root=runtime_root,
                runtime_profile_dir=runtime_profile_dir,
                runtime_id=runtime_id,
                generated_at=runtime_meta["generated_at"],
                root_snapshot_generated_at=runtime_meta["root_snapshot_generated_at"],
                profile_snapshot_generated_at=runtime_meta["profile_snapshot_generated_at"],
            )
        except Exception:
            shutil.rmtree(runtime_root, ignore_errors=True)
            raise

    def cleanup_runtime(self, runtime_root: str) -> None:
        if runtime_root and os.path.isdir(runtime_root):
            shutil.rmtree(runtime_root, ignore_errors=True)

    def cleanup_stale_runtimes(self) -> Dict:
        runtime_base = self.runtime_root()
        max_age_hours = max(1, int(self.mirror_settings.get("max_runtime_age_hours", 24)))
        cutoff = time.time() - (max_age_hours * 3600)
        removed: List[str] = []
        if not os.path.isdir(runtime_base):
            return {"removed": removed}
        for name in os.listdir(runtime_base):
            full_path = os.path.join(runtime_base, name)
            if not os.path.isdir(full_path):
                continue
            try:
                modified_at = os.path.getmtime(full_path)
            except OSError:
                modified_at = 0
            if modified_at > cutoff:
                continue
            shutil.rmtree(full_path, ignore_errors=True)
            removed.append(full_path)
        return {"removed": removed}
