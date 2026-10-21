from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

pytest.importorskip("flet")

from src import ui  # noqa: E402
from src.core.settings import MAX_ROUNDS  # noqa: E402
from tests.test_ui_build import FakePage  # noqa: E402
from tests.ui_helpers import find, find_all, texts  # noqa: E402


def ev(ctrl, **kw):
    return SimpleNamespace(control=ctrl, **kw)


def toasts(app) -> list[str]:
    return [d.content.value for d in app.page.dialogs if type(d).__name__ == "SnackBar"]


def alert(app, title: str):
    for d in app.page.dialogs:
        if type(d).__name__ == "AlertDialog" and d.title.value == title:
            return d
    return None


async def wait_alert(app, title: str):
    for _ in range(150):
        d = alert(app, title)
        if d is not None:
            return d
        await asyncio.sleep(0.02)
    raise AssertionError(f"no apareció el diálogo {title!r}: {[getattr(getattr(d, 'title', None), 'value', None) for d in app.page.dialogs]}")


def press(dialog, label):
    next(b for b in dialog.actions if b.content == label).on_click(None)


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    a = ui.NodeAiApp(FakePage())
    await a.start()
    a.tmp = tmp_path
    return a


def config_file(app) -> str:
    app.store.save()
    return (app.tmp / "usuario" / "config.json").read_text(encoding="utf-8")


async def test_everything_lives_in_settings_no_side_panel(app):
    assert not hasattr(app, "side_panel") and not hasattr(app, "side_col") and not hasattr(app, "panel_view")
    app.page.width = 1400
    app.go("chat")
    assert app.chat_view.visible and not app.config_view.visible
    row = app.root.controls[0]
    assert all(getattr(c, "width", None) != 340 for c in row.controls)
    app.go("config", "mesa")
    assert find(app.config_col.controls, "TextField", label="Rondas de debate") is not None
    app.go("config", "director")
    assert find(app.config_col.controls, "Slider") is not None
    assert len(find_all(app.config_view.content.controls, "ListTile")) == 6
    app.page.width = 500
    app._layout()
    assert len(find_all(app.config_view.content.controls, "FilledTonalButton")) == 1
    assert len(find_all(app.config_view.content.controls, "TextButton")) == 5


async def test_slot_controls_moved_to_settings_work(app):
    s = app.store.add_slot()
    s2 = app.store.add_slot()
    app.go("config", "mesa")
    assert "2 activas de 2" in " ".join(texts([app.table_count]))
    sw = next(c for c in find_all(app.config_col.controls, "Switch") if c.tooltip == "Activa en la mesa")
    sw.value = False
    sw.on_change(ev(sw))
    assert s.enabled is False and s2.enabled is True and "1 activas de 2" in app.table_count.value
    assert find(app.config_col.controls, "Dropdown", label="Rol") is not None
    assert find(app.config_col.controls, "Dropdown", label="Ejecución") is not None
    assert len([c for c in find_all(app.config_col.controls, "Slider")]) >= 2


def rounds_field(app):
    app.go("config", "mesa")
    return find(app.config_col.controls, "TextField", label="Rondas de debate")


async def test_rounds_is_a_numeric_text_field_not_a_slider(app):
    app.go("config", "mesa")
    assert find(app.config_col.controls, "TextField", label="Rondas de debate") is not None
    assert not any("ronda" in (getattr(c, "label", "") or "").lower() for c in find_all(app.config_col.controls, "Slider"))
    f = rounds_field(app)
    assert f.value == "1" and f.keyboard_type.name == "NUMBER"


@pytest.mark.parametrize("bad", ["abc", "2.5", "0", "", "-3", "1e3", "1,5", " ", "٣"])
async def test_invalid_rounds_restore_last_valid_with_notice(app, bad):
    f = rounds_field(app)
    f.value = "7"
    f.on_change(ev(f))
    assert app.s.rounds == 7
    before = len(toasts(app))
    f.value = bad
    f.on_change(ev(f))
    assert app.s.rounds == 7
    f.on_blur(ev(f))
    assert f.value == "7" and app.s.rounds == 7
    assert any("Valor no válido" in x for x in toasts(app)[before:])


