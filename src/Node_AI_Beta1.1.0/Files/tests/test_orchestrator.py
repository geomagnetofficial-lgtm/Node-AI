import pytest
from aiohttp import web

from src.core.orchestrator import Orchestrator
from src.core.settings import AppSettings, ModelSlot
from src.llm.client import LLMClient, LLMError

CALLS: list[dict] = []


async def openai_ep(request: web.Request):
    body = await request.json()
    CALLS.append({"path": request.path, "auth": request.headers.get("Authorization"), "body": body})
    model = body["model"]
    if model == "broken":
        return web.json_response({"error": {"message": "boom"}}, status=400)
    if model == "reasoner" and "temperature" in body:
        return web.json_response({"error": {"message": "Unsupported value: 'temperature'"}}, status=400)
    return web.json_response({"choices": [{"message": {"content": f"resp-{model}"}}]})


async def anthropic_ep(request: web.Request):
    body = await request.json()
    CALLS.append({"path": request.path, "key": request.headers.get("x-api-key"), "body": body})
    return web.json_response({"content": [{"type": "text", "text": "resp-claude"}]})


@pytest.fixture
async def server(aiohttp_server):
    CALLS.clear()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", openai_ep)
    app.router.add_post("/v1/messages", anthropic_ep)
    return await aiohttp_server(app)


def slot(server, model, provider="custom", **kw):
    return ModelSlot(name=model, provider=provider, model=model, api_key="k",
                     base_url=f"http://{server.host}:{server.port}" + ("/v1" if provider != "anthropic" else ""), **kw)


async def test_table_and_director(server):
    s = AppSettings(director=slot(server, "director"), slots=[slot(server, "a"), slot(server, "broken"), slot(server, "b")])
    c = LLMClient(s)
    events = []
    res = await Orchestrator(s, c).run("hola", on_event=lambda k, p: events.append(k))
    await c.close()
    assert [t.text for t in res.turns] == ["resp-a", "", "resp-b"]
    assert "boom" in res.turns[1].error
    assert res.final == "resp-director"
    director_call = [c for c in CALLS if c["body"]["model"] == "director"][0]
    assert "resp-a" in director_call["body"]["messages"][-1]["content"]
    assert "resp-b" in director_call["body"]["messages"][-1]["content"]
    assert events.count("turn") == 3 and "director" in events


async def test_rounds_replica(server):
    s = AppSettings(rounds=2, director=slot(server, "d"), slots=[slot(server, "a"), slot(server, "b")])
    c = LLMClient(s)
    res = await Orchestrator(s, c).run("pregunta")
    await c.close()
    assert len(res.turns) == 4
    second = [x for x in CALLS if x["body"]["model"] == "a"][1]["body"]["messages"][-1]["content"]
    assert "ronda anterior" in second and "resp-b" in second


async def test_no_table_director_alone(server):
    s = AppSettings(director=slot(server, "d"))
    c = LLMClient(s)
    res = await Orchestrator(s, c).run("hola")
    await c.close()
    assert res.turns == [] and res.final == "resp-d"


async def test_director_failure_is_reported(server):
    s = AppSettings(director=slot(server, "broken"), slots=[slot(server, "a")])
    c = LLMClient(s)
    res = await Orchestrator(s, c).run("x")
    await c.close()
    assert res.director_error and "boom" in res.final and res.turns[0].text == "resp-a"


async def test_reasoning_model_retries_without_temperature(server):
    s = AppSettings(director=slot(server, "reasoner"))
    c = LLMClient(s)
    res = await Orchestrator(s, c).run("x")
    await c.close()
    assert res.final == "resp-reasoner"


async def test_anthropic_protocol_and_images(server):
    d = slot(server, "claude-x", provider="anthropic")
    s = AppSettings(director=d)
    c = LLMClient(s)
    out = await c.generate(d, "sys", [{"role": "user", "content": "q"}], [("image/jpeg", "QUJD")])
    await c.close()
    call = CALLS[-1]
    assert out == "resp-claude" and call["path"] == "/v1/messages" and call["key"] == "k"
    assert call["body"]["system"] == "sys"
    assert call["body"]["messages"][0]["content"][0]["type"] == "image"


async def test_images_only_for_vision_slots(server):
    v, nv = slot(server, "v"), slot(server, "nv", vision=False)
    c = LLMClient(AppSettings(director=v))
    await c.generate(v, "", [{"role": "user", "content": "q"}], [("image/png", "QQ==")])
    await c.generate(nv, "", [{"role": "user", "content": "q"}], [("image/png", "QQ==")])
    await c.close()
    assert isinstance(CALLS[0]["body"]["messages"][-1]["content"], list)
    assert isinstance(CALLS[1]["body"]["messages"][-1]["content"], str)


async def test_missing_key_message():
    s = ModelSlot(provider="openai", model="x")
    c = LLMClient(AppSettings())
    with pytest.raises(LLMError, match="API key"):
        await c.generate(s, "", [{"role": "user", "content": "q"}])
    await c.close()


def test_err_text_handles_gemini_list_and_other_formats():
    from src.llm.client import _err_text
    gemini = '[{"error": {"code": 400, "message": "API key not valid.", "status": "INVALID_ARGUMENT"}}]'
    assert "API key not valid." in _err_text(400, gemini)
    assert "API key not valid." in _err_text(401, '{"error": {"message": "API key not valid."}}')
    assert "boom" in _err_text(500, '{"error": "boom"}')
    assert "texto plano" in _err_text(502, "texto plano")
    assert _err_text(400, "[]").startswith("HTTP 400")
