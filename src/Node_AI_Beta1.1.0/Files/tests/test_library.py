from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from src.core import library as lib_mod
from src.core.library import (
    DEFAULTS_PATH,
    LANG,
    MAX_FILE_BYTES,
    Library,
    get_library,
    set_library,
)
from src.core.orchestrator import Orchestrator
from src.core.settings import AppSettings, ModelSlot

ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, data) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def defaults_file(tmp_path, version=1, extra_prompts=(), extra_roles=()):
    data = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    data["version"] = version
    data["prompts"] += list(extra_prompts)
    data["roles"] += list(extra_roles)
    return write(tmp_path / "defaults.json", data)


@pytest.fixture(autouse=True)
def _reset_active():
    set_library(None)
    yield
    set_library(None)


def test_shipped_defaults_are_valid_and_included_in_spec():
    lib = Library()
    assert DEFAULTS_PATH.is_file() and lib.defaults_version == 1 and lib.warnings == []
    assert {"debater", "critic", "moderator", "neutral", "director"} <= set(lib.default_roles)
    assert len(lib.default_prompts) >= 8
    assert all(not i.startswith("usr-") for i in [*lib.default_roles, *lib.default_prompts])
    assert all(p.rol in lib.default_roles for p in lib.default_prompts.values() if p.rol)
    assert "src/data" in (ROOT / "NodeAi.spec").read_text(encoding="utf-8")


def test_default_role_prompts_keep_original_behavior():
    from src.core.roles import DIRECTOR, ROLES, director_prompt, role_prompt
    assert set(ROLES) == {"debater", "critic", "moderator", "neutral"}
    assert role_prompt("critic") == LANG + ROLES["critic"][1]
    assert role_prompt("no-existe") == LANG + ROLES["neutral"][1]
    assert director_prompt() == LANG + DIRECTOR and "Director de NodeAi" in DIRECTOR


def test_missing_defaults_file_falls_back_with_warning(tmp_path):
    lib = Library(defaults_path=tmp_path / "nada.json")
    assert any("prompts_default.json" in w for w in lib.warnings)
    assert "neutral" in lib.default_roles and "director" in lib.default_roles


def test_invalid_entries_are_ignored_with_warnings_not_fatal(tmp_path):
    good_role = {"id": "usr-ok1", "nombre": "Poeta", "prompt_base": "Responde en verso."}
    user = write(tmp_path / "u.json", {"version": 1, "roles": [
        good_role,
        {"id": "sinprefijo", "nombre": "x", "prompt_base": "y"},
        {"id": "usr-a", "nombre": "", "prompt_base": "y"},
        {"id": "usr-b", "nombre": "n", "prompt_base": "y", "ambito": "otro"},
        {"id": "usr-ok1", "nombre": "Duplicado", "prompt_base": "z"},
        "texto suelto", 42, {"id": "../etc", "nombre": "n", "prompt_base": "p"},
    ], "prompts": [
        {"id": "usr-p1", "titulo": "Bueno", "texto": "hola", "rol": "usr-ok1", "etiquetas": ["a", "a", 5, "", "b"]},
        {"id": "usr-p2", "titulo": "Mal", "texto": "x", "etiquetas": "no-lista"},
        {"id": "usr-p3", "titulo": "Largo", "texto": "x" * 20_001},
        {"id": "usr-p4", "titulo": 7, "texto": "x"},
    ]})
    lib = Library(user)
    assert list(lib.user_roles) == ["usr-ok1"] and lib.user_roles["usr-ok1"].nombre == "Poeta"
    assert list(lib.user_prompts) == ["usr-p1"] and lib.user_prompts["usr-p1"].etiquetas == ["a", "b"]
    assert len(lib.warnings) >= 9
    assert len(lib.default_roles) == 5


@pytest.mark.parametrize("payload", ["[1,2,3]", "no es json", '{"version": "1"}', '{"version": 0}', ""])
def test_corrupt_user_file_is_ignored_and_backed_up(tmp_path, payload):
    user = tmp_path / "u.json"
    user.write_text(payload, encoding="utf-8")
    lib = Library(user)
    assert lib.user_roles == {} and lib.user_prompts == {} and any("u.json" in w for w in lib.warnings)
    assert (tmp_path / "u.json.corrupt").read_text(encoding="utf-8") == payload
    lib.add_prompt("Nuevo", "texto")
    assert (tmp_path / "u.json.corrupt").read_text(encoding="utf-8") == payload


