from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Callable

import flet as ft

from src.agent import apps
from src.agent.desktop import Desktop, DesktopError
from src.agent.runner import AgentResult, AgentRunner, ApprovalRequest, StepEvent
from src.core.i18n import t

if TYPE_CHECKING:  # pragma: no cover
    from src.ui import NodeAiApp

C = ft.Colors
MAX_LIVE_IMAGES = 4
APPROVAL_OPTIONS = [("strict", "Estricto: pide permiso para casi todo"),
                    ("balanced", "Equilibrado: solo pide lo riesgoso (recomendado)"),
                    ("autonomous", "Autónomo: no pide permiso (lo peligroso sigue bloqueado)")]
REASONS = {"finished": ("✓ Terminado", C.GREEN), "max_steps": ("⏹ Límite de pasos alcanzado", C.AMBER),
           "loop": ("⏹ Detenido: bucle detectado", C.AMBER),
           "cancelled": ("■ Detenido por el usuario", C.AMBER), "aborted": ("■ Abortado (ratón en la esquina)", C.AMBER),
           "error": ("⚠ Error", C.ERROR), "protocol": ("⚠ El modelo no siguió el formato", C.ERROR)}
STATUS_ICON = {"done": (ft.Icons.CHECK_CIRCLE, C.GREEN), "error": (ft.Icons.ERROR, C.ERROR),
               "denied": (ft.Icons.BLOCK, C.AMBER), "blocked": (ft.Icons.GPP_BAD, C.ERROR),
               "protocol": (ft.Icons.HELP, C.AMBER)}


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


