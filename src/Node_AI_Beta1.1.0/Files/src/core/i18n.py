from __future__ import annotations

import json
import re
from pathlib import Path

from src.core.library import resource_path

LANGS: dict[str, str] = {"es": "Español", "en": "English", "pt": "Português", "fr": "Français", "de": "Deutsch"}
DEFAULT_LANG = "es"
MAX_FILE_BYTES = 400_000
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

_current = DEFAULT_LANG
_tables: dict[str, dict[str, str]] = {}


def lang_file(code: str) -> Path:
    return resource_path("src", "data", "idiomas", f"{code}.json")


def load_table(code: str, path: Path | None = None) -> dict[str, str]:
    path = path or lang_file(code)
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}


def set_language(code: str) -> str:
    global _current
    _current = code if code in LANGS else DEFAULT_LANG
    if _current != DEFAULT_LANG and _current not in _tables:
        _tables[_current] = load_table(_current)
    return _current


def t(text: str, **values: object) -> str:
    out = _tables.get(_current, {}).get(text, text) if _current != DEFAULT_LANG else text
    if values:
        out = _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), out)
    return out