def test_oversized_file_is_rejected(tmp_path):
    user = tmp_path / "u.json"
    user.write_text('{"version":1,"roles":[],"prompts":[],"relleno":"' + "x" * MAX_FILE_BYTES + '"}', encoding="utf-8")
    lib = Library(user)
    assert any("demasiado grande" in w for w in lib.warnings) and lib.user_prompts == {}


def test_default_ids_cannot_use_user_prefix_and_vice_versa(tmp_path):
    d = write(tmp_path / "d.json", {"version": 1, "roles": [
        {"id": "usr-hack", "nombre": "X", "prompt_base": "p"},
        {"id": "neutral", "nombre": "N", "prompt_base": "p"}], "prompts": []})
    lib = Library(defaults_path=d)
    assert list(lib.default_roles) == ["neutral"] and any("usr-hack" in w or "#1" in w for w in lib.warnings)
    user = write(tmp_path / "u.json", {"version": 1, "roles": [{"id": "neutral", "nombre": "N", "prompt_base": "p"}], "prompts": []})
    assert Library(user).user_roles == {}


def test_never_evaluates_content(tmp_path):
    user = write(tmp_path / "u.json", {"version": 1, "roles": [
        {"id": "usr-x", "nombre": "__import__('os').system('echo hacked')", "prompt_base": "{0.__class__}"}], "prompts": []})
    lib = Library(user)
    assert lib.user_roles["usr-x"].prompt_base == "{0.__class__}"
    assert lib.role_prompt("usr-x") == LANG + "{0.__class__}"


def test_new_default_version_loads_without_touching_user_items(tmp_path):
    user = tmp_path / "u.json"
    lib = Library(user, defaults_file(tmp_path, 1))
    mine = lib.add_prompt("Mio", "texto mio", categoria="Personal", etiquetas=["x"])
    role = lib.add_role("Mi rol", "base mia")
    before = user.read_text(encoding="utf-8")
    new_default = {"id": "nuevo-por-defecto", "titulo": "Nuevo", "rol": "neutral", "categoria": "Análisis", "texto": "t", "etiquetas": [], "desde": 2}
    lib2 = Library(user, defaults_file(tmp_path, 2, [new_default]))
    assert lib2.defaults_version == 2 and lib2.updated_from == 1
    assert "nuevo-por-defecto" in lib2.default_prompts
    assert [i.id for i in lib2.new_defaults()] == ["nuevo-por-defecto"]
    assert lib2.user_prompts[mine.id].texto == "texto mio" and role.id in lib2.user_roles
    assert user.read_text(encoding="utf-8") == before
    lib2.acknowledge_defaults()
    assert lib2.updated_from is None and json.loads(user.read_text(encoding="utf-8"))["defaults_vista"] == 2
    assert Library(user, defaults_file(tmp_path, 2, [new_default])).updated_from is None
    assert mine.id in Library(user, defaults_file(tmp_path, 2, [new_default])).user_prompts


def test_fresh_install_reports_no_updates(tmp_path):
    lib = Library(tmp_path / "u.json")
    assert lib.updated_from is None and lib.new_defaults() == []


def test_removed_default_role_keeps_user_prompt_with_warning(tmp_path):
    user = tmp_path / "u.json"
    lib = Library(user, defaults_file(tmp_path, 1, extra_roles=[{"id": "temp", "nombre": "T", "prompt_base": "p"}]))
    lib.add_prompt("P", "t", rol="temp")
    lib2 = Library(user, defaults_file(tmp_path, 2))
    assert len(lib2.user_prompts) == 1 and any("temp" in w for w in lib2.warnings)


def test_editing_a_default_duplicates_it_to_user_library(tmp_path):
    lib = Library(tmp_path / "u.json")
    original = lib.default_prompts["revision-codigo"]
    copy = lib.edit_prompt("revision-codigo")
    assert copy.id.startswith("usr-") and copy.origen == "usuario" and copy.titulo.endswith("(copia)")
    assert copy.texto == original.texto and lib.default_prompts["revision-codigo"] == original
    lib.update_prompt(copy.id, texto="otro texto", titulo="Mi revisión")
    assert lib.user_prompts[copy.id].texto == "otro texto" and lib.default_prompts["revision-codigo"].texto == original.texto
    assert lib.edit_prompt(copy.id) is lib.user_prompts[copy.id]
    assert Library(tmp_path / "u.json").user_prompts[copy.id].titulo == "Mi revisión"


