from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from src.core.roles import director_prompt, role_prompt
from src.core.settings import AppSettings, ModelSlot, clamp_rounds
from src.llm.client import Image, LLMClient

DIGEST_CHARS = 4000


@dataclass
class Turn:
    slot_id: str
    name: str
    round: int
    text: str = ""
    error: str = ""
    seconds: float = 0.0


@dataclass
class Result:
    turns: list[Turn] = field(default_factory=list)
    final: str = ""
    director_error: bool = False


Event = Callable[[str, object], Awaitable[None] | None]


def _digest(turns: list[Turn]) -> str:
    return "\n\n".join(f"### {t.name}\n{t.text[:DIGEST_CHARS]}" for t in turns if t.text)


class Orchestrator:
    def __init__(self, settings: AppSettings, client: LLMClient):
        self.s = settings
        self.client = client

    async def _emit(self, cb: Event | None, kind: str, payload: object) -> None:
        if cb:
            try:
                r = cb(kind, payload)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass

    def _system(self, slot: ModelSlot, base: str) -> str:
        return "\n\n".join(p for p in (self.s.global_system_prompt, base, slot.system_prompt) if p)

    async def _ask(self, slot: ModelSlot, rnd: int, user_text: str, history: list[dict], images: list[Image],
                   sem: asyncio.Semaphore, cb: Event | None) -> Turn:
        turn = Turn(slot.id, slot.label(), rnd)
        async with sem:
            await self._emit(cb, "start", turn)
            t0 = time.monotonic()
            try:
                turn.text = await self.client.generate(
                    slot, self._system(slot, role_prompt(slot.role)),
                    history + [{"role": "user", "content": user_text}], images)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                turn.error = str(e) or type(e).__name__
            turn.seconds = round(time.monotonic() - t0, 1)
        await self._emit(cb, "turn", turn)
        return turn

    async def _table(self, user_text: str, images: list[Image], history: list[dict],
                     on_event: Event | None) -> tuple[list[Turn], list[Turn]]:
        all_turns: list[Turn] = []
        last_round: list[Turn] = []
        slots = self.s.active_slots()
        if slots:
            sem = asyncio.Semaphore(max(1, self.s.max_parallel))
            for rnd in range(1, clamp_rounds(self.s.rounds) + 1):
                prompt = user_text
                if rnd > 1:
                    prompt = (f"{user_text}\n\n--- Respuestas de la mesa en la ronda anterior ---\n{_digest(last_round)}\n\n"
                              "Revisa lo anterior: corrige errores, rebate lo débil y refina tu postura.")
                last_round = list(await asyncio.gather(
                    *(self._ask(s, rnd, prompt, history, images, sem, on_event) for s in slots)))
                all_turns += last_round
        return all_turns, last_round

    async def consult(self, question: str) -> str:
        _, last = await self._table(question, [], [], None)
        if not self.s.active_slots():
            return "La mesa está vacía (no hay IAs activas)."
        parts = [f"### {t.name}\n{t.text[:DIGEST_CHARS]}" for t in last if t.text]
        parts += [f"### {t.name}\n(falló: {t.error})" for t in last if t.error]
        return "\n\n".join(parts) or "La mesa no devolvió respuestas."

    async def run(self, user_text: str, images: list[Image] | None = None,
                  history: list[dict] | None = None, on_event: Event | None = None) -> Result:
        images = images or []
        history = (history or [])[-self.s.history_messages:]
        res = Result()
        res.turns, last_round = await self._table(user_text, images, history, on_event)

        ok = [t for t in last_round if t.text]
        d = self.s.director
        prompt = user_text
        if ok:
            prompt += f"\n\n--- Respuestas de la mesa ---\n{_digest(ok)}\n\nRedacta la respuesta final."
        await self._emit(on_event, "director", None)
        try:
            res.final = await self.client.generate(
                d, self._system(d, director_prompt(self.s.director_role)), history + [{"role": "user", "content": prompt}], images)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            res.final = f"⚠️ El Director falló: {e}"
            res.director_error = True
        return res