class AgentUI:
    def __init__(self, app: "NodeAiApp"):
        self.app = app
        self.desktop_factory: Callable[[], Desktop] | None = None
        self.banner_text = ft.Text("", size=12, expand=True)
        self.banner = ft.Container(
            visible=False, padding=ft.Padding.symmetric(horizontal=12, vertical=6), bgcolor=C.TERTIARY_CONTAINER,
            content=ft.Row([ft.Icon(ft.Icons.SMART_TOY, size=16), self.banner_text]))
        self.diag_box = ft.Column(spacing=2)
        self._allow_all = False
        self._pending: asyncio.Future | None = None
        self.sync()

    def sync(self) -> None:
        on = self.app.s.agent.enabled
        self.banner.visible = on
        self.banner_text.value = t("Modo agente activo: el Director puede ver y controlar este equipo. "
                                   "Para abortar: botón Detener o ratón a la esquina superior izquierda.")
        inp = getattr(self.app, "input", None)
        if inp is not None:
            inp.hint_text = (t("Describe la tarea para el agente…  (Enter envía)") if on else
                             t("Escribe tu mensaje…  (Enter envía · Shift+Enter nueva línea)"))

    def _changed(self) -> None:
        self.sync()
        self.app._rebuild_all()

    def _step_mode_changed(self, e) -> None:
        self.app.s.agent.unlimited_steps = e.control.value == "unlimited"
        self.app.touch()
        self.app._rebuild_all()

    def step_selector(self) -> ft.Control:
        a, app = self.app.s.agent, self.app
        limited = not a.unlimited_steps
        steps = app._int_field(t("Máx. pasos"), a, "max_steps", lo=1, width=130)
        steps.disabled = not limited
        group = ft.RadioGroup(
            value="limit" if limited else "unlimited", on_change=self._step_mode_changed,
            content=ft.Column(spacing=2, controls=[
                ft.Row([ft.Radio(value="limit", label=t("Límite de pasos")), steps], vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ft.Radio(value="unlimited", label=t("Sin límite de pasos"))]))
        out: list[ft.Control] = [group]
        if not limited:
            out.append(ft.Text(t("Sin límite: el agente se detiene al terminar la tarea (finish), con el botón Detener, "
                                 "al mover el ratón a la esquina superior izquierda, si detecta un bucle o si el modelo falla "
                                 "3 veces seguidas con el formato. Lo bloqueado por seguridad sigue bloqueado."),
                               size=12, color=C.ON_SURFACE_VARIANT))
            out.append(ft.Row(wrap=True, controls=[
                app._int_field(t("Repeticiones iguales = bucle"), a, "loop_repeat", lo=2, width=220),
                app._int_field(t("Acciones sin cambio visible = bucle"), a, "stall_limit", lo=2, width=260)]))
        return ft.Column(spacing=6, controls=out)

    def config_section(self) -> list[ft.Control]:
        a, app = self.app.s.agent, self.app
        approval = [(k, t(v)) for k, v in APPROVAL_OPTIONS]
        return [
            ft.Text(t("El Director ve la pantalla (si su modelo admite imágenes) y usa herramientas: ratón, teclado, "
                      "terminal, archivos, 7-Zip/WinRAR y compilación con Visual Studio. Cada acción pasa por la política "
                      "de seguridad; lo destructivo se bloquea siempre."), size=12, color=C.ON_SURFACE_VARIANT),
            app._sw(t("Activar modo agente en el chat"), a, "enabled", on_change=self._changed),
            self.step_selector(),
            app._dd(t("Aprobación de acciones"), a, "approval", approval),
            app._tf(t("Carpeta de trabajo (workspace)"), a, "workspace",
                    hint=t("Los archivos se crean aquí; escribir fuera pide permiso")),
            ft.Row(wrap=True, controls=[
                app._tf(t("Ancho de captura (px)"), a, "shot_width", number=int, width=170),
                app._tf(t("Espera mínima tras acción (s)"), a, "settle_s", number=float, width=210),
                app._tf(t("Espera máxima a que la pantalla se estabilice (s)"), a, "settle_max_s", number=float, width=300),
                app._tf(t("Monitor (1 = principal)"), a, "monitor", number=int, width=170),
                app._tf(t("Timeout de comandos (s)"), a, "shell_timeout_s", number=int, width=190)]),
            app._sw(t("Comprobar si cada acción tuvo efecto (compara capturas)"), a, "verify_actions"),
            app._sw(t("Rejilla de coordenadas sobre la captura (ayuda a modelos con mala puntería)"), a, "grid"),
            ft.Row([ft.OutlinedButton(content=t("Probar pantalla y detectar programas"), icon=ft.Icons.FIND_IN_PAGE,
                                      on_click=lambda e: asyncio.create_task(self.diagnose()))]),
            self.diag_box]

    async def diagnose(self) -> None:
        a = self.app.s.agent
        self.diag_box.controls = [ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2), ft.Text(t("Comprobando…"))])]
        self.app.page.update()
        found = await asyncio.to_thread(apps.detect_all)
        try:
            shot = await asyncio.to_thread(Desktop(a.monitor, a.shot_width).screenshot)
            screen = (True, t("Pantalla OK · la captura que verá el modelo mide {w}x{h}px", w=shot.width, h=shot.height))
        except DesktopError as e:
            screen = (False, str(e))
        rows = [(screen[0], screen[1])] + [(v != "no encontrado", f"{k}: {t(v) if v == 'no encontrado' else v}") for k, v in found.items()]
        self.diag_box.controls = [ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE if ok else ft.Icons.WARNING,
                                                  color=C.GREEN if ok else C.AMBER, size=16),
                                          ft.Text(t, size=12, selectable=True, expand=True)]) for ok, t in rows]
        self.app.page.update()

    async def _dialog(self, title: str, body: list[ft.Control], buttons: list[tuple[str, object, bool]]):
        try:
            return await self.app.dialog(title, body, buttons, front=True, on_future=lambda f: setattr(self, "_pending", f))
        finally:
            self._pending = None

    async def approve(self, req: ApprovalRequest) -> bool:
        if self._allow_all:
            return True
        color = C.ERROR if req.level == "risky" else C.AMBER
        ans = await self._dialog(
            t("El agente pide permiso"),
            [ft.Text(req.description, selectable=True, weight=ft.FontWeight.BOLD),
             ft.Text(t("Nivel: {level} — {reason}", level=req.level, reason=req.reason) if req.reason
                     else t("Nivel: {level}", level=req.level), size=12, color=color)],
            [(t("Denegar"), False, False), (t("Permitir todo lo que queda"), "all", False), (t("Permitir"), True, True)])
        if ans == "all":
            self._allow_all = True
            return True
        return bool(ans)

    async def ask_user(self, question: str) -> str:
        field = ft.TextField(autofocus=True, multiline=True, min_lines=1, max_lines=5)
        await self._dialog(t("El agente pregunta"), [ft.Text(question, selectable=True), field],
                           [(t("Responder"), "ok", True)])
        return (field.value or "").strip() or "(sin respuesta)"

    async def run(self, plain_text: str, prompt: str, images: list) -> None:
        app, _cfg = self.app, self.app.s.agent
        self._allow_all = False
        status = ft.Text(t("Agente iniciando…"), italic=True, expand=True)
        steps = ft.Column(spacing=2)
        head = ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2), status])
        body = ft.Column([head, steps])
        row = app._bubble(body, False)
        app.messages.controls.append(row)
        app._set_running(True)
        app.page.update()

        tiles: dict[int, tuple[ft.ExpansionTile, ft.Column]] = {}
        live_images: list[tuple[ft.Column, ft.Control]] = []
        state = {"prev": None}

        def tile_for(ev: StepEvent):
            if ev.n in tiles:
                return tiles[ev.n]
            icon = ft.ProgressRing(width=16, height=16, stroke_width=2)
            col = ft.Column(spacing=6)
            tile = ft.ExpansionTile(leading=icon, title=ft.Text(f"{ev.n} · {_clip(ev.description or ev.action, 90)}", size=13),
                                    subtitle=ft.Text(_clip(ev.thought, 140), size=11, color=C.ON_SURFACE_VARIANT)
                                    if ev.thought else None,
                                    controls=[ft.Container(padding=10, content=col)])
            tiles[ev.n] = (tile, col)
            steps.controls.append(tile)
            return tiles[ev.n]

        async def on_event(kind: str, p) -> None:
            if kind == "start":
                note = "" if p["vision"] else "  " + t("⚠ el Director no tiene visión activada")
                limit = t("sin límite de pasos") if p.get("unlimited") else t("máx. {n} pasos", n=p["max_steps"])
                status.value = t("Agente trabajando… ({limit} · {mode})", limit=limit, mode=p["mode"]) + note
            elif kind == "step":
                tile_for(p)
                status.value = t("Paso {n}: {desc}", n=p.n, desc=_clip(p.description or p.action, 70))
            elif kind == "observation":
                tile, col = tile_for(p)
                ic, colr = STATUS_ICON.get(p.status, (ft.Icons.CHECK_CIRCLE, C.GREEN))
                tile.leading = ft.Icon(ic, color=colr, size=18)
                col.controls = [ft.Text(_clip(p.text, 1500) or t("(sin salida)"), selectable=True, size=12,
                                        color=C.ERROR if p.status in ("error", "blocked") else None)]
                if p.image is not None:
                    img = ft.Image(src=base64.b64decode(p.image.b64), width=460, fit=ft.BoxFit.CONTAIN)
                    col.controls.append(img)
                    live_images.append((col, img))
                    while len(live_images) > MAX_LIVE_IMAGES:
                        c, i = live_images.pop(0)
                        if i in c.controls:
                            c.controls.remove(i)
                if state["prev"] is not None:
                    state["prev"].expanded = False
                tile.expanded = True
                state["prev"] = tile
            app.page.update()

        result: AgentResult | None = None
        runner = AgentRunner(app.s, app.client, desktop=self.desktop_factory() if self.desktop_factory else None,
                             on_event=on_event, approve=self.approve, ask_user=self.ask_user)
        try:
            result = await runner.run(prompt, images)
        except asyncio.CancelledError:
            result = AgentResult(t("Detenido por el usuario."), len(tiles), "cancelled")
        except Exception as ex:
            result = AgentResult(t("Error inesperado: {error}", error=str(ex)), len(tiles), "error")
        finally:
            if self._pending and not self._pending.done():
                self._pending.cancel()
            label, color = REASONS.get(result.reason if result else "error", ("", None))
            label = t(label) if label else label
            final = [ft.Text(label, color=color, weight=ft.FontWeight.BOLD, size=12),
                     ft.Markdown(result.final if result else "", selectable=True,
                                 extension_set=ft.MarkdownExtensionSet.GITHUB_WEB)]
            if tiles:
                final.append(ft.ExpansionTile(title=ft.Text(t("Pasos del agente: {n}", n=len(tiles)), size=13),
                                              controls=[steps]))
            if result and result.log_path:
                final.append(ft.Text(t("Registro: {path}", path=result.log_path), size=10, color=C.ON_SURFACE_VARIANT, selectable=True))
            if row in app.messages.controls:
                app.messages.controls[app.messages.controls.index(row)] = app._bubble(ft.Column(final), False)
            if result and result.reason == "finished":
                app.history += [{"role": "user", "content": plain_text or "(tarea del agente)"},
                                {"role": "assistant", "content": result.final}]
                app.history = app.history[-40:]
            app.task = None
            app._set_running(False)
            app.page.update()
