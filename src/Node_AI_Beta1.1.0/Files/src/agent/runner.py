from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.agent import safety
from src.agent.desktop import Desktop, Shot, UserAbort
from src.agent.loops import LoopDetector
from src.agent.protocol import ProtocolError, parse_action, system_prompt
from src.agent.tools import Observation, Toolbox
from src.core.orchestrator import Orchestrator
from src.core.settings import AppSettings, app_dir
from src.llm.client import Image, LLMClient

KEEP_PAIRS = 14
MAX_PROTOCOL_ERRORS = 3
RAW_KEEP = 8000


@dataclass
class ApprovalRequest:
    step: int
    action: str
    args: dict
    level: str
    reason: str
    description: str


@dataclass
class StepEvent:
    n: int
    action: str = ""
    args: dict = field(default_factory=dict)
    thought: str = ""
    description: str = ""
    level: str = safety.SAFE
    status: str = "running"
    text: str = ""
    image: Shot | None = None


@dataclass
class AgentResult:
    final: str
    steps: int
    reason: str
    log_path: str = ""


async def _call(cb: Callable | None, *a: Any) -> Any:
    if cb is None:
        return None
    r = cb(*a)
    return await r if asyncio.iscoroutine(r) else r


def _redact(args: dict) -> dict:
    return {k: ("***" if k == "password" else v) for k, v in (args or {}).items()}