async def test_rounds_accept_any_integer_above_three_and_warn_when_high(app):
    f = rounds_field(app)
    for v in ("2", "3", "4", "12"):
        f.value = v
        f.on_change(ev(f))
        f.on_blur(ev(f))
        assert app.s.rounds == int(v)
    assert any("consumen muchos más tokens" in x for x in toasts(app))
    n = len(toasts(app))
    f.value = "3"
    f.on_blur(ev(f))
    assert len(toasts(app)) == n


async def test_rounds_are_capped_by_max_rounds_with_notice(app):
    f = rounds_field(app)
    f.value = str(MAX_ROUNDS + 500)
    f.on_change(ev(f))
    assert app.s.rounds == MAX_ROUNDS and f.value == str(MAX_ROUNDS)
    assert any(f"máximo permitido es {MAX_ROUNDS}" in x for x in toasts(app))


async def test_int_field_normalizes_and_calls_on_change(app):
    class Obj:
        n = 3
    seen = []
    field = app._int_field("x", Obj, "n", lo=2, on_change=lambda: seen.append(Obj.n))
    field.value = "007"
    field.on_change(ev(field))
    field.on_blur(ev(field))
    assert Obj.n == 7 and field.value == "7" and seen == [7]
    field.value = "1"
    field.on_submit(ev(field))
    assert Obj.n == 7 and field.value == "7"


async def chat_server(aiohttp_server, calls):
    async def ep(request):
        body = await request.json()
        calls.append(body["model"])
        return web.json_response({"choices": [{"message": {"content": f"r-{body['model']}"}}]})
    web_app = web.Application()
    web_app.router.add_post("/v1/chat/completions", ep)
    return await aiohttp_server(web_app)


async def test_four_rounds_run_and_progress_is_correct(app, aiohttp_server):
    calls: list[str] = []
    srv = await chat_server(aiohttp_server, calls)
    url = f"http://{srv.host}:{srv.port}/v1"
    d = app.s.director
    d.provider, d.model, d.api_key, d.base_url = "custom", "director", "k", url
    for m in ("a", "b"):
        s = app.store.add_slot()
        s.provider, s.model, s.api_key, s.base_url = "custom", m, "k", url
    app.s.rounds = 4
    log: list[str] = []
    app.page.update = lambda *a: log.append(" ".join(texts(app.messages.controls[-1:])))
    app.go("chat")
    app.input.value = "hola"
    await app.on_send(None)
    await app.task
    assert sorted(calls) == ["a"] * 4 + ["b"] * 4 + ["director"]
    assert any("8/8" in x and "ronda 4/4" in x for x in log)
    assert not any("ronda 5" in x or "/12" in x for x in log)
    tile_titles = " ".join(texts(app.messages.controls[-1:]))
    assert "ronda 4" in tile_titles and "Mesa: 8/8 respuestas" in tile_titles


def key_controls(app, provider="openai"):
    d = app.s.director
    d.mode, d.provider = "cloud", provider
    app.go("config", "director")
    sw = find(app.config_col.controls, "Switch", label="Guardar API key en este equipo")
    col = next(c for c in find_all(app.config_col.controls, "Column") if sw in getattr(c, "controls", []))
    return sw, col


async def test_save_key_switch_is_off_by_default_with_warning_right_below(app):
    sw, col = key_controls(app)
    assert sw.value is False
    assert col.controls[0] is sw
    warn = col.controls[1]
    assert type(warn).__name__ == "Text" and warn.value == "La API key se guarda en texto plano en tu equipo."
    assert warn.color is not None
    assert find(app.config_col.controls, "TextField", label="API key (vacía = variable OPENAI_API_KEY)").password is True


async def test_key_stays_in_memory_unless_switch_is_on(app):
    sw, _ = key_controls(app)
    d = app.s.director
    field = find(app.config_col.controls, "TextField", label="API key (vacía = variable OPENAI_API_KEY)")
    field.value = "sk-secreto-123"
    field.on_change(ev(field))
    assert d.api_key == "sk-secreto-123"
    assert "sk-secreto-123" not in config_file(app)
    sw.value = True
    sw.on_change(ev(sw))
    assert d.save_key is True
    assert "sk-secreto-123" in (app.tmp / "usuario" / "config.json").read_text(encoding="utf-8")
    sw.value = False
    sw.on_change(ev(sw))
    assert "sk-secreto-123" not in (app.tmp / "usuario" / "config.json").read_text(encoding="utf-8")
    assert d.api_key == "sk-secreto-123"


