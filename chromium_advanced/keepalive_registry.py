import copy
import glob
import hashlib
import importlib.util
import inspect
import os
import re
import tempfile
import textwrap
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from selenium.webdriver.common.keys import Keys


def _lib():
    from chromium_advanced import chromium_profile_lib as lib

    return lib


def get_keepalive_plugin_root() -> str:
    path = os.path.join(_lib().get_state_storage_dir(), "keepalive_plugins")
    os.makedirs(path, exist_ok=True)
    return path


def get_keepalive_icon_cache_dir() -> str:
    path = os.path.join(_lib().get_state_storage_dir(), "keepalive_site_icons")
    os.makedirs(path, exist_ok=True)
    return path


def safe_copy(value):
    return copy.deepcopy(value)


def normalize_site_id(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text)
    return text.strip("._-")


def _iter_keepalive_plugin_dirs(config: Optional[Dict] = None) -> List[str]:
    configured = []
    if isinstance(config, dict):
        keepalive = config.get("keepalive", {})
        if isinstance(keepalive, dict) and isinstance(keepalive.get("plugin_dirs"), list):
            configured.extend(str(item) for item in keepalive.get("plugin_dirs", []) if str(item or "").strip())
    return _lib().unique_paths([get_keepalive_plugin_root(), *configured])


def _get_keepalive_plugin_signature(config: Optional[Dict] = None) -> List:
    signature = []
    for plugin_dir in _iter_keepalive_plugin_dirs(config):
        if not os.path.isdir(plugin_dir):
            signature.append((plugin_dir, "missing"))
            continue
        files = []
        for path in sorted(glob.glob(os.path.join(plugin_dir, "*.py"))):
            if os.path.basename(path).startswith("_"):
                continue
            try:
                stat = os.stat(path)
                files.append((path, int(stat.st_mtime_ns), int(stat.st_size)))
            except OSError:
                files.append((path, "unreadable"))
        signature.append((plugin_dir, tuple(files)))
    return signature


def _normalize_keepalive_plugin_payload(raw: Dict, source: str = "") -> Dict:
    payload = dict(raw) if isinstance(raw, dict) else {}
    site_id = normalize_site_id(payload.get("site_id") or payload.get("id") or payload.get("name"))
    if not site_id:
        return {}
    home_url = str(payload.get("home_url", "") or "").strip()
    icon_url = str(payload.get("icon_url", "") or "").strip()
    if not icon_url and home_url:
        parsed = urlparse(home_url)
        if parsed.scheme and parsed.netloc:
            icon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    return {
        "site_id": site_id,
        "display_name": str(payload.get("display_name") or payload.get("label") or site_id.title()).strip(),
        "home_url": home_url,
        "icon_url": icon_url,
        "builtin": bool(payload.get("builtin", False)),
        "source": source,
        "module_name": str(payload.get("module_name", "") or "").strip(),
        "function_name": str(payload.get("function_name", "keepalive") or "keepalive").strip(),
        "class_name": str(payload.get("class_name", "") or "").strip(),
        "plugin_type": str(payload.get("plugin_type", "") or "").strip(),
        "load_error": str(payload.get("load_error", "") or "").strip(),
    }


def _extract_keepalive_plugin_metadata_from_module(module, path: str, module_name: str) -> Dict:
    raw = None
    if hasattr(module, "get_plugin"):
        raw = module.get_plugin()
    elif hasattr(module, "SITE_PLUGIN"):
        raw = module.SITE_PLUGIN
    elif hasattr(module, "PLUGIN"):
        raw = module.PLUGIN
    elif hasattr(module, "KeepalivePlugin"):
        plugin_class = getattr(module, "KeepalivePlugin")
        if inspect.isclass(plugin_class):
            instance = plugin_class()
            if hasattr(instance, "get_plugin"):
                raw = instance.get_plugin()
            else:
                raw = getattr(instance, "metadata", None)
            if isinstance(raw, dict):
                raw = dict(raw)
                raw.setdefault("class_name", "KeepalivePlugin")
                raw.setdefault("function_name", "keepalive")
                raw.setdefault("plugin_type", "class")
    if not isinstance(raw, dict):
        return {}
    raw = dict(raw)
    raw["source"] = path
    raw["module_name"] = module_name
    return _normalize_keepalive_plugin_payload(raw, source=path)


