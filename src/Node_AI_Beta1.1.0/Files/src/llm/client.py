from __future__ import annotations

import asyncio
import os

import aiohttp

from src.core.settings import AppSettings, ModelSlot
from src.llm.providers import get_provider
from src.local_engine.inference_engine import EngineCache, LocalEngineError

Image = tuple[str, str]


class LLMError(RuntimeError):
    pass


def _err_text(status: int, body: str) -> str:
    import json
    msg = None
    try:
        data = json.loads(body)
        if isinstance(data, list):
            data = data[0] if data else {}
        err = data.get("error", data) if isinstance(data, dict) else data
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError, TypeError, IndexError):
        msg = None
    text = f"HTTP {status}: {(msg or body or 'sin detalle')[:300]}"
    if status in (401, 403):
        text += " (revisa que la API key sea válida, tenga permisos/saldo y el modelo esté habilitado para tu cuenta)"
    elif status == 404:
        text += " (revisa el nombre del modelo y la URL base)"
    return text


class LLMClient:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.engines = EngineCache(settings.hardware)
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.settings.timeout_s))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self.engines.close()

    async def generate(self, slot: ModelSlot, system: str, messages: list[dict],
                       images: list[Image] | None = None) -> str:
        provider = get_provider(slot.provider)
        images = images if (images and slot.vision) else []
        if provider.protocol == "gguf":
            return await self._gguf(slot, system, messages, images)
        key = slot.api_key or os.environ.get(provider.env_key, "")
        if provider.needs_key and not key:
            raise LLMError(f"Falta la API key de {provider.label} (Configuración o variable {provider.env_key}).")
        base = (slot.base_url or provider.base_url).rstrip("/")
        if not base:
            raise LLMError("Falta la URL base del proveedor.")
        if not slot.model:
            raise LLMError("Falta el nombre del modelo.")
        if provider.protocol == "anthropic":
            return await self._anthropic(slot, base, key, system, messages, images)
        return await self._openai(slot, provider.key, base, key, system, messages, images)

    async def _post(self, url: str, headers: dict, payload: dict) -> dict:
        last: Exception | None = None
        for attempt in range(3):
            try:
                async with self._http().post(url, json=payload, headers=headers) as r:
                    body = await r.text()
                    if r.status == 200:
                        import json
                        return json.loads(body)
                    if r.status in (429, 500, 502, 503, 504, 529) and attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise LLMError(_err_text(r.status, body))
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                last = e
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
        raise LLMError(f"No se pudo conectar con {url} ({type(last).__name__}). ¿Está el servidor en marcha?")

    async def _openai(self, slot, pkey, base, key, system, messages, images) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [dict(m) for m in messages]
        if images and msgs and msgs[-1]["role"] == "user":
            parts = [{"type": "text", "text": msgs[-1]["content"]}]
            parts += [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}} for mime, b64 in images]
            msgs[-1]["content"] = parts
        payload = {"model": slot.model, "messages": msgs, "temperature": slot.temperature, "top_p": slot.top_p,
                   ("max_completion_tokens" if pkey == "openai" else "max_tokens"): slot.max_tokens}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        url = f"{base}/chat/completions"
        try:
            data = await self._post(url, headers, payload)
        except LLMError as e:
            if "temperature" in str(e) or "top_p" in str(e):
                payload.pop("temperature", None)
                payload.pop("top_p", None)
                data = await self._post(url, headers, payload)
            else:
                raise
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("Respuesta inesperada del servidor.") from None
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip()

    async def _anthropic(self, slot, base, key, system, messages, images) -> str:
        msgs = [dict(m) for m in messages]
        if images and msgs and msgs[-1]["role"] == "user":
            parts = [{"type": "image", "source": {"type": "base64", "media_type": m, "data": b}} for m, b in images]
            parts.append({"type": "text", "text": msgs[-1]["content"]})
            msgs[-1]["content"] = parts
        payload = {"model": slot.model, "max_tokens": slot.max_tokens, "messages": msgs,
                   "temperature": slot.temperature}
        if system:
            payload["system"] = system
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        data = await self._post(f"{base}/v1/messages", headers, payload)
        try:
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text").strip()
        except (KeyError, TypeError):
            raise LLMError("Respuesta inesperada de Anthropic.") from None

    async def _gguf(self, slot, system, messages, images) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [dict(m) for m in messages]
        if images and msgs[-1]["role"] == "user":
            msgs[-1]["content"] = f"[{len(images)} imagen(es) omitidas: el motor GGUF integrado no procesa imágenes]\n" + msgs[-1]["content"]
        try:
            return await self.engines.chat(slot.gguf_path, msgs, slot.temperature, slot.top_p, slot.max_tokens)
        except LocalEngineError as e:
            raise LLMError(str(e)) from e
