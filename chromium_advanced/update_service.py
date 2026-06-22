from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple


DEFAULT_UPDATE_CHANNEL = "stable"
SUPPORTED_UPDATE_CHANNELS = {"stable", "rc", "beta", "dev"}


def build_default_update_config() -> Dict:
    return {
        "enabled": True,
        "check_on_startup": True,
        "channel": DEFAULT_UPDATE_CHANNEL,
        "feed_url": "",
        "check_interval_hours": 12,
        "last_checked_at": "",
        "last_status": "idle",
        "last_error": "",
        "last_available_version": "",
        "last_notes_url": "",
        "skipped_version": "",
    }


def normalize_update_channel(value: str) -> str:
    channel = str(value or "").strip().lower()
    if channel in SUPPORTED_UPDATE_CHANNELS:
        return channel
    return DEFAULT_UPDATE_CHANNEL


def parse_version_parts(version_text: str) -> Tuple[Tuple[int, ...], Tuple[str, int]]:
    text = str(version_text or "").strip().lower().lstrip("v")
    if not text:
        return (0,), ("stable", 0)
    main_part = text
    prerelease_label = "stable"
    prerelease_number = 0
    if "-" in text:
        main_part, suffix = text.split("-", 1)
        suffix = suffix.strip()
        if "." in suffix:
            label, number_text = suffix.split(".", 1)
        else:
            label, number_text = suffix, "0"
        prerelease_label = label.strip() or "stable"
        try:
            prerelease_number = int(number_text.strip() or "0")
        except ValueError:
            prerelease_number = 0
    parts = []
    for item in main_part.split("."):
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    rank_map = {
        "dev": 0,
        "beta": 1,
        "rc": 2,
        "stable": 3,
    }
    return tuple(parts or [0]), (prerelease_label, rank_map.get(prerelease_label, 0) * 100000 + prerelease_number)


def compare_versions(current_version: str, candidate_version: str) -> int:
    current_main, current_pre = parse_version_parts(current_version)
    candidate_main, candidate_pre = parse_version_parts(candidate_version)
    if candidate_main > current_main:
        return 1
    if candidate_main < current_main:
        return -1
    if candidate_pre[1] > current_pre[1]:
        return 1
    if candidate_pre[1] < current_pre[1]:
        return -1
    return 0


def fetch_update_manifest(feed_url: str) -> Dict:
    url = str(feed_url or "").strip()
    if not url:
        raise RuntimeError("empty update feed url")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "mcp-chromium-advanced-update-checker",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"update feed http error: {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"update feed network error: {exc.reason}") from exc


def should_check_now(update_config: Dict, now: datetime | None = None) -> bool:
    config = dict(build_default_update_config())
    if isinstance(update_config, dict):
        config.update(update_config)
    if not bool(config.get("enabled", True)):
        return False
    last_checked_at = str(config.get("last_checked_at", "") or "").strip()
    if not last_checked_at:
        return True
    current_time = now or datetime.now(timezone.utc)
    try:
        checked_at = datetime.fromisoformat(last_checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    try:
        interval_hours = max(1, int(config.get("check_interval_hours", 12) or 12))
    except Exception:
        interval_hours = 12
    return current_time >= checked_at + timedelta(hours=interval_hours)


def check_for_update(*, current_version: str, update_config: Dict) -> Dict:
    config = dict(build_default_update_config())
    if isinstance(update_config, dict):
        config.update(update_config)
    channel = normalize_update_channel(config.get("channel", DEFAULT_UPDATE_CHANNEL))
    feed_url = str(config.get("feed_url", "") or "").strip()
    if not bool(config.get("enabled", True)):
        return {
            "ok": False,
            "status": "disabled",
            "message": "update checks disabled",
            "channel": channel,
            "feed_url": feed_url,
        }
    if not feed_url:
        return {
            "ok": False,
            "status": "missing_feed",
            "message": "update feed url not configured",
            "channel": channel,
            "feed_url": feed_url,
        }
    manifest = fetch_update_manifest(feed_url)
    version_text = str(manifest.get("version", "") or "").strip()
    notes_url = str(manifest.get("notes_url", "") or "").strip()
    mandatory = bool(manifest.get("mandatory", False))
    skipped_version = str(config.get("skipped_version", "") or "").strip()
    if not version_text:
        raise RuntimeError("update manifest missing version")
    comparison = compare_versions(str(current_version or ""), version_text)
    available = comparison < 0
    skipped = bool(skipped_version) and skipped_version == version_text
    return {
        "ok": True,
        "status": "update_available" if available else "up_to_date",
        "message": "update available" if available else "already up to date",
        "channel": channel,
        "feed_url": feed_url,
        "version": version_text,
        "notes_url": notes_url,
        "mandatory": mandatory,
        "available": available,
        "skipped": skipped,
        "manifest": manifest,
    }