def test_defaults_are_read_only(tmp_path):
    lib = Library(tmp_path / "u.json")
    for call in (lambda: lib.update_prompt("revision-codigo", texto="x"), lambda: lib.delete_prompt("revision-codigo"),
                 lambda: lib.update_role("critic", nombre="x"), lambda: lib.delete_role("critic")):
        with pytest.raises(PermissionError):
            call()
    with pytest.raises(KeyError):
        lib.delete_prompt("usr-noexiste")


def test_crud_roles_and_prompts_and_duplicate(tmp_path):
    lib = Library(tmp_path / "u.json")
    r = lib.add_role("Poeta", "Responde en verso.", "Estilo lírico")
    r2 = lib.duplicate_role(r.id)
    assert r2.id != r.id and r2.nombre == "Poeta (copia)" and r2.prompt_base == r.prompt_base
    dr = lib.edit_role("critic")
    assert dr.id.startswith("usr-") and dr.prompt_base == lib.default_roles["critic"].prompt_base
    p = lib.add_prompt("T", "texto", rol=r.id, categoria="C", etiquetas=["uno", "dos"])
    assert lib.duplicate_prompt(p.id).etiquetas == ["uno", "dos"]
    lib.delete_prompt(p.id)
    lib.delete_role(r.id)
    assert p.id not in lib.user_prompts and r.id not in lib.user_roles
    with pytest.raises(ValueError):
        lib.add_prompt("", "x")
    with pytest.raises(ValueError):
        lib.add_role("n", "x" * 20_001)


def test_ids_never_collide(tmp_path):
    lib = Library(tmp_path / "u.json")
    ids = [lib.add_prompt(f"p{i}", "t").id for i in range(150)] + [lib.add_role(f"r{i}", "b").id for i in range(150)]
    assert len(set(ids)) == 300 and all(i.startswith("usr-") for i in ids)
    assert not set(ids) & (set(lib.default_roles) | set(lib.default_prompts))


@pytest.mark.skipif(os.name == "nt", reason="permisos POSIX")
def test_user_file_written_atomically_with_600(tmp_path):
    lib = Library(tmp_path / "sub" / "u.json")
    lib.add_prompt("a", "b")
    assert stat.S_IMODE((tmp_path / "sub" / "u.json").stat().st_mode) == 0o600
    assert not list((tmp_path / "sub").glob("*.tmp"))


def test_loading_never_rewrites_the_user_file(tmp_path):
    user = write(tmp_path / "u.json", {"version": 1, "roles": [], "prompts": [
        {"id": "usr-p", "titulo": "A", "texto": "b", "etiquetas": []}]})
    before = user.read_bytes()
    Library(user)
    assert user.read_bytes() == before


def test_search_and_filters(tmp_path):
    lib = Library(tmp_path / "u.json")
    mine = lib.add_prompt("Receta de paella", "arroz y azafrán", categoria="Cocina", etiquetas=["comida"])
    assert [p.id for p in lib.search_prompts("paella")] == [mine.id]
    assert [p.id for p in lib.search_prompts("arroz azafrán")] == [mine.id]
    assert lib.search_prompts("zzzz") == []
    assert all(p.rol == "critic" for p in lib.search_prompts(rol="critic")) and lib.search_prompts(rol="critic")
    assert {p.categoria for p in lib.search_prompts(categoria="Programación")} == {"Programación"}
    assert [p.id for p in lib.search_prompts(etiqueta="comida")] == [mine.id]
    assert [p.id for p in lib.search_prompts(origen="usuario")] == [mine.id]
    assert "Cocina" in lib.categorias() and "comida" in lib.etiquetas()