async def test_env_var_key_still_works_without_saving(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "desde-entorno")
    a = ui.NodeAiApp(FakePage())
    a.s.director.provider, a.s.director.model = "openai", "gpt"
    assert a.s.director.is_ready()
    a.store.save()
    assert "desde-entorno" not in (tmp_path / "usuario" / "config.json").read_text(encoding="utf-8")


def legacy_home(tmp_path, key="sk-antigua-777"):
    (tmp_path / "config.json").write_text(json.dumps({"configured": True, "director": {
        "provider": "openai", "model": "gpt", "api_key": key}}), encoding="utf-8")


async def test_legacy_keys_notice_remove_from_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    legacy_home(tmp_path)
    a = ui.NodeAiApp(FakePage())
    await a.start()
    dlg = await wait_alert(a, "Cambio en el guardado de las API keys")
    assert (tmp_path / "config.json").exists()
    press(dlg, "Quitar del disco")
    for _ in range(50):
        if not (tmp_path / "config.json").exists():
            break
        await asyncio.sleep(0.02)
    assert not (tmp_path / "config.json").exists() and not (tmp_path / "config.json.bak").exists()
    assert "sk-antigua-777" not in (tmp_path / "usuario" / "config.json").read_text(encoding="utf-8")
    assert a.s.director.api_key == "sk-antigua-777"
    assert any("quitaron del disco" in x for x in toasts(a))


async def test_legacy_keys_notice_keep_and_shown_only_once(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    legacy_home(tmp_path)
    a = ui.NodeAiApp(FakePage())
    await a.start()
    press(await wait_alert(a, "Cambio en el guardado de las API keys"), "Mantener guardadas")
    await asyncio.sleep(0.1)
    assert "sk-antigua-777" in (tmp_path / "usuario" / "config.json").read_text(encoding="utf-8")
    b = ui.NodeAiApp(FakePage())
    await b.start()
    await asyncio.sleep(0.1)
    assert alert(b, "Cambio en el guardado de las API keys") is None
    assert b.s.director.save_key is True and b.s.director.api_key == "sk-antigua-777"


async def test_migration_toast_and_user_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"configured": True, "rounds": 3}), encoding="utf-8")
    a = ui.NodeAiApp(FakePage())
    await a.start()
    await asyncio.sleep(0.1)
    assert (tmp_path / "usuario" / "config.json").exists() and (tmp_path / "config.json.bak").exists()
    assert any("config.json.bak" in x for x in toasts(a)) and a.s.rounds == 3
    assert (tmp_path / "usuario" / "prompts_usuario.json").parent.is_dir()


async def test_step_selector_two_options_under_the_agent_switch(app):
    ctrls = app.agent_ui.config_section()
    assert type(ctrls[1]).__name__ == "Switch" and ctrls[1].label == "Activar modo agente en el chat"
    group = find([ctrls[2]], "RadioGroup")
    assert group is not None and group.value == "limit"
    labels = [r.label for r in find_all([group], "Radio")]
    assert labels == ["Límite de pasos", "Sin límite de pasos"]
    steps = find([group], "TextField", label="Máx. pasos")
    assert steps.value == "30" and steps.disabled is False


async def test_selecting_no_limit_updates_settings_and_hides_counter(app):
    app.go("config", "agente")
    group = find(app.config_col.controls, "RadioGroup")
    group.value = "unlimited"
    group.on_change(ev(group, ))
    assert app.s.agent.unlimited_steps is True
    group = find(app.config_col.controls, "RadioGroup")
    assert group.value == "unlimited"
    assert find([group], "TextField", label="Máx. pasos").disabled is True
    assert any("Sin límite:" in x for x in texts(app.config_col.controls))
    assert find(app.config_col.controls, "TextField", label="Repeticiones iguales = bucle") is not None
    group.value = "limit"
    group.on_change(ev(group))
    assert app.s.agent.unlimited_steps is False
    assert not any("Sin límite:" in x for x in texts(app.config_col.controls))