def discover_external_keepalive_site_metadata(config: Optional[Dict] = None) -> Dict[str, Dict]:
    cache = _lib()._KEEPALIVE_PLUGIN_METADATA_CACHE
    signature = _get_keepalive_plugin_signature(config)
    if cache["signature"] == signature:
        return {site_id: dict(meta) for site_id, meta in cache["metadata"].items()}

    discovered: Dict[str, Dict] = {}
    for plugin_dir in _iter_keepalive_plugin_dirs(config):
        if not os.path.isdir(plugin_dir):
            continue
        for path in sorted(glob.glob(os.path.join(plugin_dir, "*.py"))):
            if os.path.basename(path).startswith("_"):
                continue
            module_name = f"chromium_advanced_user_keepalive_{hashlib.sha1(path.encode('utf-8')).hexdigest()[:12]}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                metadata = _extract_keepalive_plugin_metadata_from_module(module, path, module_name)
                if metadata:
                    discovered[metadata["site_id"]] = metadata
            except Exception as exc:
                site_id = normalize_site_id(os.path.splitext(os.path.basename(path))[0])
                if site_id:
                    discovered[site_id] = _normalize_keepalive_plugin_payload(
                        {
                            "site_id": site_id,
                            "display_name": site_id.replace("_", " ").title(),
                            "source": path,
                            "module_name": module_name,
                            "plugin_type": "external",
                            "load_error": str(exc),
                        },
                        source=path,
                    )
    cache["signature"] = signature
    cache["metadata"] = {site_id: dict(meta) for site_id, meta in discovered.items()}
    return discovered


def get_keepalive_site_registry(config: Optional[Dict] = None) -> Dict[str, Dict]:
    registry = {site_id: dict(meta) for site_id, meta in _lib().BUILTIN_KEEPALIVE_SITE_METADATA.items()}
    registry.update(discover_external_keepalive_site_metadata(config))
    if isinstance(config, dict):
        for profile in config.get("profiles", []):
            keepalive_sites = profile.get("keepalive_sites", {})
            if isinstance(keepalive_sites, dict):
                for site_id, enabled in keepalive_sites.items():
                    normalized = normalize_site_id(site_id)
                    if normalized and bool(enabled) and normalized not in registry:
                        registry[normalized] = {
                            "site_id": normalized,
                            "display_name": normalized.title(),
                            "home_url": "",
                            "icon_url": "",
                            "builtin": False,
                        }
            last_keepalive_details = profile.get("last_keepalive_details", {})
            if isinstance(last_keepalive_details, dict):
                for site_id in last_keepalive_details:
                    normalized = normalize_site_id(site_id)
                    if normalized and normalized not in registry:
                        registry[normalized] = {
                            "site_id": normalized,
                            "display_name": normalized.title(),
                            "home_url": "",
                            "icon_url": "",
                            "builtin": False,
                        }
    return registry


def get_keepalive_plugin_records(config: Optional[Dict] = None) -> List[Dict]:
    records = []
    for site_id in get_keepalive_site_ids(config):
        metadata = dict(get_keepalive_site_registry(config).get(site_id, {}))
        if not metadata:
            continue
        metadata.setdefault("site_id", site_id)
        metadata["plugin_type"] = "system" if metadata.get("builtin") else "external"
        metadata["editable"] = not metadata.get("builtin")
        if metadata.get("builtin") and not metadata.get("source"):
            action = _lib().BUILTIN_KEEPALIVE_SITE_ACTIONS.get(site_id)
            if action:
                metadata["source"] = f"builtin::{action.__name__}"
        records.append(metadata)
    return records


def get_keepalive_site_ids(config: Optional[Dict] = None) -> List[str]:
    registry = get_keepalive_site_registry(config)
    ordered = [site_id for site_id in _lib().KEEPALIVE_SITE_ORDER if site_id in registry]
    extras = sorted(site_id for site_id in registry if site_id not in ordered)
    return ordered + extras


