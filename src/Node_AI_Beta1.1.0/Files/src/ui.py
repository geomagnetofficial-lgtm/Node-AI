from __future__ import annotations

import asyncio
import os
import time

import flet as ft

from src.core.i18n import LANGS, set_language, t
from src.core.library import USER_FILE, Library, set_library
from src.core.orchestrator import Orchestrator, Result, Turn
from src.core.settings import MAX_MODELS, MAX_ROUNDS, ROUNDS_WARN, ModelSlot, SettingsStore, clamp_rounds, parse_int, user_dir
from src.llm.client import LLMClient
from src.llm.providers import PROVIDERS, providers_for
from src.local_engine import hardware_check
from src.local_engine.model_downloader import download_model, hf_url, list_models
from src.multimodal.attachments import Attachment, build_prompt, load_attachment
from src.ui_agent import AgentUI
from src.ui_library import LibraryUI
from src.ui_models import ModelPicker

WIDE = 900
COPYRIGHT = "NodeAi · Geo Magnet Studio · © 2026 Geo Magnet. Todos los derechos reservados."
C = ft.Colors
CONFIG_TABS = [("general", ft.Icons.TUNE, "General"), ("director", ft.Icons.SMART_TOY, "Director"),
               ("mesa", ft.Icons.GROUPS, "Mesa de IAs"), ("agente", ft.Icons.SUPPORT_AGENT, "Agente"),
               ("biblioteca", ft.Icons.LIBRARY_BOOKS, "Biblioteca"), ("local", ft.Icons.MEMORY, "Local y hardware")]


class NodeAiApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.store = SettingsStore()
        self.s = self.store.settings
        set_language(self.s.language)
        self.library = Library(user_dir() / USER_FILE)
        set_library(self.library)
        self.client = LLMClient(self.s)
        self.hw: hardware_check.Hardware | None = None
        self.attachments: list[Attachment] = []
        self.history: list[dict] = []
        self.task: asyncio.Task | None = None
        self.view = "chat"
        self.cfg_tab = "general"
        self.open_tiles: set[str] = set()
        self.slot_titles: dict[str, ft.Text] = {}
        self.table_count = ft.Text("", size=12, color=C.ON_SURFACE_VARIANT)
        self.agent_ui = AgentUI(self)
        self.lib_ui = LibraryUI(self)
        self.model_picker = ModelPicker(self)

    def toast(self, msg: str) -> None:
        self.page.show_dialog(ft.SnackBar(content=ft.Text(msg)))

    def touch(self) -> None:
        self.store.save_soon()

    async def dialog(self, title: str, body: list[ft.Control], buttons: list[tuple[str, object, bool]], *,
                     width: int = 480, front: bool = False, on_future=None):
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if on_future:
            on_future(fut)

        def choose(v):
            return lambda e: fut.done() or fut.set_result(v)

        acts = [(ft.FilledButton if hi else ft.TextButton)(content=txt, on_click=choose(v)) for txt, v, hi in buttons]
        self.page.show_dialog(ft.AlertDialog(modal=True, title=ft.Text(title), content=ft.Column(body, tight=True, width=width, scroll=ft.ScrollMode.AUTO),
                                             actions=acts))
        if front:
            try:
                w = getattr(self.page, "window", None)
                if w is not None:
                    await w.to_front()
            except Exception:
                pass
        try:
            return await fut
        finally:
            try:
                self.page.pop_dialog()
            except Exception:
                pass

    def _tf(self, label, obj, attr, *, password=False, multiline=False, number=None, hint=None,
            on_change=None, on_blur=None, width=None, expand=None):
        def handler(e):
            v = e.control.value
            if number:
                try:
                    v = number(v)
                except ValueError:
                    return
            setattr(obj, attr, v)
            self.touch()
            if on_change:
                on_change()
        kw = {}
        if multiline:
            kw.update(multiline=True, min_lines=2, max_lines=8)
        if number:
            kw["keyboard_type"] = ft.KeyboardType.NUMBER
        if width:
            kw["width"] = width
        if expand:
            kw["expand"] = expand
        return ft.TextField(label=label, value=str(getattr(obj, attr) or ""), hint_text=hint, dense=True,
                            password=password, can_reveal_password=password, on_change=handler,
                            on_blur=(lambda e: on_blur()) if on_blur else None, **kw)

    def _int_field(self, label, obj, attr, *, lo=1, hi=None, warn_over=None, warn_msg="", width=170, on_change=None):
        last = {"v": int(getattr(obj, attr))}
        field = ft.TextField(label=label, value=str(last["v"]), dense=True, width=width,
                             keyboard_type=ft.KeyboardType.NUMBER, input_filter=ft.NumbersOnlyInputFilter())

        def apply(final: bool) -> None:
            v = parse_int(field.value or "", lo)
            if v is None:
                if final:
                    field.value = str(last["v"])
                    self.toast(t("Valor no válido: escribe un número entero ≥ {lo}. Se restauró {v}.", lo=lo, v=last["v"]))
                    self.page.update()
                return
            if hi is not None and v > hi:
                v = hi
                field.value = str(hi)
                self.toast(t("El máximo permitido es {hi}; se ajustó.", hi=hi))
            elif final and field.value != str(v):
                field.value = str(v)
            changed = v != last["v"]
            last["v"] = v
            setattr(obj, attr, v)
            self.touch()
            if final and warn_over is not None and v > warn_over:
                self.toast(warn_msg)
            if changed and on_change:
                on_change()
            if final:
                self.page.update()
        field.on_change = lambda e: apply(False)
        field.on_blur = lambda e: apply(True)
        field.on_submit = lambda e: apply(True)
        return field

    def _dd(self, label, obj, attr, options, on_change=None, width=None):
        def handler(e):
            setattr(obj, attr, e.control.value)
            self.touch()
            if on_change:
                on_change()
        kw = {"width": width} if width else {}
        return ft.Dropdown(label=label, value=getattr(obj, attr), dense=True, on_select=handler,
                           options=[ft.DropdownOption(key=k, text=txt) for k, txt in options], **kw)

    def _sl(self, label, obj, attr, lo, hi, div, on_change=None, as_int=False):
        def handler(e):
            v = e.control.value
            setattr(obj, attr, int(round(v)) if as_int else round(v, 2))
            self.touch()
            if on_change:
                on_change()
        return ft.Column(spacing=0, controls=[
            ft.Text(label, size=12, color=C.ON_SURFACE_VARIANT),
            ft.Slider(min=lo, max=hi, divisions=div, value=getattr(obj, attr), label="{value}", on_change=handler)])

    def _sw(self, label, obj, attr, on_change=None):
        def handler(e):
            setattr(obj, attr, bool(e.control.value))
            self.touch()
            if on_change:
                on_change()
        return ft.Switch(label=label, value=bool(getattr(obj, attr)), on_change=handler)

    def role_options(self, ambito: str, current: str = "") -> list[tuple[str, str]]:
        opts = [(r.id, self.lib_ui.title_of(r)) for r in self.library.roles(ambito)]
        if current and current not in dict(opts):
            opts.append((current, t("{id} (no existe)", id=current)))
        return opts

    async def start(self) -> None:
        p = self.page
        p.title = t("NodeAi — Geo Magnet Studio")
        p.theme_mode = ft.ThemeMode.DARK
        p.padding = 0
        p.on_resize = lambda e: self._layout()
        p.on_close = lambda e: self._shutdown()
        self.picker = ft.FilePicker()
        p.services.append(self.picker)

        self.rail = ft.NavigationRail(
            selected_index=0, label_type=ft.NavigationRailLabelType.ALL, min_width=80,
            destinations=[ft.NavigationRailDestination(icon=ft.Icons.CHAT, label=t("Chat")),
                          ft.NavigationRailDestination(icon=ft.Icons.SETTINGS, label=t("Configuración"))],
            on_change=lambda e: self.go("chat" if e.control.selected_index == 0 else "config"))
        self.navbar = ft.NavigationBar(
            selected_index=0,
            destinations=[ft.NavigationBarDestination(icon=ft.Icons.CHAT, label=t("Chat")),
                          ft.NavigationBarDestination(icon=ft.Icons.SETTINGS, label=t("Configuración"))],
            on_change=lambda e: self.go(("chat", "config")[e.control.selected_index]))

        self.chat_view = self._build_chat()
        self.config_col = ft.Column(scroll=ft.ScrollMode.AUTO, expand=True, spacing=10)
        self.config_view = ft.Container(content=self.config_col, padding=16, expand=True, visible=False)
        self.hw_box = ft.Column(spacing=2)
        self.hw_hint = ft.Text("", selectable=True, size=12, font_family="monospace")

        self.root = ft.Column(expand=True, spacing=0, controls=[
            ft.Row(expand=True, spacing=0, controls=[self.rail, ft.VerticalDivider(width=1), self.chat_view, self.config_view]),
            self.navbar])
        p.add(ft.SafeArea(expand=True, content=self.root))

        self._rebuild_all()
        if not self.s.configured:
            self.go("config")
        self._layout()
        asyncio.create_task(self._scan_hw())
        asyncio.create_task(self._startup_notices())

    async def _startup_notices(self) -> None:
        if self.store.migrated_from:
            self.toast(t("Tus ajustes ahora están en {path}. Se conserva una copia del archivo anterior (config.json.bak).",
                         path=str(self.store.path)))
        if not self.store.needs_key_notice:
            return
        n = len(self.store.legacy_key_slots)
        ans = await self.dialog(
            t("Cambio en el guardado de las API keys"),
            [ft.Text(t("Antes NodeAi guardaba tus API keys automáticamente. Ahora NO se guardan en disco por defecto.")),
             ft.Text(t("Tienes {n} clave(s) guardadas en texto plano en este equipo. Elige qué hacer:", n=n)),
             ft.Text(t("• Mantener guardadas: se conservan (puedes cambiarlo por IA con «Guardar API key en este equipo»)."), size=12),
             ft.Text(t("• Quitar del disco: seguirán funcionando en esta sesión, pero tendrás que escribirlas de nuevo; "
                       "también se borran las copias antiguas (config.json y config.json.bak). .env y variables de entorno no se tocan."), size=12)],
            [(t("Mantener guardadas"), "keep", False), (t("Quitar del disco"), "drop", True)], front=True)
        if ans == "drop":
            self.store.drop_legacy_keys()
            self.toast(t("Las API keys se quitaron del disco."))
        elif ans == "keep":
            self.store.keep_legacy_keys()
        self._rebuild_all()

    def _shutdown(self) -> None:
        self.store.save()
        if self.task:
            self.task.cancel()
        try:
            asyncio.get_running_loop().create_task(self.client.close())
        except RuntimeError:
            pass

    def _apply_texts(self) -> None:
        self.page.title = t("NodeAi — Geo Magnet Studio")
        self.rail.destinations[0].label, self.rail.destinations[1].label = t("Chat"), t("Configuración")
        self.navbar.destinations[0].label, self.navbar.destinations[1].label = t("Chat"), t("Configuración")
        self.btn_attach.tooltip = t("Adjuntar imagen, video, PDF, código…")
        self.btn_send.tooltip, self.btn_stop.tooltip = t("Enviar"), t("Detener")
        self.agent_ui.sync()

    def _set_language(self, code: str) -> None:
        self.s.language = set_language(code)
        self.store.save()
        self._apply_texts()
        self._rebuild_all()

    def _wide(self) -> bool:
        return (self.page.width or WIDE) >= WIDE

    def go(self, view: str, tab: str | None = None) -> None:
        self.view = view
        if tab in dict((k, v) for k, _, v in CONFIG_TABS):
            self.cfg_tab = tab
            self.config_col.controls = self._config_controls()
        self._layout()

    def _layout(self) -> None:
        wide = self._wide()
        self.rail.visible = wide
        self.navbar.visible = not wide
        idx = 1 if self.view == "config" else 0
        self.rail.selected_index = self.navbar.selected_index = idx
        self.chat_view.visible = self.view == "chat"
        self.config_view.visible = self.view == "config"
        self._layout_config(wide)
        self.page.update()

    def _select_tab(self, key: str) -> None:
        self.cfg_tab = key
        self._rebuild_all()

    def _layout_config(self, wide: bool) -> None:
        if wide:
            nav = ft.Column(spacing=2, controls=[
                ft.ListTile(leading=ft.Icon(icon), title=ft.Text(t(label)), selected=key == self.cfg_tab, dense=True,
                            on_click=lambda e, k=key: self._select_tab(k)) for key, icon, label in CONFIG_TABS])
            self.config_view.content = ft.Row(expand=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.START, controls=[
                ft.Container(content=nav, width=230), ft.VerticalDivider(width=1), self.config_col])
        else:
            nav = ft.Row(scroll=ft.ScrollMode.AUTO, spacing=6, controls=[
                (ft.FilledTonalButton if key == self.cfg_tab else ft.TextButton)(
                    content=t(label), icon=icon, on_click=lambda e, k=key: self._select_tab(k)) for key, icon, label in CONFIG_TABS])
            self.config_view.content = ft.Column(expand=True, spacing=6, controls=[nav, self.config_col])

    def _build_chat(self) -> ft.Control:
        self.messages = ft.ListView(expand=True, spacing=10, auto_scroll=True, padding=12)
        self.chips = ft.Row(wrap=True, spacing=6)
        self.input = ft.TextField(hint_text=t("Escribe tu mensaje…  (Enter envía · Shift+Enter nueva línea)"),
                                  expand=True, multiline=True, min_lines=1, max_lines=6, shift_enter=True,
                                  border_radius=20, on_submit=self.on_send)
        self.btn_attach = ft.IconButton(icon=ft.Icons.ATTACH_FILE, tooltip=t("Adjuntar imagen, video, PDF, código…"),
                                        on_click=self.on_attach)
        self.btn_send = ft.IconButton(icon=ft.Icons.SEND, tooltip=t("Enviar"), on_click=self.on_send)
        self.btn_stop = ft.IconButton(icon=ft.Icons.STOP, tooltip=t("Detener"), visible=False, icon_color=C.ERROR,
                                      on_click=self.on_stop)
        self.setup_card = ft.Container(visible=False)
        self.agent_ui.sync()
        return ft.Column(expand=True, spacing=0, controls=[
            self.setup_card, self.agent_ui.banner, self.messages,
            ft.Container(padding=ft.Padding.symmetric(horizontal=12, vertical=4), content=self.chips),
            ft.Container(padding=ft.Padding.only(left=8, right=8, bottom=8),
                         content=ft.Row(vertical_alignment=ft.CrossAxisAlignment.END,
                                        controls=[self.btn_attach, self.input, self.btn_send, self.btn_stop]))])

    def _refresh_setup_card(self) -> None:
        d_ok = self.s.director.is_ready()
        self.setup_card.visible = not d_ok
        if not d_ok:
            self.setup_card.content = ft.Container(
                margin=12, padding=12, border_radius=12, bgcolor=C.SECONDARY_CONTAINER,
                content=ft.Row([ft.Icon(ft.Icons.WARNING, color=C.AMBER),
                                ft.Text(t("Antes de chatear configura el modelo Director (proveedor, modelo y API key)."), expand=True),
                                ft.TextButton(content=t("Configurar"), on_click=lambda e: self.go("config", "director"))]))

    async def on_attach(self, e) -> None:
        files = await self.picker.pick_files(allow_multiple=True, dialog_title=t("Adjuntar archivos"))
        for f in files or []:
            if not f.path:
                self.toast(t("Esta plataforma no expone la ruta del archivo; no se pudo adjuntar."))
                continue
            att = await asyncio.to_thread(load_attachment, f.path)
            if att.kind == "unsupported":
                self.toast(f"{att.name}: {att.note}")
                continue
            self.attachments.append(att)
        self._render_chips()

    def _render_chips(self) -> None:
        def remover(a):
            return lambda e: (self.attachments.remove(a), self._render_chips())
        self.chips.controls = [ft.Chip(label=ft.Text(f"{a.name}" + (f" · {a.note}" if a.note else "")),
                                       on_delete=remover(a)) for a in self.attachments]
        self.page.update()

    def _bubble(self, content: ft.Control, mine: bool) -> ft.Control:
        return ft.Row(alignment=ft.MainAxisAlignment.END if mine else ft.MainAxisAlignment.START,
                      controls=[ft.Container(
                          content=content, padding=12, border_radius=14, expand=not mine,
                          bgcolor=C.PRIMARY_CONTAINER if mine else C.SURFACE_CONTAINER_HIGHEST,
                          margin=ft.Margin.only(left=48) if mine else ft.Margin.only(right=12))])

    def _table_tile(self, turns: list[Turn]) -> ft.Control:
        multi = max((x.round for x in turns), default=1) > 1
        tiles = []
        for x in turns:
            tiles.append(ft.ExpansionTile(
                title=ft.Text(f"{x.name}" + (t(" · ronda {n}", n=x.round) if multi else "")),
                subtitle=ft.Text(f"⚠ {x.error}" if x.error else f"{x.seconds}s", size=12,
                                 color=C.ERROR if x.error else C.ON_SURFACE_VARIANT, max_lines=2),
                controls=[] if x.error else [ft.Container(padding=10, content=ft.Markdown(
                    x.text, selectable=True, extension_set=ft.MarkdownExtensionSet.GITHUB_WEB))]))
        ok = sum(1 for x in turns if x.text)
        return ft.ExpansionTile(title=ft.Text(t("Mesa: {ok}/{total} respuestas", ok=ok, total=len(turns)), size=13), controls=tiles)

    async def on_send(self, e) -> None:
        text = (self.input.value or "").strip()
        if (not text and not self.attachments) or self.task:
            return
        if not self.s.director.is_ready():
            self.toast(t("Configura primero el modelo Director."))
            self.go("config", "director")
            return
        atts, self.attachments = self.attachments, []
        shown = text + ("".join(f"\n📎 {a.name}" for a in atts))
        prompt, images = build_prompt(text or t("Analiza los archivos adjuntos."), atts)
        self.input.value = ""
        self._render_chips()
        self.messages.controls.append(self._bubble(ft.Text(shown, selectable=True), True))
        job = self.agent_ui.run(text, prompt, images) if self.s.agent.enabled else self._run(text, prompt, images)
        self.task = asyncio.create_task(job)
        self.page.update()

    async def on_stop(self, e) -> None:
        if self.task:
            self.task.cancel()

    def _set_running(self, running: bool) -> None:
        self.btn_send.visible = not running
        self.btn_stop.visible = running

    async def _run(self, plain_text: str, prompt: str, images: list) -> None:
        active = self.s.active_slots()
        rounds = clamp_rounds(self.s.rounds)
        total = max(1, len(active)) * rounds
        status = ft.Text(t("La mesa está pensando… 0/{n}", n=total) if active else t("El Director está pensando…"), italic=True)
        row = self._bubble(ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2), status]), False)
        self.messages.controls.append(row)
        self._set_running(True)
        self.page.update()
        done = 0

        async def on_event(kind, payload):
            nonlocal done
            if kind == "turn":
                done += 1
                msg = t("La mesa responde… {done}/{total}", done=done, total=total)
                if rounds > 1:
                    msg += t(" (ronda {r}/{n})", r=min(rounds, (done - 1) // max(1, len(active)) + 1), n=rounds)
                status.value = msg
            elif kind == "director":
                status.value = t("El Director sintetiza la respuesta final…")
            self.page.update()

        try:
            res: Result = await Orchestrator(self.s, self.client).run(prompt, images, self.history, on_event)
            parts: list[ft.Control] = [ft.Markdown(res.final, selectable=True,
                                                   extension_set=ft.MarkdownExtensionSet.GITHUB_WEB)]
            if res.turns:
                parts.append(self._table_tile(res.turns))
            self.messages.controls[self.messages.controls.index(row)] = self._bubble(ft.Column(parts), False)
            if not res.director_error:
                self.history += [{"role": "user", "content": plain_text or t("(archivos adjuntos)")},
                                 {"role": "assistant", "content": res.final}]
                self.history = self.history[-40:]
        except asyncio.CancelledError:
            self.messages.controls[self.messages.controls.index(row)] = self._bubble(
                ft.Text(t("Detenido por el usuario."), italic=True), False)
        except Exception as ex:
            self.messages.controls[self.messages.controls.index(row)] = self._bubble(
                ft.Text(t("⚠️ Error inesperado: {error}", error=str(ex)), color=C.ERROR, selectable=True), False)
        finally:
            self.task = None
            self._set_running(False)
            self.page.update()

    @staticmethod
    def _sync_mode(slot: ModelSlot) -> None:
        if PROVIDERS[slot.provider].local != (slot.mode == "local"):
            slot.provider = providers_for(slot.mode)[0].key
            slot.vision = slot.mode != "local"

    def _rebuild_all(self) -> None:
        self.config_col.controls = self._config_controls()
        self._refresh_setup_card()
        self._layout_config(self._wide())
        self.page.update()

    def _section(self, title: str, icon, body: list[ft.Control], expanded=True) -> ft.Control:
        return ft.Card(content=ft.ExpansionTile(
            leading=ft.Icon(icon), title=ft.Text(title, weight=ft.FontWeight.BOLD), expanded=expanded,
            controls=[ft.Container(padding=ft.Padding.only(left=14, right=14, bottom=14),
                                   content=ft.Column(body, spacing=10))]))

    def _config_controls(self) -> list[ft.Control]:
        builder = {"general": self._tab_general, "director": self._tab_director, "mesa": self._tab_mesa,
                   "agente": self._tab_agent, "biblioteca": self._tab_library, "local": self._tab_local}
        out = builder.get(self.cfg_tab, self._tab_general)()
        out.append(ft.Text(COPYRIGHT, size=11, color=C.ON_SURFACE_VARIANT))
        return out

    def _tab_general(self) -> list[ft.Control]:
        s = self.s
        ready_d = s.director.is_ready()
        n_ready = sum(1 for x in s.slots if x.is_ready())
        checks = [(self.hw is not None, t("Hardware detectado") if self.hw else t("Detectando hardware…")),
                  (ready_d, t("Director configurado") if ready_d else t("Configura el Director (modelo principal)")),
                  (n_ready > 0, t("{n} IA(s) de la mesa listas", n=n_ready) if n_ready else
                   t("Opcional: añade IAs a la mesa (sin ellas el Director responde solo)"))]
        status = [ft.Row([ft.Icon(ft.Icons.CHECK_CIRCLE if ok else ft.Icons.WARNING,
                                  color=C.GREEN if ok else C.AMBER, size=18), ft.Text(txt)]) for ok, txt in checks]
        status.append(ft.FilledButton(content=t("Listo, ir al chat"), icon=ft.Icons.CHAT, disabled=not ready_d,
                                      on_click=self._finish_setup))
        title = t("Bienvenido a NodeAi: configuración inicial") if not s.configured else t("Estado")
        lang = ft.Dropdown(label=t("Idioma de la interfaz"), value=s.language, dense=True, width=260,
                           options=[ft.DropdownOption(key=k, text=v) for k, v in LANGS.items()],
                           on_select=lambda e: self._set_language(e.control.value))
        return [self._section(title, ft.Icons.CHECK_CIRCLE, status),
                self._section(t("Idioma"), ft.Icons.LANGUAGE, [lang]),
                self._section(t("Ajustes generales"), ft.Icons.TUNE, [
                    self._tf(t("Prompt de sistema global (se añade a todas las IAs)"), s, "global_system_prompt", multiline=True),
                    ft.Row([self._tf(t("Timeout (s)"), s, "timeout_s", number=int, width=130),
                            self._tf(t("IAs en paralelo"), s, "max_parallel", number=int, width=150),
                            self._tf(t("Mensajes de historial"), s, "history_messages", number=int, width=170)], wrap=True)])]

    def _tab_director(self) -> list[ft.Control]:
        return [self._section(t("Modelo Director (anfitrión)"), ft.Icons.SMART_TOY,
                              [self._slot_fields(self.s.director, director=True)])]

    def _tab_mesa(self) -> list[ft.Control]:
        s = self.s
        rounds = self._int_field(
            t("Rondas de debate"), s, "rounds", lo=1, hi=MAX_ROUNDS, warn_over=ROUNDS_WARN, width=190,
            warn_msg=t("Más de {n} rondas consumen muchos más tokens: cada ronda vuelve a llamar a todas las IAs.", n=ROUNDS_WARN))
        head = [rounds, ft.Text(t("1 = respuestas en paralelo; 2 o más = las IAs se replican entre sí (máximo {max}). "
                                  "Más rondas = más tokens y más tiempo.", max=MAX_ROUNDS), size=12, color=C.ON_SURFACE_VARIANT)]
        return [self._section(t("Rondas"), ft.Icons.FORUM, head),
                self._section(t("Mesa de IAs ({n}/{max})", n=len(s.slots), max=MAX_MODELS), ft.Icons.GROUPS, self._table_controls())]

    def _tab_agent(self) -> list[ft.Control]:
        return [self._section(t("Agente (control del PC)"), ft.Icons.SUPPORT_AGENT, self.agent_ui.config_section())]

    def _tab_library(self) -> list[ft.Control]:
        return [self._section(t("Biblioteca de prompts y roles"), ft.Icons.LIBRARY_BOOKS, self.lib_ui.section())]

    def _tab_local(self) -> list[ft.Control]:
        return [self._section(t("Hardware y motor local"), ft.Icons.MEMORY, self._hardware_controls()),
                self._section(t("Modelos locales (GGUF)"), ft.Icons.DOWNLOAD, self._models_controls(), expanded=False)]

    def _finish_setup(self, e) -> None:
        self.s.configured = True
        self.store.save()
        self._rebuild_all()
        self.go("chat")

    def _hardware_controls(self) -> list[ft.Control]:
        h = self.s.hardware
        self._fill_hw()
        return [self.hw_box,
                ft.Row([ft.OutlinedButton(content=t("Volver a detectar"), icon=ft.Icons.REFRESH,
                                          on_click=lambda e: asyncio.create_task(self._scan_hw(True)))]),
                ft.Row(wrap=True, controls=[
                    self._dd(t("Dispositivo"), h, "device", [("auto", t("Automático")), ("cpu", t("Solo CPU")), ("gpu", t("Forzar GPU"))], width=190),
                    self._tf(t("Capas en GPU (-1 = todas)"), h, "n_gpu_layers", number=int, width=210),
                    self._tf(t("Hilos CPU (0 = auto)"), h, "n_threads", number=int, width=170),
                    self._tf(t("Contexto (tokens)"), h, "n_ctx", number=int, width=160),
                    self._tf(t("GGUF cargados a la vez"), h, "max_loaded_models", number=int, width=200)]),
                self.hw_hint]

    def _fill_hw(self) -> None:
        if self.hw is None:
            self.hw_box.controls = [ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2),
                                            ft.Text(t("Detectando hardware…"))])]
            self.hw_hint.value = ""
            return
        self.hw_box.controls = [ft.Text(l, selectable=True) for l in self.hw.summary_lines()]
        hw = self.hw
        if hw.llama_cpp_installed and (hw.llama_cpp_gpu or not hw.has_gpu):
            self.hw_hint.value = ""
            return
        win = os.name == "nt"
        flag = ("-DGGML_METAL=on" if any(g.backend == "metal" for g in hw.gpus) else
                "-DGGML_CUDA=on" if any(g.vendor == "nvidia" for g in hw.gpus) else
                "-DGGML_VULKAN=on" if hw.gpus else "")
        if win:
            cmd = (f'$env:CMAKE_ARGS="{flag}"; ' if flag else "") + "pip install llama-cpp-python --no-cache-dir"
        else:
            cmd = (f'CMAKE_ARGS="{flag}" ' if flag else "") + "pip install llama-cpp-python --no-cache-dir"
        self.hw_hint.value = (t("Para usar modelos GGUF integrados, instala el motor (requiere compilador C++; "
                                "ver README de llama-cpp-python):") + "\n" + cmd +
                              "\n" + t("Sin compilar: usa Ollama o LM Studio (modo Local)."))

    async def _scan_hw(self, refresh: bool = False) -> None:
        if refresh:
            self.hw = None
            self._fill_hw()
            self.page.update()
        self.hw = await asyncio.to_thread(hardware_check.detect, refresh)
        self._fill_hw()
        self._rebuild_all()

    def _slot_fields(self, slot: ModelSlot, director: bool = False) -> ft.Column:
        col = ft.Column(spacing=10)

        def refill():
            col.controls = self._slot_field_list(slot, refill, director)
            if col.page:
                col.update()
        col.controls = self._slot_field_list(slot, refill, director)
        return col

    def _key_save_switch(self, slot: ModelSlot) -> ft.Control:
        def handler(e):
            slot.save_key = bool(e.control.value)
            self.store.save()
            self.toast(t("La API key se guardará en este equipo.") if slot.save_key else
                       t("La API key ya no se guarda en disco: solo vive en memoria esta sesión."))
        return ft.Column(spacing=0, controls=[
            ft.Switch(label=t("Guardar API key en este equipo"), value=bool(slot.save_key), on_change=handler),
            ft.Text(t("La API key se guarda en texto plano en tu equipo."), size=12, color=C.ERROR)])

    def _refresh_titles(self) -> None:
        for sl in self.s.slots:
            txt = self.slot_titles.get(sl.id)
            if txt is not None:
                txt.value = sl.label()
        self.page.update()

    def _slot_field_list(self, slot: ModelSlot, refill, director: bool) -> list[ft.Control]:
        prov = PROVIDERS[slot.provider]
        opts = [(p.key, p.label) for p in providers_for(slot.mode)]

        def mode_changed():
            self._sync_mode(slot)
            refill()

        def picked(name: str) -> None:
            slot.model = name
            self.touch()
            refill()

        f: list[ft.Control] = []
        if not director:
            f.append(self._tf(t("Nombre"), slot, "name", on_blur=self._refresh_titles))
        f.append(ft.Row(wrap=True, controls=[
            self._dd(t("Ejecución"), slot, "mode", [("cloud", t("Nube (API)")), ("local", t("Local"))], on_change=mode_changed, width=170),
            self._dd(t("Proveedor"), slot, "provider", opts, on_change=refill, width=300)]))
        if prov.protocol == "gguf":
            installed = [(str(p), p.name) for p in list_models(self.s.models_dir)]
            if installed:
                f.append(self._dd(t("Modelo instalado"), slot, "gguf_path", installed, on_change=refill))
            f.append(self._tf(t("Ruta del archivo .gguf"), slot, "gguf_path", hint="C:\\modelos\\modelo.gguf"))
        else:
            model = self._tf(t("Modelo (id exacto del proveedor)"), slot, "model",
                             hint=t("p. ej. claude-sonnet-5-5 · llama3.2"), on_blur=self._refresh_titles, expand=True)
            f.append(ft.Row([model, self.model_picker.button(slot, picked)]))
            if prov.needs_key:
                f.append(self._tf(t("API key (vacía = variable {env})", env=prov.env_key), slot, "api_key", password=True))
                f.append(self._key_save_switch(slot))
            f.append(self._tf(t("URL base"), slot, "base_url", hint=prov.base_url or "https://…/v1"))
        if director:
            f.append(self._dd(t("Rol del Director"), self.s, "director_role", self.role_options("director", self.s.director_role), width=260))
        else:
            f.append(self._dd(t("Rol"), slot, "role", self.role_options("mesa", slot.role), width=260))
        f.append(self._tf(t("Prompt de sistema propio"), slot, "system_prompt", multiline=True))
        f.append(self._sl(t("Temperatura (creatividad)"), slot, "temperature", 0, 2, 20))
        f.append(self._sl(t("Top-p"), slot, "top_p", 0, 1, 20))
        f.append(self._tf(t("Máx. tokens de respuesta"), slot, "max_tokens", number=int, width=220))
        f.append(self._sw(t("Enviar imágenes a este modelo (solo si tiene visión)"), slot, "vision"))
        return f

    def _toggle(self, slot: ModelSlot):
        def h(e):
            slot.enabled = bool(e.control.value)
            self.touch()
            self._update_count()
            self.page.update()
        return h

    def _update_count(self) -> None:
        self.table_count.value = t("IAs de la mesa: {n} activas de {total} (máx. {max})",
                                   n=len(self.s.active_slots()), total=len(self.s.slots), max=MAX_MODELS)

    def _table_controls(self) -> list[ft.Control]:
        self._update_count()
        ctrls: list[ft.Control] = [self.table_count]
        if not self.s.slots:
            ctrls.append(ft.Text(t("Aún no hay IAs en la mesa. Añade la primera con el botón de abajo."), italic=True))
        self.slot_titles = {}
        for slot in self.s.slots:
            def remove(e, sl=slot):
                self.s.slots.remove(sl)
                self.store.save()
                self._rebuild_all()

            def toggled(e, sl=slot):
                (self.open_tiles.add if e.control.expanded else self.open_tiles.discard)(sl.id)
            title = ft.Text(slot.label(), max_lines=1, overflow=ft.TextOverflow.ELLIPSIS)
            self.slot_titles[slot.id] = title
            ready = slot.is_ready()
            ctrls.append(ft.ExpansionTile(
                leading=ft.Switch(value=slot.enabled, on_change=self._toggle(slot), tooltip=t("Activa en la mesa")),
                title=title,
                subtitle=ft.Text((("" if ready else t("⚠ incompleta") + " · ") + PROVIDERS[slot.provider].label
                                  + " · " + (t("Local") if slot.mode == "local" else t("Nube"))), size=12),
                expanded=slot.id in self.open_tiles, on_change=toggled,
                controls=[ft.Container(padding=10, content=ft.Column([
                    self._slot_fields(slot),
                    ft.OutlinedButton(content=t("Quitar esta IA"), icon=ft.Icons.DELETE, on_click=remove)]))]))
        full = len(self.s.slots) >= MAX_MODELS

        def add(e):
            slot = self.store.add_slot()
            if slot:
                self.open_tiles.add(slot.id)
                self.store.save()
                self._rebuild_all()
        ctrls.append(ft.FilledButton(content=t("Añadir IA ({n}/{max})", n=len(self.s.slots), max=MAX_MODELS), icon=ft.Icons.ADD,
                                     disabled=full, on_click=add))
        return ctrls

    def _models_controls(self) -> list[ft.Control]:
        self.dl_url = ft.TextField(label=t("URL del .gguf, o repo de Hugging Face (usuario/repo)"), dense=True)
        self.dl_file = ft.TextField(label=t("Archivo .gguf (solo si pones un repo)"), dense=True)
        self.dl_bar = ft.ProgressBar(value=0, visible=False)
        self.dl_text = ft.Text("", size=12)
        self.dl_btn = ft.FilledButton(content=t("Descargar"), icon=ft.Icons.DOWNLOAD, on_click=self._on_download)
        self.dl_cancel = ft.TextButton(content=t("Cancelar"), visible=False, on_click=self._on_cancel_dl)
        self.dl_task: asyncio.Task | None = None
        models = list_models(self.s.models_dir)
        listing = [ft.Text(f"• {m.name}  ({m.stat().st_size / 1024**3:.2f} GB)", selectable=True) for m in models] \
            or [ft.Text(t("Aún no hay modelos .gguf en la carpeta."), italic=True)]
        return [self._tf(t("Carpeta de modelos"), self.s, "models_dir"), *listing,
                ft.Divider(), self.dl_url, self.dl_file, ft.Row([self.dl_btn, self.dl_cancel]), self.dl_bar, self.dl_text]

    def _on_cancel_dl(self, e) -> None:
        if self.dl_task:
            self.dl_task.cancel()

    async def _on_download(self, e) -> None:
        src = (self.dl_url.value or "").strip()
        if not src or self.dl_task:
            return
        fname = (self.dl_file.value or "").strip()
        url = src
        if not src.startswith("http"):
            if not fname:
                self.toast(t("Indica también el nombre del archivo .gguf del repositorio."))
                return
            url = hf_url(src, fname)
        self.dl_btn.disabled, self.dl_cancel.visible, self.dl_bar.visible = True, True, True
        self.dl_text.value = t("Descargando…")
        self.page.update()
        last = 0.0

        def progress(frac: float, done: int, total: int) -> None:
            nonlocal last
            if time.monotonic() - last < 0.5:
                return
            last = time.monotonic()
            self.dl_bar.value = frac or None
            self.dl_text.value = f"{done / 1024**2:.0f} MB" + (t(" de {total} MB", total=f"{total / 1024**2:.0f}") if total else "")
            self.page.update()

        async def job():
            return await download_model(url, self.s.models_dir, fname or None, progress)
        self.dl_task = asyncio.create_task(job())
        try:
            path = await self.dl_task
            self.dl_text.value = t("✔ Guardado: {name}", name=path.name)
        except asyncio.CancelledError:
            self.dl_text.value = t("Descarga cancelada.")
        except Exception as ex:
            self.dl_text.value = t("⚠ Error: {error}", error=str(ex))
        finally:
            self.dl_task = None
            self.dl_btn.disabled, self.dl_cancel.visible, self.dl_bar.visible = False, False, False
            self.page.update()


async def main(page: ft.Page) -> None:
    await NodeAiApp(page).start()
