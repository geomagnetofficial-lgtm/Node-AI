from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from src.core import i18n
from src.core.i18n import LANGS, load_table, set_language, t
from src.core.library import Library

ROOT = Path(__file__).resolve().parents[1]
UI_FILES = ["src/ui.py", "src/ui_agent.py", "src/ui_library.py", "src/ui_models.py"]
OTHER = [c for c in LANGS if c != "es"]


def used_keys() -> set[str]:
    keys: set[str] = set()
    for f in UI_FILES:
        for n in ast.walk(ast.parse((ROOT / f).read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t" and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    keys.add(a.value)
    from src.ui import CONFIG_TABS
    from src.ui_agent import APPROVAL_OPTIONS, REASONS
    keys |= {v[0] for v in REASONS.values()} | {v for _, v in APPROVAL_OPTIONS} | {label for _, _, label in CONFIG_TABS}
    keys.add("no encontrado")
    lib = Library()
    for r in lib.default_roles.values():
        keys.add(r.nombre)
    for p in lib.default_prompts.values():
        keys |= {p.titulo, p.categoria} - {""}
    return keys


def test_no_fstrings_inside_t_calls():
    for f in UI_FILES:
        for n in ast.walk(ast.parse((ROOT / f).read_text(encoding="utf-8"))):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t" and n.args:
                assert not isinstance(n.args[0], ast.JoinedStr), f"{f}:{n.lineno} usa f-string en t()"


@pytest.mark.parametrize("code", OTHER)
def test_every_language_translates_every_key(code):
    table = load_table(code)
    keys = used_keys()
    assert len(keys) > 200
    assert sorted(keys - set(table)) == [], f"{code}: faltan traducciones"
    assert sorted(set(table) - keys) == [], f"{code}: claves que ya no se usan"


@pytest.mark.parametrize("code", OTHER)
def test_placeholders_match_the_spanish_source(code):
    ph = lambda s: sorted(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for key, value in load_table(code).items():
        assert ph(key) == ph(value), f"{code}: {key!r}"
        assert value.strip()


@pytest.mark.parametrize("code", OTHER)
def test_translations_actually_differ_for_most_strings(code):
    table = load_table(code)
    same = [k for k, v in table.items() if k == v]
    assert len(same) < len(table) * 0.25


def test_switching_language_and_fallbacks():
    assert set_language("en") == "en" and t("Enviar") == "Send"
    assert set_language("de") == "de" and t("Enviar") == "Senden"
    assert t("texto sin traducir") == "texto sin traducir"
    assert set_language("klingon") == "es" and t("Enviar") == "Enviar"


def test_placeholder_substitution_is_safe():
    set_language("en")
    assert t("Añadir IA ({n}/{max})", n=3, max=20) == "Add AI (3/20)"
    assert t("Hola {x} {y.__class__} {0}", x=1) == "Hola 1 {y.__class__} {0}"
    assert t("Estado {faltante}") == "Estado {faltante}"


def test_language_files_are_untrusted_data(tmp_path):
    f = tmp_path / "x.json"
    f.write_text(json.dumps({"a": "b", "c": 5, "d": ["x"], "e": "  "}), encoding="utf-8")
    assert load_table("x", f) == {"a": "b"}
    f.write_text("[1,2]", encoding="utf-8")
    assert load_table("x", f) == {}
    f.write_text("no json", encoding="utf-8")
    assert load_table("x", f) == {}
    f.write_text(json.dumps({"k": "v" * 500_000}), encoding="utf-8")
    assert load_table("x", f) == {}
    assert load_table("x", tmp_path / "nada.json") == {}


def test_language_files_are_bundled_in_spec_data():
    assert all((ROOT / "src/data/idiomas" / f"{c}.json").is_file() for c in OTHER)
    assert "src/data" in (ROOT / "NodeAi.spec").read_text(encoding="utf-8")
    assert i18n.lang_file("en").is_file()
