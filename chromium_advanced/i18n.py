import json
import os
from typing import Dict, List, Tuple


def _resource_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "resources", "i18n"))


def _read_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def load_language_options() -> List[Tuple[str, str]]:
    config = _read_json(os.path.join(_resource_root(), "languages.json"))
    items = config.get("languages", [])
    results: List[Tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip().lower()
        label = str(item.get("label", "")).strip()
        if code and label:
            results.append((code, label))
    return results or [("en", "English")]


def load_translations() -> Dict[str, Dict[str, str]]:
    translations: Dict[str, Dict[str, str]] = {}
    for code, _label in load_language_options():
        path = os.path.join(_resource_root(), f"{code}.json")
        if not os.path.exists(path):
            continue
        loaded = _read_json(path)
        translations[code] = {str(key): str(value) for key, value in loaded.items()}
    return translations
