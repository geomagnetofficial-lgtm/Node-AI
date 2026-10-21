import asyncio
import json

import pytest
from aiohttp import web

flet = pytest.importorskip("flet")

from src import ui  # noqa: E402
from src.agent import apps  # noqa: E402
from tests.test_agent import FakeDesktop, act  # noqa: E402
from tests.test_ui_build import FakePage  # noqa: E402
from tests.ui_helpers import find, texts  # noqa: E402


@pytest.fixture
async def env(aiohttp_server, tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    script: list = []

    async def ep(request):
        await request.json()
        item = script.pop(0)
        if callable(item):
            await item()
            item = script.pop(0)
        return web.json_response({"choices": [{"message": {"content": json.dumps(item)}}]})

    web_app = web.Application()
    web_app.router.add_post("/v1/chat/completions", ep)
    srv = await aiohttp_server(web_app)
    a = ui.NodeAiApp(FakePage())
    await a.start()
    d = a.s.director
    d.provider, d.model, d.api_key, d.base_url = "custom", "director", "k", f"http://{srv.host}:{srv.port}/v1"
    a.s.agent.enabled, a.s.agent.settle_s = True, 0
    a.s.agent.workspace = str(tmp_path / "ws")
    a.agent_ui.desktop_factory = FakeDesktop
    a.go("chat")
    a._rebuild_all()
    return a, script


async def wait_dialog(app, n=1):
    for _ in range(100):
        if len(app.page.dialogs) >= n:
            return app.page.dialogs[n - 1]
        await asyncio.sleep(0.02)
    raise AssertionError("no apareció el diálogo")


def press(dialog, label):
    next(b for b in dialog.actions if b.content == label).on_click(None)


async def test_agent_mode_runs_in_chat(env):
    app, script = env
    script += [act("click", x=5, y=6), act("finish", summary="**Listo**, todo hecho")]
    app.input.value = "haz una cosa"
    await app.on_send(None)
    await app.task
    assert len(app.messages.controls) == 2
    assert app.history[-1]["content"] == "**Listo**, todo hecho"
    assert app.btn_send.visible and not app.btn_stop.visible and app.task is None
    assert app.page.dialogs == []


async def test_risky_action_shows_permission_dialog_and_allow_runs_it(env, tmp_path):
    app, script = env
    script += [act("run", command="touch permitido.txt"), act("finish", summary="ok")]
    app.input.value = "crea un archivo"
    await app.on_send(None)
    dlg = await wait_dialog(app)
    assert "touch permitido.txt" in dlg.content.controls[0].value
    press(dlg, "Permitir")
    await app.task
    assert (tmp_path / "ws/permitido.txt").exists() and app.page.popped == 1


async def test_deny_and_allow_all(env, tmp_path):
    app, script = env
    script += [act("run", command="touch a.txt"), act("finish", summary="sin permiso")]
    app.input.value = "x"
    await app.on_send(None)
    press(await wait_dialog(app), "Denegar")
    await app.task
    assert not (tmp_path / "ws/a.txt").exists()
    script += [act("run", command="touch b.txt"), act("run", command="touch c.txt"), act("finish", summary="ok")]
    app.input.value = "y"
    await app.on_send(None)
    press(await wait_dialog(app, 2), "Permitir todo lo que queda")
    await app.task
    assert (tmp_path / "ws/b.txt").exists() and (tmp_path / "ws/c.txt").exists()
    assert len(app.page.dialogs) == 2
    assert app.s.agent.approval == "balanced"


async def test_stop_while_permission_dialog_is_open(env):
    app, script = env
    script += [act("run", command="touch nunca.txt"), act("finish")]
    app.input.value = "x"
    await app.on_send(None)
    await wait_dialog(app)
    await app.on_stop(None)
    await asyncio.sleep(0.2)
    assert app.task is None and app.btn_send.visible and app.page.popped >= 1


async def test_ask_user_dialog(env):
    app, script = env
    script += [act("ask_user", question="¿qué formato?"), act("finish", summary="hecho")]
    app.input.value = "comprime"
    await app.on_send(None)
    dlg = await wait_dialog(app)
    dlg.content.controls[1].value = "zip"
    press(dlg, "Responder")
    await app.task
    assert app.history[-1]["content"] == "hecho"


async def test_blocked_action_is_never_executed_and_ui_shows_it(env):
    app, script = env
    script += [act("run", command="echo shutdown /s"), act("finish", summary="no pude")]
    app.input.value = "apaga"
    await app.on_send(None)
    await app.task
    assert app.page.dialogs == []


async def test_agent_mode_off_uses_normal_chat(env):
    app, script = env
    app.s.agent.enabled = False
    app.agent_ui.sync()
    assert not app.agent_ui.banner.visible
    script += [{"unused": 1}]
    app.input.value = "hola"
    await app.on_send(None)
    await app.task
    assert script == []


async def test_settings_controls_and_toggle(env):
    app, _ = env
    app.s.agent.enabled = False
    app.go("config", "agente")
    sw = find(app.config_col.controls, "Switch", label="Activar modo agente en el chat")
    sw.value = True
    sw.on_change(type("E", (), {"control": sw})())
    assert app.s.agent.enabled and app.agent_ui.banner.visible and "agente" in app.input.hint_text
    assert "Agente (control del PC)" in texts(app.config_col.controls)


async def test_diagnose_reports_screen_and_programs(env, monkeypatch):
    app, _ = env
    monkeypatch.setattr(apps, "detect_all", lambda: {"7-Zip": "C:/7z.exe", "WinRAR": "no encontrado"})
    monkeypatch.setattr("src.ui_agent.Desktop", lambda *a, **k: FakeDesktop())
    await app.agent_ui.diagnose()
    texts = " ".join(r.controls[1].value for r in app.agent_ui.diag_box.controls)
    assert "Pantalla OK" in texts and "7-Zip: C:/7z.exe" in texts and "WinRAR: no encontrado" in texts


async def test_unlimited_mode_runs_past_the_old_step_limit_in_the_ui(env):
    app, script = env
    app.s.agent.max_steps, app.s.agent.unlimited_steps = 2, True
    script += [act("wait", seconds=i / 1000) for i in range(6)] + [act("finish", summary="listo sin tope")]
    app.input.value = "tarea larga"
    await app.on_send(None)
    await app.task
    assert app.history[-1]["content"] == "listo sin tope"
    assert "Pasos del agente: 7" in texts(app.messages.controls)
    assert "✓ Terminado" in texts(app.messages.controls)


async def test_unlimited_mode_stops_on_loop_and_explains_in_the_ui(env):
    app, script = env
    app.s.agent.unlimited_steps, app.s.agent.loop_repeat = True, 4
    script += [act("wait", seconds=0)] * 6
    app.input.value = "tarea atascada"
    await app.on_send(None)
    await app.task
    shown = texts(app.messages.controls)
    assert "⏹ Detenido: bucle detectado" in shown
    assert any("repitió 4 veces seguidas" in x for x in shown)
    assert app.history == [] and app.btn_send.visible and app.task is None


async def test_limited_mode_still_stops_at_max_steps_in_the_ui(env):
    app, script = env
    app.s.agent.max_steps = 3
    script += [act("wait", seconds=i / 1000) for i in range(3)]
    app.input.value = "x"
    await app.on_send(None)
    await app.task
    assert "⏹ Límite de pasos alcanzado" in texts(app.messages.controls)
