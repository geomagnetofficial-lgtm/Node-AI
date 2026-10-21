from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.agent.tools import NO_EFFECT_WARN, Observation

TEXT_KEEP = 2000


@dataclass
class LoopDetector:
    repeat_limit: int = 5
    stall_limit: int = 8
    repeats: int = field(default=0, init=False)
    stalled: int = field(default=0, init=False)
    _last: tuple | None = field(default=None, init=False, repr=False)

    @staticmethod
    def fingerprint(action: str, args: dict[str, Any], obs: Observation) -> tuple:
        sig = obs.image.sig if obs.image is not None else None
        return (action, json.dumps(args, sort_keys=True, default=str), obs.text[:TEXT_KEEP], sig)

    def update(self, action: str, args: dict[str, Any], obs: Observation) -> str | None:
        fp = self.fingerprint(action, args, obs)
        self.repeats = self.repeats + 1 if fp == self._last else 1
        self._last = fp
        if obs.changed is True:
            self.stalled = 0
        elif obs.changed is False and not obs.error and action in NO_EFFECT_WARN:
            self.stalled += 1
        if self.repeats >= max(2, self.repeat_limit):
            return (f"Detuve al agente: repitió {self.repeats} veces seguidas la acción '{action}' con los mismos "
                    "argumentos y el mismo resultado (bucle).")
        if self.stalled >= max(2, self.stall_limit):
            return (f"Detuve al agente: {self.stalled} acciones seguidas sin ningún cambio visible en la pantalla "
                    "(bucle).")
        return None

    def hint(self) -> str:
        if self.stalled >= 3:
            return f"AVISO: llevas {self.stalled} acciones seguidas sin cambio visible en la pantalla. Cambia de estrategia o usa finish."
        return ""
