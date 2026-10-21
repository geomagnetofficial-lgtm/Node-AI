from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any
from urllib.parse import urlparse

import aiohttp

from src.llm.providers import get_provider
from src.local_engine.model_downloader import list_models as list_gguf_files

TIMEOUT_S = 15
MAX_BODY = 5_000_000
MAX_MODELS_LISTED = 5000
MAX_PAGES = 5
ANTHROPIC_VERSION = "2023-06-01"

_CACHE: dict[tuple[str, str, str, str], list[str]] = {}


class ModelListError(RuntimeError):
    pass


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _clean(ids: list[Any]) -> list[str]:
    seen: dict[str, None] = {}
    for raw in ids:
        if not isinstance(raw, str):
            continue
        name = raw.strip().removeprefix("models/")
        if name and len(name) <= 300:
            seen.setdefault(name, None)
    return sorted(seen, key=str.casefold)[:MAX_MODELS_LISTED]


def parse_openai_models(data: Any) -> list[str]:
    items: Any = data
    if isinstance(data, dict):
        items = data.get("data")
        if not isinstance(items, list):
            items = data.get("models")
    if not isinstance(items, list):
        raise ModelListError("La respuesta del servidor no tiene el formato esperado de lista de modelos.")
    ids: list[Any] = []
    for it in items:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict):
            ids.append(it.get("id") or it.get("name") or it.get("model"))
    return _clean(ids)


def parse_anthropic_models(data: Any) -> tuple[list[str], bool, str | None]:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ModelListError("La respuesta de Anthropic no tiene el formato esperado.")
    ids = [it.get("id") for it in data["data"] if isinstance(it, dict)]
    last = data.get("last_id")
    return _clean(ids), bool(data.get("has_more")), last if isinstance(last, str) else None


def filter_models(models: list[str], query: str) -> list[str]:
    terms = query.lower().split()
    if not terms:
        return list(models)
    return [m for m in models if all(t in m.lower() for t in terms)]


def _error_for(status: int, url: str, key: str, body: str) -> ModelListError:
    shown = url.split("?")[0]
    if status in (401, 403):
        return ModelListError(f"API key inválida o sin permiso para listar modelos (HTTP {status}).")
    if status == 429:
        return ModelListError("Límite de peticiones alcanzado (HTTP 429). Espera un momento y vuelve a intentarlo.")
    if status == 404:
        return ModelListError(f"Este servidor no ofrece lista de modelos ({shown} dio 404). Escribe el id del modelo a mano.")
    if status >= 500:
        return ModelListError(f"El servidor falló al listar modelos (HTTP {status}). Inténtalo más tarde.")
    return ModelListError(f"No se pudo listar los modelos (HTTP {status}).")


async def _get_json(session: aiohttp.ClientSession, url: str, headers: dict[str, str], key: str) -> Any:
    host = urlparse(url).netloc or url
    try:
        async with session.get(url, headers=headers) as r:
            if r.status >= 400:
                raise _error_for(r.status, url, key, "")
            if r.content_length and r.content_length > MAX_BODY:
                raise ModelListError("La respuesta del servidor es demasiado grande.")
            raw = await r.content.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ModelListError("La respuesta del servidor es demasiado grande.")
            try:
                return json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                raise ModelListError("El servidor no devolvió JSON válido al listar modelos.") from None
    except asyncio.TimeoutError:
        raise ModelListError(f"Tiempo de espera agotado ({TIMEOUT_S} s) al consultar {host}.") from None
    except aiohttp.InvalidURL:
        raise ModelListError("La URL base no es válida.") from None
    except aiohttp.ClientConnectionError:
        raise ModelListError(f"Sin conexión con {host}. Revisa tu red o que el servidor esté en marcha.") from None
    except aiohttp.ClientError as e:
        raise ModelListError(_scrub(f"Error de red al listar modelos ({type(e).__name__}).", key)) from None


async def list_models(provider_key: str, api_key: str, base_url: str = "", *, models_dir: str = "") -> list[str]:
    try:
        provider = get_provider(provider_key)
    except ValueError as e:
        raise ModelListError(str(e)) from None
    if provider.protocol == "gguf":
        return [str(p) for p in list_gguf_files(models_dir)] if models_dir else []
    key = (api_key or "").strip() or (os.environ.get(provider.env_key, "") if provider.env_key else "")
    if provider.needs_key and not key:
        raise ModelListError("Falta la API key: escríbela en su campo o defínela en .env / variable de entorno.")
    base = (base_url or provider.base_url).strip().rstrip("/")
    if not base:
        raise ModelListError("Indica la URL base del servidor para poder listar sus modelos.")
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if provider.protocol == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
            models: list[str] = []
            after: str | None = None
            for _ in range(MAX_PAGES):
                url = f"{base}/v1/models?limit=1000" + (f"&after_id={after}" if after else "")
                ids, more, after = parse_anthropic_models(await _get_json(session, url, headers, key))
                models += ids
                if not more or not after:
                    break
            return _clean(models)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return parse_openai_models(await _get_json(session, f"{base}/models", headers, key))


def _cache_key(provider_key: str, api_key: str, base_url: str, models_dir: str) -> tuple[str, str, str, str]:
    digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:16] if api_key else ""
    return provider_key, digest, base_url.rstrip("/"), models_dir


def cached_models(provider_key: str, api_key: str, base_url: str = "", *, models_dir: str = "") -> list[str] | None:
    return _CACHE.get(_cache_key(provider_key, api_key, base_url, models_dir))


async def get_models(provider_key: str, api_key: str, base_url: str = "", *, models_dir: str = "",
                     refresh: bool = False) -> list[str]:
    ck = _cache_key(provider_key, api_key, base_url, models_dir)
    if not refresh and ck in _CACHE:
        return list(_CACHE[ck])
    models = await list_models(provider_key, api_key, base_url, models_dir=models_dir)
    _CACHE[ck] = models
    return list(models)


def clear_cache() -> None:
    _CACHE.clear()
