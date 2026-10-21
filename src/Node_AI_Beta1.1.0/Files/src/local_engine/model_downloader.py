from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import aiohttp


def hf_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo.strip('/')}/resolve/main/{filename.lstrip('/')}"


def list_models(models_dir: str) -> list[Path]:
    d = Path(models_dir)
    return sorted(d.glob("*.gguf")) if d.is_dir() else []


async def download_model(url: str, models_dir: str, filename: str | None = None,
                         progress: Callable[[float, int, int], None] | None = None,
                         hf_token: str = "") -> Path:
    name = Path(filename or url.split("?")[0].rsplit("/", 1)[-1]).name
    if not name.lower().endswith(".gguf"):
        raise ValueError("Solo se admiten archivos .gguf")
    dest_dir = Path(models_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest, part = dest_dir / name, dest_dir / (name + ".part")
    if dest.exists() and dest.stat().st_size > 0:
        if progress:
            progress(1.0, dest.stat().st_size, dest.stat().st_size)
        return dest

    headers = {"Authorization": f"Bearer {hf_token}"} if hf_token else {}
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                with open(part, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done / total if total else 0.0, done, total)
        if total and done != total:
            raise OSError(f"Descarga incompleta ({done} de {total} bytes)")
        os.replace(part, dest)
        return dest
    except BaseException:
        part.unlink(missing_ok=True)
        raise
