from __future__ import annotations

import pytest
from aiohttp import web

from src.llm import model_lister as ml
from src.llm.model_lister import ModelListError, filter_models, list_models

SECRET = "sk-test-clave-secreta-999"
HITS: list[dict] = []


async def openai_models(request: web.Request):
    HITS.append({"path": request.path, "auth": request.headers.get("Authorization")})
    auth = request.headers.get("Authorization", "")
    if auth == "Bearer bad":
        return web.json_response({"error": {"message": "invalid key bad"}}, status=401)
    if auth == "Bearer limit":
        return web.json_response({}, status=429)
    return web.json_response({"object": "list", "data": [
        {"id": "gpt-4o"}, {"id": "GPT-4o-mini"}, {"id": "models/gemini-2.0-flash"}, {"id": "gpt-4o"}, {"nope": 1}, "text-x"]})


async def ollama_models(request: web.Request):
    HITS.append({"path": request.path, "auth": request.headers.get("Authorization")})
    return web.json_response({"models": [{"name": "llama3:8b"}, {"model": "qwen2.5:7b"}]})


async def anthropic_models(request: web.Request):
    HITS.append({"path": request.path, "key": request.headers.get("x-api-key"),
                 "ver": request.headers.get("anthropic-version"), "after": request.query.get("after_id")})
    if request.headers.get("x-api-key") != "good":
        return web.json_response({"error": {"message": "bad"}}, status=401)
    if request.query.get("after_id") == "m2":
        return web.json_response({"data": [{"id": "claude-haiku"}], "has_more": False, "last_id": "m3"})
    return web.json_response({"data": [{"id": "claude-opus"}, {"id": "claude-sonnet"}], "has_more": True, "last_id": "m2"})


async def html_ep(request: web.Request):
    return web.Response(text="<html>no json</html>")


async def slow_ep(request: web.Request):
    import asyncio
    await asyncio.sleep(2)
    return web.json_response({"data": []})


async def not_found(request: web.Request):
    return web.Response(status=404)


@pytest.fixture
async def server(aiohttp_server):
    HITS.clear()
    ml.clear_cache()
    app = web.Application()
    app.router.add_get("/v1/models", openai_models)
    app.router.add_get("/ollama/v1/models", ollama_models)
    app.router.add_get("/anth/v1/models", anthropic_models)
    app.router.add_get("/html/models", html_ep)
    app.router.add_get("/slow/models", slow_ep)
    app.router.add_get("/none/models", not_found)
    return await aiohttp_server(app)


def url(server, path=""):
    return f"http://{server.host}:{server.port}{path}"


async def test_openai_protocol_parses_sorts_dedupes_and_sends_bearer(server):
    models = await list_models("openai", SECRET, url(server, "/v1"))
    assert models == ["gemini-2.0-flash", "gpt-4o", "GPT-4o-mini", "text-x"]
    assert HITS[0]["auth"] == f"Bearer {SECRET}" and HITS[0]["path"] == "/v1/models"


async def test_custom_and_other_openai_providers_use_same_protocol(server):
    assert "gpt-4o" in await list_models("custom", "k", url(server, "/v1"))
    assert "gpt-4o" in await list_models("groq", "k", url(server, "/v1"))


async def test_ollama_needs_no_key_and_parses_native_shape(server):
    models = await list_models("ollama", "", url(server, "/ollama/v1"))
    assert models == ["llama3:8b", "qwen2.5:7b"] and HITS[0]["auth"] is None


async def test_anthropic_headers_and_pagination(server):
    models = await list_models("anthropic", "good", url(server, "/anth"))
    assert models == ["claude-haiku", "claude-opus", "claude-sonnet"]
    assert HITS[0]["key"] == "good" and HITS[0]["ver"] == "2023-06-01" and HITS[1]["after"] == "m2"


async def test_gguf_lists_files_with_existing_helper(tmp_path):
    (tmp_path / "b.gguf").write_bytes(b"x")
    (tmp_path / "a.gguf").write_bytes(b"x")
    (tmp_path / "otro.txt").write_text("x")
    assert await list_models("gguf", "", models_dir=str(tmp_path)) == [str(tmp_path / "a.gguf"), str(tmp_path / "b.gguf")]
    assert await list_models("gguf", "", models_dir=str(tmp_path / "no-existe")) == []
    assert await list_models("gguf", "") == []


async def test_invalid_key_error_is_clear_and_hides_key(server):
    with pytest.raises(ModelListError) as e:
        await list_models("openai", "bad", url(server, "/v1"))
    assert "inválida" in str(e.value) and "bad" not in str(e.value).replace("inválida", "")
    with pytest.raises(ModelListError) as e:
        await list_models("anthropic", SECRET, url(server, "/anth"))
    assert SECRET not in str(e.value)


async def test_rate_limit_error(server):
    with pytest.raises(ModelListError, match="Límite de peticiones"):
        await list_models("openai", "limit", url(server, "/v1"))


async def test_not_found_and_bad_json(server):
    with pytest.raises(ModelListError, match="a mano"):
        await list_models("custom", "k", url(server, "/none"))
    with pytest.raises(ModelListError, match="JSON válido"):
        await list_models("custom", "k", url(server, "/html"))


async def test_connection_refused_message_has_no_key():
    with pytest.raises(ModelListError) as e:
        await list_models("openai", SECRET, "http://127.0.0.1:9/v1")
    assert "Sin conexión" in str(e.value) and SECRET not in str(e.value)


async def test_timeout_is_reported(server, monkeypatch):
    monkeypatch.setattr(ml, "TIMEOUT_S", 0.3)
    with pytest.raises(ModelListError, match="Tiempo de espera"):
        await list_models("custom", "k", url(server, "/slow"))


async def test_missing_key_and_missing_base_url(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelListError, match="Falta la API key"):
        await list_models("openai", "")
    with pytest.raises(ModelListError, match="URL base"):
        await list_models("custom", "k", "")
    with pytest.raises(ModelListError, match="desconocido"):
        await list_models("nope", "k")


async def test_env_var_key_is_used_when_field_empty(server, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    await list_models("openai", "", url(server, "/v1"))
    assert HITS[0]["auth"] == "Bearer from-env"


async def test_session_cache_avoids_repeated_requests(server):
    base = url(server, "/v1")
    a = await ml.get_models("openai", "k1", base)
    b = await ml.get_models("openai", "k1", base)
    assert a == b and len(HITS) == 1
    assert ml.cached_models("openai", "k1", base) == a
    await ml.get_models("openai", "k1", base, refresh=True)
    assert len(HITS) == 2
    await ml.get_models("openai", "otra-clave", base)
    assert len(HITS) == 3
    assert all("k1" not in repr(k) for k in ml._CACHE)


def test_filter_models():
    ms = ["gpt-4o", "GPT-4o-mini", "claude-opus-4", "gemini-flash"]
    assert filter_models(ms, "") == ms
    assert filter_models(ms, "GPT") == ["gpt-4o", "GPT-4o-mini"]
    assert filter_models(ms, "4o mini") == ["GPT-4o-mini"]
    assert filter_models(ms, "zzz") == []


def test_parse_shapes():
    assert ml.parse_openai_models(["b", "a"]) == ["a", "b"]
    assert ml.parse_openai_models({"data": [{"id": "x"}]}) == ["x"]
    with pytest.raises(ModelListError):
        ml.parse_openai_models({"error": "x"})
    with pytest.raises(ModelListError):
        ml.parse_anthropic_models([])