class AgentRunner:
    def __init__(self, settings: AppSettings, client: LLMClient, *, desktop: Desktop | None = None,
                 on_event: Callable | None = None, approve: Callable[[ApprovalRequest], Awaitable[bool] | bool] | None = None,
                 ask_user: Callable[[str], Awaitable[str]] | None = None,
                 consult: Callable[[str], Awaitable[str]] | None = None, log_dir: Path | None = None):
        self.s = settings
        self.cfg = settings.agent
        self.client = client
        self.on_event = on_event
        self.approve = approve
        self.ws = Path(self.cfg.workspace or (app_dir() / "workspace"))
        self.desktop = desktop or Desktop(self.cfg.monitor, self.cfg.shot_width, self.cfg.grid)
        self.tools = Toolbox(self.cfg, self.ws, self.desktop,
                             consult=consult or Orchestrator(settings, client).consult, ask_user=ask_user)
        self.log_dir = log_dir if log_dir is not None else app_dir() / "agent_logs"
        self._log = None

    async def _emit(self, kind: str, payload: Any) -> None:
        try:
            await _call(self.on_event, kind, payload)
        except Exception:
            pass

    def _open_log(self) -> str:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
            self._log = path.open("a", encoding="utf-8")
            return str(path)
        except OSError:
            return ""

    def _write_log(self, **row: Any) -> None:
        if self._log:
            try:
                self._log.write(json.dumps({"t": time.strftime("%H:%M:%S"), **row}, ensure_ascii=False, default=str) + "\n")
                self._log.flush()
            except OSError:
                pass

    async def _ask_permission(self, req: ApprovalRequest) -> bool:
        if self.approve is None:
            return False
        try:
            return bool(await _call(self.approve, req))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    @staticmethod
    def _view(msgs: list[dict]) -> list[dict]:
        if len(msgs) <= 1 + 2 * KEEP_PAIRS:
            return msgs
        dropped = (len(msgs) - 1 - 2 * KEEP_PAIRS) // 2
        first = dict(msgs[0])
        first["content"] += f"\n\n(Se omitieron {dropped} pasos antiguos para ahorrar contexto.)"
        return [first] + msgs[-2 * KEEP_PAIRS:]

    async def run(self, task: str, images: list[Image] | None = None) -> AgentResult:
        log_path = self._open_log()
        try:
            res = await self._loop(task, images or [], log_path)
        except asyncio.CancelledError:
            await self._emit("done", AgentResult("Detenido por el usuario.", 0, "cancelled", log_path))
            raise
        finally:
            if self._log:
                self._log.close()
        await self._emit("done", res)
        return res

    async def _loop(self, task: str, user_images: list[Image], log_path: str) -> AgentResult:
        cfg, ws = self.cfg, self.ws
        director = dataclasses.replace(self.s.director, temperature=cfg.temperature)
        system = "\n\n".join(p for p in (self.s.global_system_prompt,
                                         system_prompt(ws, has_vision=director.vision, unlimited=cfg.unlimited_steps),
                                         director.system_prompt) if p)
        await self._emit("start", {"task": task, "workspace": str(ws), "mode": cfg.approval,
                                   "vision": director.vision, "unlimited": cfg.unlimited_steps,
                                   "max_steps": None if cfg.unlimited_steps else cfg.max_steps})
        self._write_log(event="start", task=task, mode=cfg.approval, workspace=str(ws))

        first = f"TAREA DEL USUARIO:\n{task}"
        cur_images: list[Image] = list(user_images)
        if user_images:
            first += f"\n\n(El usuario adjuntó {len(user_images)} imagen(es); solo las ves en este primer turno.)"
        obs0 = await self.tools.execute("screenshot", {})
        if obs0.image:
            first += f"\n\n{obs0.image.describe()}"
            cur_images.append((obs0.image.mime, obs0.image.b64))
        else:
            first += f"\n\n(Sin captura inicial: {obs0.text})"
        msgs: list[dict] = [{"role": "user", "content": first}]

        proto_errors = 0
        recent: list[str] = []
        last_thought = ""
        limit = None if cfg.unlimited_steps else max(1, int(cfg.max_steps))
        detector = LoopDetector(cfg.loop_repeat, cfg.stall_limit) if limit is None else None
        n = 0
        while limit is None or n < limit:
            n += 1
            raw = ""
            for attempt in (1, 2):
                try:
                    raw = await self.client.generate(director, system, self._view(msgs), cur_images)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    if attempt == 2:
                        msg = f"El Director falló: {e}"
                        self._write_log(event="llm_error", error=str(e))
                        return AgentResult(msg, n - 1, "error", log_path)
                    await asyncio.sleep(2)
            cur_images = []
            msgs.append({"role": "assistant", "content": raw[:RAW_KEEP] or "(vacío)"})

            try:
                action, args, thought = parse_action(raw)
            except ProtocolError as e:
                proto_errors += 1
                ev = StepEvent(n, "(formato inválido)", status="protocol", text=str(e))
                await self._emit("observation", ev)
                self._write_log(event="protocol_error", n=n, error=str(e), raw=raw[:300])
                if proto_errors >= MAX_PROTOCOL_ERRORS:
                    return AgentResult("El modelo no respondió con el formato de acción esperado "
                                       f"({MAX_PROTOCOL_ERRORS} veces seguidas). Prueba con un modelo más capaz como Director.",
                                       n, "protocol", log_path)
                msgs.append({"role": "user", "content": f"ERROR de formato: {e}"})
                continue
            proto_errors = 0
            last_thought = thought or last_thought

            verdict = safety.classify(action, args, ws)
            desc = safety.describe(action, args)
            ev = StepEvent(n, action, _redact(args), thought, desc, verdict.level)
            if action == "finish":
                final = str(args.get("summary") or "Tarea terminada.")
                ev.status, ev.text = "done", final
                await self._emit("step", ev)
                self._write_log(event="finish", n=n, summary=final)
                return AgentResult(final, n, "finished", log_path)
            await self._emit("step", ev)

            if verdict.level == safety.BLOCKED:
                ev.status, ev.text = "blocked", verdict.reason
                obs = Observation(f"BLOQUEADO por la política de seguridad: {verdict.reason} "
                                  "No lo intentes por otra vía; explica la situación al usuario o propón una alternativa segura.", error=True)
            elif safety.needs_approval(verdict, cfg.approval) and not await self._ask_permission(
                    ApprovalRequest(n, action, _redact(args), verdict.level, verdict.reason, desc)):
                ev.status, ev.text = "denied", "Denegado por el usuario"
                obs = Observation("El usuario DENEGÓ esta acción. No la repitas; busca otra forma o pregunta con ask_user.", error=True)
            else:
                try:
                    obs = await self.tools.execute(action, args)
                except UserAbort as e:
                    self._write_log(event="abort", n=n)
                    return AgentResult(str(e), n, "aborted", log_path)
                ev.status = "error" if obs.error else "done"
                ev.text, ev.image = obs.text, obs.image
            await self._emit("observation", ev)
            self._write_log(event="step", n=n, action=action, args=_redact(args), level=verdict.level,
                            status=ev.status, text=ev.text[:500])

            sig = f"{action}:{json.dumps(args, sort_keys=True, default=str)}"
            recent = (recent + [sig])[-3:]
            text = f"Resultado de {action}: {'ERROR: ' if obs.error else ''}{obs.text}".rstrip()
            if len(recent) == 3 and len(set(recent)) == 1:
                text += "\nAVISO: llevas 3 veces la misma acción. Cambia de estrategia."
            if obs.image:
                text += f"\n{obs.image.describe()}"
                cur_images = [(obs.image.mime, obs.image.b64)]
            if detector is not None:
                loop_msg = detector.update(action, args, obs)
                if loop_msg:
                    self._write_log(event="loop", n=n, action=action, reason=loop_msg)
                    return AgentResult(f"{loop_msg} Último razonamiento: {last_thought or '(ninguno)'}", n, "loop", log_path)
                if detector.hint():
                    text += "\n" + detector.hint()
            if limit is None:
                text += f"\n(paso {n})"
            else:
                text += f"\n(paso {n}/{limit})" + (" Quedan pocos pasos: termina con finish." if limit - n <= 2 else "")
            msgs.append({"role": "user", "content": text})

        return AgentResult(f"Alcancé el límite de {limit} pasos sin terminar. Último razonamiento: "
                           f"{last_thought or '(ninguno)'}", limit, "max_steps", log_path)