async def test_max_steps_field_validates_integers(app):
    app.go("config", "agente")
    f = find(app.config_col.controls, "TextField", label="Máx. pasos")
    f.value = "45"
    f.on_change(ev(f))
    assert app.s.agent.max_steps == 45
    f.value = "x"
    f.on_blur(ev(f))
    assert f.value == "45" and app.s.agent.max_steps == 45
    f.value = "0"
    f.on_blur(ev(f))
    assert app.s.agent.max_steps == 45


async def test_old_config_without_new_fields_keeps_limit_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    (tmp_path / "usuario").mkdir()
    (tmp_path / "usuario" / "config.json").write_text(json.dumps({"agent": {"enabled": True, "max_steps": 12}}), encoding="utf-8")
    a = ui.NodeAiApp(FakePage())
    assert a.s.agent.unlimited_steps is False and a.s.agent.max_steps == 12
    group = find(a.agent_ui.config_section(), "RadioGroup")
    assert group.value == "limit"


async def models_server(aiohttp_server, hits, fail=False):
    async def ep(request):
        hits.append(request.headers.get("Authorization"))
        if fail:
            return web.json_response({}, status=401)
        return web.json_response({"data": [{"id": m} for m in ("gpt-4o", "gpt-4o-mini", "o3", "claude-x")]})
    web_app = web.Application()
    web_app.router.add_get("/v1/models", ep)
    return await aiohttp_server(web_app)


def custom_slot(app, srv, key="k"):
    s = app.store.add_slot()
    s.provider, s.api_key, s.base_url = "custom", key, f"http://{srv.host}:{srv.port}/v1"
    return s


async def test_model_picker_lists_filters_refreshes_and_picks(app, aiohttp_server):
    from src.llm import model_lister
    model_lister.clear_cache()
    hits: list = []
    srv = await models_server(aiohttp_server, hits)
    slot = custom_slot(app, srv)
    picked: list[str] = []
    task = asyncio.create_task(app.model_picker.open(slot, picked.append))
    dlg = await wait_alert(app, "Modelos de Otro (compatible OpenAI, URL propia)")
    search = find(dlg.content.controls, "TextField", label="Buscar modelo")
    listing = find(dlg.content.controls, "ListView")
    for _ in range(100):
        if listing.controls:
            break
        await asyncio.sleep(0.02)
    assert [c.title.value for c in listing.controls] == ["claude-x", "gpt-4o", "gpt-4o-mini", "o3"]
    search.value = "gpt mini"
    search.on_change(ev(search))
    assert [c.title.value for c in listing.controls] == ["gpt-4o-mini"]
    status = find(dlg.content.controls, "Text", value="1 de 4 modelos")
    assert status is not None
    refresh = find(dlg.content.controls, "OutlinedButton")
    assert refresh.content == "Actualizar lista"
    refresh.on_click(None)
    await asyncio.sleep(0.2)
    assert len(hits) == 2
    listing.controls[0].on_click(None)
    assert await task == "gpt-4o-mini" and picked == ["gpt-4o-mini"]


async def test_model_picker_uses_session_cache(app, aiohttp_server):
    from src.llm import model_lister
    model_lister.clear_cache()
    hits: list = []
    srv = await models_server(aiohttp_server, hits)
    slot = custom_slot(app, srv)
    for _ in range(2):
        task = asyncio.create_task(app.model_picker.open(slot, lambda m: None))
        dlg = await wait_alert(app, "Modelos de Otro (compatible OpenAI, URL propia)")
        for _ in range(100):
            if find(dlg.content.controls, "ListView").controls:
                break
            await asyncio.sleep(0.02)
        press(dlg, "Cerrar")
        await task
        app.page.dialogs.clear()
    assert len(hits) == 1


async def test_model_picker_shows_clear_error_without_key_and_manual_id_still_works(app, aiohttp_server):
    from src.llm import model_lister
    model_lister.clear_cache()
    srv = await models_server(aiohttp_server, [], fail=True)
    slot = custom_slot(app, srv, key="sk-no-debe-verse")
    task = asyncio.create_task(app.model_picker.open(slot, lambda m: None))
    dlg = await wait_alert(app, "Modelos de Otro (compatible OpenAI, URL propia)")
    for _ in range(100):
        if any("inválida" in x for x in texts(dlg.content.controls)):
            break
        await asyncio.sleep(0.02)
    shown = " ".join(texts(dlg.content.controls))
    assert "API key inválida" in shown and "sk-no-debe-verse" not in shown
    press(dlg, "Cerrar")
    assert await task is None
    app.go("config", "mesa")
    model = find(app.config_col.controls, "TextField", label="Modelo (id exacto del proveedor)")
    model.value = "mi-modelo-manual"
    model.on_change(ev(model))
    assert slot.model == "mi-modelo-manual"
    assert find(app.config_col.controls, "IconButton", tooltip="Elegir de la lista de modelos del proveedor") is not None