def test_export_then_import_roundtrip(tmp_path):
    a = Library(tmp_path / "a.json")
    role = a.add_role("Poeta", "En verso.")
    a.add_prompt("Soneto", "Escribe un soneto", rol=role.id, categoria="Escritura", etiquetas=["poesía"])
    out = tmp_path / "export.json"
    a.export_user(out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == 1 and len(doc["roles"]) == 1 and "defaults_vista" not in doc
    b = Library(tmp_path / "b.json")
    rep = b.import_user(out)
    assert (rep.roles, rep.prompts, rep.skipped) == (1, 1, 0) and list(b.user_roles) == list(a.user_roles)
    again = b.import_user(out)
    assert (again.roles, again.prompts, again.skipped) == (0, 0, 2)
    assert Library(tmp_path / "b.json").user_prompts.keys() == a.user_prompts.keys()


def test_import_remaps_foreign_ids_and_role_references(tmp_path):
    src = write(tmp_path / "x.json", {"version": 3, "roles": [{"id": "critic", "nombre": "Mi crítico", "prompt_base": "duro"}],
                                      "prompts": [{"id": "revision-codigo", "titulo": "Mi revisión", "texto": "t", "rol": "critic"}]})
    lib = Library(tmp_path / "u.json")
    default_before = dict(lib.default_roles), dict(lib.default_prompts)
    rep = lib.import_user(src)
    assert rep.roles == 1 and rep.prompts == 1
    new_role = next(iter(lib.user_roles.values()))
    new_prompt = next(iter(lib.user_prompts.values()))
    assert new_role.id.startswith("usr-") and new_prompt.id.startswith("usr-") and new_prompt.rol == new_role.id
    assert (lib.default_roles, lib.default_prompts) == default_before


def test_import_invalid_file_changes_nothing(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    lib = Library(tmp_path / "u.json")
    rep = lib.import_user(bad)
    assert rep.roles == rep.prompts == 0 and any("No se importó nada" in w for w in rep.warnings)
    rep = lib.import_user(tmp_path / "no-existe.json")
    assert rep.warnings and lib.user_prompts == {}


class SystemCapture:
    def __init__(self):
        self.systems: list[str] = []

    async def generate(self, slot, system, messages, images=None):
        self.systems.append(system)
        return "ok"


async def test_custom_role_is_used_like_builtin_in_orchestrator(tmp_path):
    lib = Library(tmp_path / "u.json")
    role = lib.add_role("Pirata", "Hablas como un pirata del Caribe.")
    drole = lib.add_role("Director breve", "Sintetiza en una sola frase.", ambito="director")
    set_library(lib)
    s = AppSettings(director_role=drole.id,
                    slots=[ModelSlot(name="a", provider="custom", model="m", base_url="http://x", role=role.id),
                           ModelSlot(name="b", provider="custom", model="m", base_url="http://x", role="critic")])
    s.director = ModelSlot(name="d", provider="custom", model="m", base_url="http://x")
    cap = SystemCapture()
    await Orchestrator(s, cap).run("hola")
    assert LANG + "Hablas como un pirata del Caribe." in cap.systems[0]
    assert "auditor de la mesa" in cap.systems[1]
    assert LANG + "Sintetiza en una sola frase." in cap.systems[2]


async def test_unknown_or_wrong_scope_role_falls_back():
    lib = get_library()
    assert lib.role_prompt("usr-borrado") == lib.role_prompt("neutral")
    assert lib.role_prompt("critic", director=True) == lib.role_prompt("director", director=True)
    assert lib.role_prompt("director") == lib.role_prompt("neutral")


async def test_default_director_prompt_unchanged_in_orchestrator():
    from src.core.roles import DIRECTOR
    s = AppSettings(slots=[ModelSlot(name="a", provider="custom", model="m", base_url="http://x")])
    s.director = ModelSlot(name="d", provider="custom", model="m", base_url="http://x")
    cap = SystemCapture()
    await Orchestrator(s, cap).run("hola")
    assert LANG + DIRECTOR in cap.systems[-1]


def test_apply_prompt_to_slot(tmp_path):
    lib = Library(tmp_path / "u.json")
    slot = ModelSlot(name="a")
    lib.apply_prompt(slot, lib.default_prompts["revision-codigo"])
    assert slot.system_prompt == lib.default_prompts["revision-codigo"].texto and slot.role == "critic"
    d = ModelSlot(name="d", role="moderator")
    lib.apply_prompt(d, lib.default_prompts["revision-codigo"], director=True)
    assert d.role == "moderator" and d.system_prompt
    custom = lib.add_prompt("sin rol", "t", rol="usr-fantasma")
    slot2 = ModelSlot(name="b", role="neutral")
    lib.apply_prompt(slot2, custom)
    assert slot2.role == "neutral"


def test_module_singleton_set_and_get(tmp_path):
    lib = Library(tmp_path / "u.json")
    set_library(lib)
    assert get_library() is lib
    set_library(None)
    assert get_library() is not lib and lib_mod._ACTIVE is not lib