def get_keepalive_site_label(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    return str(get_keepalive_site_registry(config).get(normalized, {}).get("display_name") or normalized.title())


def get_keepalive_plugin_root_for_site(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    registry = get_keepalive_site_registry(config)
    metadata = registry.get(normalized, {})
    source = str(metadata.get("source", "") or "").strip()
    if source:
        return source
    return os.path.join(get_keepalive_plugin_root(), f"{normalized}.py")


def get_keepalive_plugin_source_text(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    if normalized in _lib().BUILTIN_KEEPALIVE_SITE_ACTIONS:
        action = _lib().BUILTIN_KEEPALIVE_SITE_ACTIONS[normalized]
        try:
            return inspect.getsource(action).strip() + "\n"
        except (OSError, IOError, TypeError):
            return build_builtin_keepalive_plugin_reference_source(normalized)
    source_path = get_keepalive_plugin_root_for_site(normalized, config)
    if not source_path or not os.path.exists(source_path):
        raise FileNotFoundError(f"keepalive plugin source not found for {normalized}: {source_path}")
    with open(source_path, "r", encoding="utf-8") as handle:
        return handle.read()


def build_builtin_keepalive_plugin_reference_source(site_id: str) -> str:
    normalized = normalize_site_id(site_id)
    if normalized == "google":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "google",
                    "display_name": "Google",
                    "home_url": "https://www.google.com/",
                    "icon_url": "https://www.google.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    query = str(settings.get("google_query", "")).strip() or "profile keepalive"
                    browser.goto("https://www.google.com/")
                    browser.wait_ready()

                    if browser.exists("a[href*='ServiceLogin']", by="css", timeout=0):
                        return results.signed_out("Google is not signed in for this profile.")

                    browser.fill("textarea[name='q']", query, by="css", timeout=10)
                    browser.press(Keys.ENTER)
                    browser.wait_ready()
                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success(f"search results loaded for query: {query}")
            """
        )
    if normalized == "gmail":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "gmail",
                    "display_name": "Gmail",
                    "home_url": "https://mail.google.com/",
                    "icon_url": "https://mail.google.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    browser.goto("https://mail.google.com/")
                    browser.wait_ready()

                    current_url = browser.current_url().lower()
                    if "service=mail" in current_url or "accounts.google.com" in current_url:
                        return results.signed_out("Gmail is not signed in for this profile.")

                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success("gmail inbox loaded")
            """
        )
    if normalized == "github":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "github",
                    "display_name": "GitHub",
                    "home_url": "https://github.com/",
                    "icon_url": "https://github.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    browser.goto("https://github.com/")
                    browser.wait_ready()

                    user_login = browser.execute(
                        "const meta = document.querySelector('meta[name=\"user-login\"]'); return meta ? meta.content || '' : '';"
                    )
                    if not str(user_login or "").strip():
                        return results.signed_out("GitHub is not signed in for this profile.")

                    browser.goto("https://github.com/pulls")
                    browser.wait_ready()
                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success("pull requests page loaded")
            """
        )
    if normalized == "chatgpt":
        return textwrap.dedent(
            """\
            class KeepalivePlugin:
                metadata = {
                    "site_id": "chatgpt",
                    "display_name": "ChatGPT",
                    "home_url": "https://chatgpt.com/",
                    "icon_url": "https://chatgpt.com/favicon.ico",
                }

                def keepalive(self, context):
                    browser = context["browser"]
                    results = context["results"]
                    settings = context["settings"]

                    browser.goto("https://chatgpt.com/")
                    browser.wait_ready()

                    current_url = browser.current_url().lower()
                    if "auth" in current_url or "login" in current_url:
                        return results.signed_out("ChatGPT is not signed in for this profile.")

                    prompt = str(settings.get("chatgpt_prompt", "")).strip() or "Reply with one word: alive"
                    browser.fill("#prompt-textarea", prompt, by="css", timeout=15)
                    browser.press(Keys.ENTER)
                    browser.sleep(int(settings.get("site_dwell_seconds", 6)))
                    return results.success("prompt sent and reply flow observed")
            """
        )
    return build_keepalive_plugin_template(normalized, normalized.replace("_", " ").title(), f"https://example.com/{normalized}")


def build_keepalive_plugin_template(site_id: str, display_name: str = "", home_url: str = "") -> str:
    normalized = normalize_site_id(site_id) or "example_site"
    label = str(display_name or normalized.replace("_", " ").title()).strip()
    home = str(home_url or f"https://example.com/{normalized}").strip()
    parsed = urlparse(home)
    icon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico" if parsed.scheme and parsed.netloc else ""
    return textwrap.dedent(
        f"""\
        class KeepalivePlugin:
            metadata = {{
                "site_id": "{normalized}",
                "display_name": "{label}",
                "home_url": "{home}",
                "icon_url": "{icon_url}",
            }}

            def get_plugin(self):
                return dict(self.metadata)

            def keepalive(self, context):
                browser = context["browser"]
                results = context["results"]
                log = context["log"]

                browser.goto("{home}")
                browser.wait_ready()

                if "login" in browser.current_url().lower():
                    return results.signed_out("{label} is not signed in for this profile.")

                log("page opened")
                return results.success("{label} page opened")
        """
    )


def inspect_keepalive_plugin_source(site_id: str, source_text: str) -> Dict:
    normalized = normalize_site_id(site_id)
    if not normalized:
        raise ValueError("plugin site_id is required")
    source_text = str(source_text or "")
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8", newline="\n") as handle:
        handle.write(source_text)
        temp_path = handle.name
    module_name = f"chromium_advanced_preview_keepalive_{hashlib.sha1((normalized + source_text).encode('utf-8')).hexdigest()[:12]}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, temp_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("failed to load keepalive plugin spec")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        metadata = _extract_keepalive_plugin_metadata_from_module(module, temp_path, module_name)
        if not metadata:
            raise RuntimeError("keepalive plugin does not expose metadata")
        return metadata
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def save_keepalive_plugin_source(site_id: str, source_text: str, config: Optional[Dict] = None) -> Dict:
    normalized = normalize_site_id(site_id)
    if not normalized:
        raise ValueError("plugin site_id is required")
    registry = get_keepalive_site_registry(config)
    if registry.get(normalized, {}).get("builtin"):
        raise ValueError(f"builtin keepalive plugin '{normalized}' is read-only")
    metadata = inspect_keepalive_plugin_source(normalized, source_text)
    resolved_site_id = normalize_site_id(metadata.get("site_id", "")) or normalized
    if registry.get(resolved_site_id, {}).get("builtin"):
        raise ValueError(f"builtin keepalive plugin '{resolved_site_id}' is read-only")
    source_path = get_keepalive_plugin_root_for_site(normalized, config)
    target_path = get_keepalive_plugin_root_for_site(resolved_site_id, config)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source_text)
    source_path = os.path.normcase(os.path.abspath(source_path)) if source_path else ""
    target_path_norm = os.path.normcase(os.path.abspath(target_path))
    if source_path and source_path != target_path_norm and os.path.exists(source_path):
        try:
            os.remove(source_path)
        except OSError:
            pass
    return {
        "path": target_path,
        "site_id": resolved_site_id,
        "display_name": str(metadata.get("display_name", "") or resolved_site_id),
        "home_url": str(metadata.get("home_url", "") or ""),
        "icon_url": str(metadata.get("icon_url", "") or ""),
        "previous_site_id": normalized,
    }


def delete_keepalive_plugin_source(site_id: str, config: Optional[Dict] = None) -> str:
    normalized = normalize_site_id(site_id)
    registry = get_keepalive_site_registry(config)
    if registry.get(normalized, {}).get("builtin"):
        raise ValueError(f"builtin keepalive plugin '{normalized}' cannot be deleted")
    target_path = get_keepalive_plugin_root_for_site(normalized, config)
    if not target_path or not os.path.exists(target_path):
        raise FileNotFoundError(f"keepalive plugin source not found for {normalized}: {target_path}")
    os.remove(target_path)
    return target_path


def migrate_keepalive_site_id_references(config: Dict, old_site_id: str, new_site_id: str) -> Tuple[Dict, bool]:
    normalized_old = normalize_site_id(old_site_id)
    normalized_new = normalize_site_id(new_site_id)
    if not normalized_old or not normalized_new or normalized_old == normalized_new:
        return config, False

    payload = dict(config) if isinstance(config, dict) else {}
    changed = False

    keepalive = payload.get("keepalive", {})
    if isinstance(keepalive, dict):
        enabled_sites = keepalive.get("enabled_sites")
        if isinstance(enabled_sites, dict) and normalized_old in enabled_sites:
            old_value = bool(enabled_sites.pop(normalized_old))
            enabled_sites[normalized_new] = bool(enabled_sites.get(normalized_new, False) or old_value)
            changed = True

    profiles = payload.get("profiles", [])
    if isinstance(profiles, list):
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            keepalive_sites = profile.get("keepalive_sites")
            if isinstance(keepalive_sites, dict) and normalized_old in keepalive_sites:
                old_value = bool(keepalive_sites.pop(normalized_old))
                if old_value or normalized_new in keepalive_sites:
                    keepalive_sites[normalized_new] = bool(keepalive_sites.get(normalized_new, False) or old_value)
                changed = True
            last_keepalive_details = profile.get("last_keepalive_details")
            if isinstance(last_keepalive_details, dict) and normalized_old in last_keepalive_details:
                old_detail = last_keepalive_details.pop(normalized_old)
                last_keepalive_details.setdefault(normalized_new, old_detail)
                changed = True
    return payload, changed


def get_keepalive_site_icon_path(site_id: str, config: Optional[Dict] = None, fetch: bool = True) -> str:
    normalized = normalize_site_id(site_id)
    if not normalized:
        return ""
    registry = get_keepalive_site_registry(config)
    metadata = registry.get(normalized, {})
    icon_url = str(metadata.get("icon_url", "") or "").strip()
    if not icon_url:
        return ""
    cache_dir = get_keepalive_icon_cache_dir()
    extension = os.path.splitext(urlparse(icon_url).path)[1].lower()
    if extension not in {".ico", ".png", ".jpg", ".jpeg", ".webp"}:
        extension = ".ico"
    target = os.path.join(cache_dir, f"{normalized}-{hashlib.sha1(icon_url.encode('utf-8')).hexdigest()[:12]}{extension}")
    if os.path.exists(target) or not fetch:
        return target if os.path.exists(target) else ""
    try:
        request = Request(icon_url, headers={"User-Agent": f"{_lib().APP_NAME}/1.0"})
        with urlopen(request, timeout=5) as response:
            data = response.read(512 * 1024)
        if data:
            with open(target, "wb") as handle:
                handle.write(data)
            return target
    except Exception:
        return ""
    return ""


def warm_keepalive_site_icon_cache(config: Optional[Dict] = None) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for site_id in get_keepalive_site_ids(config):
        path = get_keepalive_site_icon_path(site_id, config, fetch=True)
        if path:
            results[site_id] = path
    return results


def normalize_keepalive_site_flags(value, default: bool = False, site_ids: Optional[Sequence[str]] = None) -> Dict[str, bool]:
    ordered_site_ids = list(site_ids or [])
    flags = {site_name: bool(default) for site_name in ordered_site_ids}
    if isinstance(value, dict):
        for raw_site_name, enabled in value.items():
            site_name = normalize_site_id(raw_site_name)
            if site_name:
                flags[site_name] = bool(enabled)
    return flags


def format_keepalive_sites_text(site_flags: Dict, translate: Optional[Callable[[str, str], str]] = None) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    labels = []
    normalized = normalize_keepalive_site_flags(site_flags, default=False)
    for site_name in get_keepalive_site_ids({"profiles": [{"keepalive_sites": normalized}]}):
        if normalized.get(site_name):
            labels.append(tr(f"site_name_{site_name}", get_keepalive_site_label(site_name)))
    return ", ".join(labels) if labels else "-"


def normalize_keepalive_site_result_for_display(info: Dict) -> Dict:
    payload = dict(info) if isinstance(info, dict) else {}
    status = str(payload.get("status", "") or "").strip().lower()
    message = str(payload.get("message", "") or "")
    if status == "failed" and _lib().is_browser_closed_error(RuntimeError(message)):
        payload["status"] = "attention"
        payload.setdefault("signed_in", None)
    return payload


def normalize_keepalive_action_result(site_name: str, result: Dict) -> Dict:
    payload = dict(result) if isinstance(result, dict) else {}
    status = str(payload.get("status", "") or "").strip().lower()
    if status not in {"success", "signed_out", "attention", "failed", "skipped"}:
        status = "success" if status in {"", "ok"} else "attention"
    message = str(payload.get("message", "") or "").strip()
    if not message:
        message = f"{site_name} {status}"
    signed_in = payload.get("signed_in")
    if status == "success":
        signed_in = True if signed_in is None else bool(signed_in)
    elif status == "signed_out":
        signed_in = False
    elif status in {"attention", "failed"} and signed_in is None:
        signed_in = None
    payload.update({"status": status, "message": message, "signed_in": signed_in})
    return payload


def format_keepalive_site_status(
    site_name: str,
    info: Dict,
    translate: Optional[Callable[[str, str], str]] = None,
) -> str:
    tr = translate or (lambda key, fallback="": fallback or key)
    payload = normalize_keepalive_site_result_for_display(info)
    status = str(payload.get("status", "unknown") or "unknown").strip().lower()
    message = str(payload.get("message", "") or "").strip()
    site_label = tr(f"site_name_{site_name}", get_keepalive_site_label(site_name))
    status_label = {
        "success": tr("keepalive_site_status_success", "ok"),
        "signed_out": tr("keepalive_site_status_signed_out", "signed out"),
        "attention": tr("keepalive_site_status_attention", "attention"),
        "failed": tr("keepalive_site_status_failed", "failed"),
        "unknown": tr("keepalive_site_status_unknown", "unknown"),
    }.get(status, status)
    base = f"{site_label}: {status_label}"
    return f"{base} - {message}" if message else base