async def test_library_tab_lists_filters_and_applies_to_slot(app):
    slot = app.store.add_slot()
    app.go("config", "biblioteca")
    lu = app.lib_ui
    assert len(lu.list_box.controls) == 1 + len(app.library.default_prompts)
    lu.query = "paella"
    lu.render_list()
    assert lu.list_box.controls[0].value == "0 prompts"
    lu.query, lu.f_rol = "", "critic"
    lu.render_list()
    shown = [c.title.value for c in lu.list_box.controls[1:]]
    assert shown and all(app.library.search_prompts(rol="critic")[i].titulo == shown[i] for i in range(len(shown)))
    lu.f_rol, lu.f_cat = "*", "Programación"
    lu.render_list()
    assert {c.subtitle.value.split(" · ")[1] for c in lu.list_box.controls[1:]} == {"Programación"}
    lu.f_cat, lu.query = "*", "revisión código"
    lu.render_list()
    assert len(lu.list_box.controls) == 2
    prompt = lu.filtered()[0]
    lu.target = slot.id
    lu.use_prompt(prompt)
    assert slot.system_prompt == prompt.texto and slot.role == "critic"
    assert any("aplicado a" in x for x in toasts(app))
    lu.target = "director"
    lu.use_prompt(prompt)
    assert app.s.director.system_prompt == prompt.texto


async def test_library_edit_default_creates_user_copy_via_dialog(app):
    app.go("config", "biblioteca")
    lu = app.lib_ui
    default = app.library.default_prompts["revision-codigo"]
    task = asyncio.create_task(lu.edit_prompt(default))
    dlg = await wait_alert(app, "Editar prompt")
    assert any("prompt por defecto" in x for x in texts(dlg.content.controls))
    title = find(dlg.content.controls, "TextField", label="Título")
    body = find(dlg.content.controls, "TextField", label="Texto del prompt")
    title.value, body.value = "Mi revisión", "texto personalizado"
    press(dlg, "Guardar")
    await task
    assert app.library.default_prompts["revision-codigo"].texto == default.texto
    mine = [p for p in app.library.user_prompts.values()]
    assert len(mine) == 1 and mine[0].titulo == "Mi revisión" and mine[0].id.startswith("usr-")
    saved = json.loads((app.tmp / "usuario" / "prompts_usuario.json").read_text(encoding="utf-8"))
    assert saved["prompts"][0]["texto"] == "texto personalizado"


async def test_library_new_prompt_validation_error_is_shown(app):
    app.go("config", "biblioteca")
    task = asyncio.create_task(app.lib_ui.edit_prompt(None))
    dlg = await wait_alert(app, "Nuevo prompt")
    press(dlg, "Guardar")
    await task
    assert any("No se pudo guardar" in x for x in toasts(app)) and app.library.user_prompts == {}


async def test_library_delete_requires_confirmation_and_only_user_items(app):
    p = app.library.add_prompt("Borrable", "t")
    app.go("config", "biblioteca")
    lu = app.lib_ui
    tile = next(c for c in lu.list_box.controls[1:] if c.title.value == "Borrable")
    assert len([b for b in find_all([tile], "OutlinedButton") if b.content == "Borrar"]) == 1
    default_tile = next(c for c in lu.list_box.controls[1:] if c.title.value == "Revisión de código")
    assert not [b for b in find_all([default_tile], "OutlinedButton") if b.content == "Borrar"]
    task = asyncio.create_task(lu.delete_prompt(p))
    dlg = await wait_alert(app, "Confirmar")
    press(dlg, "Cancelar")
    await task
    assert p.id in app.library.user_prompts
    app.page.dialogs.clear()
    task = asyncio.create_task(lu.delete_prompt(p))
    press(await wait_alert(app, "Confirmar"), "Borrar")
    await task
    assert p.id not in app.library.user_prompts


