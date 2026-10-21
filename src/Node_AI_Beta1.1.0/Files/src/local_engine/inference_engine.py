from __future__ import annotations

import asyncio
import gc
import os
import threading
from collections import OrderedDict

from src.core.settings import HardwareSettings
from src.local_engine import hardware_check


class LocalEngineError(RuntimeError):
    pass


class LocalEngine:
    def __init__(self, model_path: str, hw_cfg: HardwareSettings):
        try:
            from llama_cpp import Llama
        except ImportError as e:
            raise LocalEngineError(
                "llama-cpp-python no está instalado. Instálalo (ver Configuración > Hardware) "
                "o usa Ollama / LM Studio desde la pestaña Local.") from e
        if not os.path.isfile(model_path):
            raise LocalEngineError(f"No se encuentra el modelo GGUF: {model_path}")
        hw = hardware_check.detect()
        layers, self.note = hardware_check.resolve_gpu_layers(hw_cfg.device, hw_cfg.n_gpu_layers, hw)
        threads = hw_cfg.n_threads or max(1, hw.cores_physical)
        self._lock = threading.Lock()
        self.llm = Llama(model_path=model_path, n_ctx=hw_cfg.n_ctx, n_threads=threads,
                         n_gpu_layers=layers, verbose=False)

    def chat(self, messages: list[dict], temperature: float, top_p: float, max_tokens: int) -> str:
        with self._lock:
            out = self.llm.create_chat_completion(messages=messages, temperature=temperature,
                                                  top_p=top_p, max_tokens=max_tokens)
        return (out["choices"][0]["message"].get("content") or "").strip()

    def close(self) -> None:
        try:
            self.llm.close()
        except Exception:
            pass


class EngineCache:

    def __init__(self, hw_cfg: HardwareSettings):
        self.hw_cfg = hw_cfg
        self._engines: OrderedDict[str, LocalEngine] = OrderedDict()
        self._load_lock = asyncio.Lock()

    async def get(self, path: str) -> LocalEngine:
        async with self._load_lock:
            if path in self._engines:
                self._engines.move_to_end(path)
                return self._engines[path]
            while len(self._engines) >= max(1, self.hw_cfg.max_loaded_models):
                _, old = self._engines.popitem(last=False)
                old.close()
                del old
                gc.collect()
            engine = await asyncio.to_thread(LocalEngine, path, self.hw_cfg)
            self._engines[path] = engine
            return engine

    async def chat(self, path: str, messages: list[dict], temperature: float, top_p: float, max_tokens: int) -> str:
        engine = await self.get(path)
        return await asyncio.to_thread(engine.chat, messages, temperature, top_p, max_tokens)

    def close(self) -> None:
        for e in self._engines.values():
            e.close()
        self._engines.clear()
