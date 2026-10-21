import asyncio

import pytest
from aiohttp import web

flet = pytest.importorskip("flet")

from src import ui  # noqa: E402
from tests.test_ui_build import FakePage  # noqa: E402


async def ep(request):
    body = await request.json()
    await asyncio.sleep(0.05 if body["model"] != "slow" else 5)
    if body["model"] == "bad":
        return web.json_response({"error": {"message": "sin saldo"}}, status=402)
    return web.json_response({"choices": [{"message": {"content": f"**{body['model']}** ok"}}]})


@pytest.fixture
async def app(aiohttp_server, tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    web_app = web.Application()
    web_app.router.add_post("/v1/chat/completions", ep)
    srv = await aiohttp_server(web_app)
    a = ui.NodeAiApp(FakePage())
    await a.start()
    url = f"http://{srv.host}:{srv.port}/v1"
    d = a.s.director
    d.provider, d.model, d.api_key, d.base_url = "custom", "director", "k", url
    for m in ("a", "bad"):
        s = a.store.add_slot()
        s.provider, s.model, s.api_key, s.base_url = "custom", m, "k", url
    a.go("chat")
    a._rebuild_all()
    a.url = url
    return a


async def test_full_chat_round(app):
    app.input.value = "hola mesa"
    await app.on_send(None)
    await app.task
    kinds = [type(c).__name__ for c in app.messages.controls]
    assert kinds == ["Row", "Row"]
    assert len(app.history) == 2 and app.history[1]["content"] == "**director** ok"
    assert app.btn_send.visible and not app.btn_stop.visible and app.task is None


async def test_stop_button_cancels(app):
    app.s.director.model = "slow"
    app.input.value = "x"
    await app.on_send(None)
    await asyncio.sleep(0.1)
    assert app.btn_stop.visible
    await app.on_stop(None)
    await asyncio.sleep(0.1)
    assert app.task is None and app.btn_send.visible and app.history == []


async def test_attachment_flow(app, tmp_path):
    f = tmp_path / "nota.txt"
    f.write_text("contenido secreto")
    from src.multimodal.attachments import load_attachment
    app.attachments.append(load_attachment(f))
    app._render_chips()
    assert len(app.chips.controls) == 1
    app.input.value = "resume"
    await app.on_send(None)
    await app.task
    assert app.attachments == [] and app.chips.controls == []