async def test_custom_role_appears_in_slot_and_director_dropdowns(app):
    role = app.library.add_role("Poeta", "En verso.")
    drole = app.library.add_role("Director breve", "Una frase.", ambito="director")
    app.store.add_slot()
    app.go("config", "mesa")
    opts = [o.key for o in find(app.config_col.controls, "Dropdown", label="Rol").options]
    assert role.id in opts and drole.id not in opts and "critic" in opts
    app.go("config", "director")
    dopts = [o.key for o in find(app.config_col.controls, "Dropdown", label="Rol del Director").options]
    assert drole.id in dopts and role.id not in dopts and "director" in dopts


async def test_library_import_export_via_picker(app, tmp_path):
    app.library.add_prompt("Para exportar", "texto")
    out = tmp_path / "salida.json"

    async def save_file(**kw):
        return str(out)

    app.picker.save_file = save_file
    await app.lib_ui.export_lib()
    assert json.loads(out.read_text(encoding="utf-8"))["prompts"][0]["titulo"] == "Para exportar"
    app.library.delete_prompt(next(iter(app.library.user_prompts)))

    async def pick_files(**kw):
        return [SimpleNamespace(path=str(out))]

    app.picker.pick_files = pick_files
    await app.lib_ui.import_lib()
    assert [p.titulo for p in app.library.user_prompts.values()] == ["Para exportar"]
    assert any("Importados: 0 roles y 1 prompts" in x for x in toasts(app))


async def test_library_new_defaults_notice_and_ack(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    (tmp_path / "usuario").mkdir()
    (tmp_path / "usuario" / "prompts_usuario.json").write_text(
        json.dumps({"version": 1, "defaults_vista": 0, "roles": [], "prompts": []}), encoding="utf-8")
    a = ui.NodeAiApp(FakePage())
    await a.start()
    a.library.updated_from = 0
    a.go("config", "biblioteca")
    assert any("versión 1" in x for x in texts(a.config_col.controls))
    ack = find(a.config_col.controls, "TextButton", content="Entendido")
    ack.on_click(None)
    assert a.library.updated_from is None


async def test_language_switch_is_instant_persisted_and_covers_the_window(app):
    app.go("config", "general")
    dd = find(app.config_col.controls, "Dropdown", label="Idioma de la interfaz")
    assert [o.key for o in dd.options] == ["es", "en", "pt", "fr", "de"]
    dd.value = "en"
    dd.on_select(ev(dd))
    assert app.s.language == "en"
    assert app.rail.destinations[1].label == "Settings" and app.navbar.destinations[1].label == "Settings"
    assert app.btn_send.tooltip == "Send" and "Type your message" in app.input.hint_text
    assert find(app.config_col.controls, "Dropdown", label="Interface language") is not None
    assert "Welcome to NodeAi: initial setup" in texts(app.config_col.controls)
    assert json.loads((app.tmp / "usuario" / "config.json").read_text(encoding="utf-8"))["language"] == "en"
    nav = [t for t in texts(app.config_view.content.controls)]
    assert "Agent" in nav and "Library" in nav and "AI table" in nav


async def test_language_persists_across_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    a = ui.NodeAiApp(FakePage())
    await a.start()
    a._set_language("fr")
    b = ui.NodeAiApp(FakePage())
    await b.start()
    assert b.s.language == "fr" and b.rail.destinations[1].label == "Paramètres"


@pytest.mark.parametrize("code", ["es", "en", "pt", "fr", "de"])
async def test_every_language_builds_every_tab_and_runs_a_chat_turn_label(app, code):
    app._set_language(code)
    app.store.add_slot()
    for tab in ("general", "director", "mesa", "agente", "biblioteca", "local"):
        app.go("config", tab)
        assert app.config_col.controls
    from src.core.i18n import t
    assert app.rail.destinations[0].label == "Chat" and app.btn_stop.tooltip == t("Detener")


async def test_existing_users_without_language_key_stay_in_spanish(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    (tmp_path / "usuario").mkdir()
    (tmp_path / "usuario" / "config.json").write_text(json.dumps({"configured": True}), encoding="utf-8")
    a = ui.NodeAiApp(FakePage())
    assert a.s.language == "es"
