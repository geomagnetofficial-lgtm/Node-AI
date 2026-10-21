from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

from src.agent import apps
from src.agent.desktop import Desktop, DesktopError
from src.agent.runner import AgentResult, AgentRunner, ApprovalRequest, StepEvent
from src.agent.safety import MODES
from src.core.library import USER_FILE, Library, set_library
from src.core.settings import SettingsStore, user_dir
from src.llm.client import LLMClient

ICON = {"done": "✓", "error": "✗", "denied": "⊘", "blocked": "⛔", "protocol": "?", "running": "…"}


def _clip(s: str, n: int = 300) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def check(settings) -> int:
    print("== Diagnóstico del modo agente ==")
    d = settings.director
    print(f"Director: {d.provider} / {d.model or '(sin modelo)'}  visión={'sí' if d.vision else 'NO'}")
    print(f"Workspace: {settings.agent.workspace}")
    print("\nProgramas detectados:")
    for k, v in apps.detect_all().items():
        print(f"  {k:<26} {v}")
    print("\nPantalla:")
    try:
        shot = Desktop(settings.agent.monitor, settings.agent.shot_width).screenshot()
        print(f"  OK  {shot.describe()}")
    except DesktopError as e:
        print(f"  ERROR  {e}")
        return 1
    return 0


async def amain(args) -> int:
    store = SettingsStore()
    s = store.settings
    set_library(Library(user_dir() / USER_FILE))
    if args.approval:
        s.agent.approval = args.approval
    if args.max_steps:
        s.agent.max_steps = args.max_steps
        s.agent.unlimited_steps = False
    if args.no_step_limit:
        s.agent.unlimited_steps = True
    if args.workspace:
        s.agent.workspace = args.workspace
    if args.grid:
        s.agent.grid = True
    if args.check:
        return check(s)
    if not args.task:
        print("Falta la tarea. Ejemplo: python -m src.agent \"lista los archivos del workspace\"")
        return 2
    if not s.director.model:
        print("El Director no tiene modelo configurado. Ábrelo en NodeAi > Configuración (o edita la configuración).")
        return 2

    shots_dir = Path(args.save_shots) if args.save_shots else None
    if shots_dir:
        shots_dir.mkdir(parents=True, exist_ok=True)
    state = {"all": False}

    def on_event(kind, p):
        if kind == "start":
            limit = "sin límite de pasos" if p.get("unlimited") else f"máx {p['max_steps']} pasos"
            print(f"▶ Agente iniciado · modo {p['mode']} · workspace {p['workspace']} · {limit}")
            if not p["vision"]:
                print("  ⚠ El Director no tiene visión activada: no verá la pantalla.")
        elif kind == "step" and isinstance(p, StepEvent):
            print(f"\n[{p.n}] {p.description}" + (f"\n    💭 {_clip(p.thought, 200)}" if p.thought else ""))
        elif kind == "observation" and isinstance(p, StepEvent):
            print(f"    {ICON.get(p.status, '·')} {_clip(p.text)}")
            if shots_dir and p.image:
                (shots_dir / f"paso_{p.n:02d}.jpg").write_bytes(base64.b64decode(p.image.b64))

    async def approve(req: ApprovalRequest) -> bool:
        if state["all"]:
            return True
        print(f"\n⚠ Permiso requerido [{req.level}] paso {req.step}: {req.description}\n  Motivo: {req.reason}")
        ans = (await _ainput("  ¿Permitir? [s]í / [N]o / [t]odo lo que queda: ")).strip().lower()
        if ans in ("t", "todo"):
            state["all"] = True
            return True
        return ans in ("s", "si", "sí", "y", "yes")

    async def ask_user(q: str) -> str:
        print(f"\n❓ El agente pregunta: {q}")
        return await _ainput("  Tu respuesta: ")

    client = LLMClient(s)
    try:
        res: AgentResult = await AgentRunner(s, client, on_event=on_event, approve=approve, ask_user=ask_user).run(args.task)
    finally:
        await client.close()
    print(f"\n■ {res.reason.upper()} tras {res.steps} paso(s)\n{res.final}")
    if res.log_path:
        print(f"\nRegistro: {res.log_path}")
    return 0 if res.reason == "finished" else 1


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="python -m src.agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="?", help="Qué quieres que haga el agente")
    ap.add_argument("--approval", choices=MODES, help="strict | balanced | autonomous")
    ap.add_argument("--max-steps", type=int)
    ap.add_argument("--no-step-limit", action="store_true", help="sin límite de pasos (paran finish, Ctrl+C y la detección de bucles)")
    ap.add_argument("--workspace")
    ap.add_argument("--grid", action="store_true", help="rejilla de coordenadas sobre la captura")
    ap.add_argument("--save-shots", metavar="DIR", help="guarda lo que ve el modelo en cada paso")
    ap.add_argument("--check", action="store_true", help="diagnóstico y salir")
    try:
        sys.exit(asyncio.run(amain(ap.parse_args())))
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.")
        sys.exit(130)


if __name__ == "__main__":
    main()
