from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from src.core.i18n import t
from src.core.library import Prompt, Role

if TYPE_CHECKING:  # pragma: no cover
    from src.ui import NodeAiApp

C = ft.Colors
ALL = "*"


class LibraryUI:
    def __init__(self, app: "NodeAiApp"):
        self.app = app
        self.query = ""
        self.f_rol = self.f_cat = self.f_tag = self.f_origen = ALL
        self.target = "director"
        self.list_box = ft.Column(spacing=4)
        self.roles_box = ft.Column(spacing=4)

    @property
    def lib(self):
        return self.app.library

    def origin_label(self, origen: str) -> str:
        return t("Por defecto") if origen == "default" else t("Mío")

    def title_of(self, item: Prompt | Role) -> str:
        name = item.titulo if isinstance(item, Prompt) else item.nombre
        return t(name) if item.origen == "default" else name

    def role_name(self, role_id: str) -> str:
        role = self.lib.get_role(role_id)
        return self.title_of(role) if role else (t("{id} (no existe)", id=role_id) if role_id else "—")

    def _targets(self) -> list[tuple[str, str]]:
        out = [("director", t("Director"))]
        out += [(s.id, s.label()) for s in self.app.s.slots]
        return out

    def section(self) -> list[ft.Control]:
        lib = self.lib
        ctrls: list[ft.Control] = []
        news = lib.new_defaults()
        if lib.updated_from is not None:
            ctrls.append(ft.Container(padding=10, border_radius=10, bgcolor=C.SECONDARY_CONTAINER, content=ft.Row([
                ft.Icon(ft.Icons.INFO),
                ft.Text(t("Se cargaron los valores por defecto de la versión {v} ({n} novedades). Tus roles y prompts no se tocaron.",
                          v=lib.defaults_version, n=len(news)), expand=True),
                ft.TextButton(content=t("Entendido"), on_click=self._ack)])))
        if lib.warnings:
            ctrls.append(ft.ExpansionTile(
                leading=ft.Icon(ft.Icons.WARNING, color=C.AMBER), title=ft.Text(t("Avisos de la biblioteca ({n})", n=len(lib.warnings)), size=13),
                controls=[ft.Container(padding=10, content=ft.Column([ft.Text(w, size=12, selectable=True) for w in lib.warnings]))]))
        ctrls.append(ft.Text(t("Los prompts y roles por defecto no se editan: «Editar» crea tu propia copia. "
                               "Lo tuyo se guarda en usuario/prompts_usuario.json y nunca se sobrescribe al actualizar."),
                             size=12, color=C.ON_SURFACE_VARIANT))
        search = ft.TextField(label=t("Buscar prompts"), dense=True, prefix_icon=ft.Icons.SEARCH, value=self.query,
                              on_change=self._on_query, expand=True)
        roles_opts = [(ALL, t("Todos los roles"))] + [(r.id, self.title_of(r)) for r in lib.roles()]
        cats = [(ALL, t("Todas las categorías"))] + [(c, c) for c in lib.categorias()]
        tags = [(ALL, t("Todas las etiquetas"))] + [(x, x) for x in lib.etiquetas()]
        origs = [(ALL, t("Todos los orígenes")), ("default", t("Por defecto")), ("usuario", t("Mío"))]
        ctrls.append(ft.Row([search]))
        ctrls.append(ft.Row(wrap=True, controls=[
            self._filter(t("Rol"), "f_rol", roles_opts), self._filter(t("Categoría"), "f_cat", cats),
            self._filter(t("Etiqueta"), "f_tag", tags), self._filter(t("Origen"), "f_origen", origs)]))
        targets = self._targets()
        if self.target not in dict(targets):
            self.target = "director"
        ctrls.append(ft.Dropdown(label=t("Aplicar a"), value=self.target, dense=True, width=260,
                                 options=[ft.DropdownOption(key=k, text=v) for k, v in targets],
                                 on_select=lambda e: setattr(self, "target", e.control.value)))
        ctrls.append(self.list_box)
        ctrls.append(ft.Row(wrap=True, controls=[
            ft.FilledButton(content=t("Nuevo prompt"), icon=ft.Icons.ADD, on_click=lambda e: asyncio.create_task(self.edit_prompt(None))),
            ft.OutlinedButton(content=t("Nuevo rol"), icon=ft.Icons.ADD, on_click=lambda e: asyncio.create_task(self.edit_role(None))),
            ft.OutlinedButton(content=t("Importar"), icon=ft.Icons.FILE_UPLOAD, on_click=lambda e: asyncio.create_task(self.import_lib())),
            ft.OutlinedButton(content=t("Exportar"), icon=ft.Icons.FILE_DOWNLOAD, on_click=lambda e: asyncio.create_task(self.export_lib()))]))
        ctrls.append(ft.Divider())
        ctrls.append(ft.Text(t("Roles"), weight=ft.FontWeight.BOLD))
        ctrls.append(self.roles_box)
        self.render_list(update=False)
        self.render_roles(update=False)
        return ctrls

    def _filter(self, label: str, attr: str, options: list[tuple[str, str]]) -> ft.Dropdown:
        def handler(e):
            setattr(self, attr, e.control.value)
            self.render_list()
        return ft.Dropdown(label=label, value=getattr(self, attr), dense=True, width=210, on_select=handler,
                           options=[ft.DropdownOption(key=k, text=v) for k, v in options])

    def _on_query(self, e) -> None:
        self.query = e.control.value or ""
        self.render_list()

    def _ack(self, e) -> None:
        self.lib.acknowledge_defaults()
        self.app._rebuild_all()

    def filtered(self) -> list[Prompt]:
        al = lambda v: "" if v == ALL else v  # noqa: E731
        return self.lib.search_prompts(self.query, al(self.f_rol), al(self.f_cat), al(self.f_tag), al(self.f_origen))

    def render_list(self, update: bool = True) -> None:
        items = self.filtered()
        rows: list[ft.Control] = [ft.Text(t("{n} prompts", n=len(items)), size=12, color=C.ON_SURFACE_VARIANT)]
        for p in items:
            mine = p.origen == "usuario"
            buttons: list[ft.Control] = [
                ft.FilledButton(content=t("Usar"), icon=ft.Icons.PLAY_ARROW, on_click=lambda e, x=p: self.use_prompt(x)),
                ft.OutlinedButton(content=t("Editar"), icon=ft.Icons.EDIT, on_click=lambda e, x=p: asyncio.create_task(self.edit_prompt(x))),
                ft.OutlinedButton(content=t("Duplicar"), icon=ft.Icons.CONTENT_COPY, on_click=lambda e, x=p: self.duplicate_prompt(x))]
            if mine:
                buttons.append(ft.OutlinedButton(content=t("Borrar"), icon=ft.Icons.DELETE,
                                                 on_click=lambda e, x=p: asyncio.create_task(self.delete_prompt(x))))
            rows.append(ft.ExpansionTile(
                title=ft.Text(self.title_of(p), max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(" · ".join(x for x in (self.role_name(p.rol) if p.rol else "", p.categoria, self.origin_label(p.origen)) if x), size=12),
                controls=[ft.Container(padding=10, content=ft.Column([
                    ft.Text(p.texto, selectable=True, size=13),
                    ft.Text(("# " + "  # ".join(p.etiquetas)) if p.etiquetas else "", size=11, color=C.ON_SURFACE_VARIANT),
                    ft.Row(buttons, wrap=True)]))]))
        self.list_box.controls = rows
        if update:
            self.app.page.update()

    def render_roles(self, update: bool = True) -> None:
        rows: list[ft.Control] = []
        for r in self.lib.roles():
            mine = r.origen == "usuario"
            buttons: list[ft.Control] = [
                ft.OutlinedButton(content=t("Editar"), icon=ft.Icons.EDIT, on_click=lambda e, x=r: asyncio.create_task(self.edit_role(x))),
                ft.OutlinedButton(content=t("Duplicar"), icon=ft.Icons.CONTENT_COPY, on_click=lambda e, x=r: self.duplicate_role(x))]
            if mine:
                buttons.append(ft.OutlinedButton(content=t("Borrar"), icon=ft.Icons.DELETE,
                                                 on_click=lambda e, x=r: asyncio.create_task(self.delete_role(x))))
            scope = t("Director") if r.ambito == "director" else t("Mesa")
            rows.append(ft.ExpansionTile(
                title=ft.Text(self.title_of(r)),
                subtitle=ft.Text(f"{scope} · {self.origin_label(r.origen)}" + (f" · {r.descripcion}" if r.descripcion else ""), size=12),
                controls=[ft.Container(padding=10, content=ft.Column([ft.Text(r.prompt_base, selectable=True, size=13), ft.Row(buttons, wrap=True)]))]))
        self.roles_box.controls = rows
        if update:
            self.app.page.update()

    def use_prompt(self, p: Prompt) -> None:
        if self.target == "director":
            self.lib.apply_prompt(self.app.s.director, p, director=True)
            name = t("Director")
        else:
            slot = next((s for s in self.app.s.slots if s.id == self.target), None)
            if slot is None:
                self.app.toast(t("Elige a qué IA aplicar el prompt."))
                return
            self.lib.apply_prompt(slot, p)
            name = slot.label()
        self.app.store.save()
        self.app.toast(t("Prompt «{titulo}» aplicado a {name}.", titulo=self.title_of(p), name=name))
        self.app._rebuild_all()

    def duplicate_prompt(self, p: Prompt) -> None:
        self.lib.duplicate_prompt(p.id)
        self.app.toast(t("Copia creada en tus prompts."))
        self.app._rebuild_all()

    async def delete_prompt(self, p: Prompt) -> None:
        if await self._confirm(t("¿Borrar el prompt «{titulo}»?", titulo=p.titulo)):
            self.lib.delete_prompt(p.id)
            self.render_list()

    async def edit_prompt(self, p: Prompt | None) -> None:
        roles = [("", t("(ninguno)"))] + [(r.id, self.title_of(r)) for r in self.lib.roles("mesa")]
        titulo = ft.TextField(label=t("Título"), value=p.titulo if p else "", dense=True)
        rol = ft.Dropdown(label=t("Rol"), value=p.rol if p and p.rol in dict(roles) else "", dense=True,
                          options=[ft.DropdownOption(key=k, text=v) for k, v in roles])
        cat = ft.TextField(label=t("Categoría"), value=p.categoria if p else "", dense=True)
        tags = ft.TextField(label=t("Etiquetas (separadas por comas)"), value=", ".join(p.etiquetas) if p else "", dense=True)
        texto = ft.TextField(label=t("Texto del prompt"), value=p.texto if p else "", multiline=True, min_lines=4, max_lines=12)
        head = t("Nuevo prompt") if p is None else t("Editar prompt")
        note = [ft.Text(t("Es un prompt por defecto: al guardar se crea tu propia copia."), size=12)] if p and p.origen == "default" else []
        if not await self.app.dialog(head, [*note, titulo, rol, cat, tags, texto], [(t("Cancelar"), False, False), (t("Guardar"), True, True)]):
            return
        values = dict(titulo=titulo.value or "", texto=texto.value or "", rol=rol.value or "", categoria=cat.value or "",
                      etiquetas=[x.strip() for x in (tags.value or "").split(",") if x.strip()])
        try:
            if p is None:
                self.lib.add_prompt(**values)
            else:
                target = self.lib.edit_prompt(p.id)
                self.lib.update_prompt(target.id, **values)
        except ValueError as e:
            self.app.toast(t("No se pudo guardar: {error}", error=str(e)))
            return
        self.app._rebuild_all()

    def duplicate_role(self, r: Role) -> None:
        self.lib.duplicate_role(r.id)
        self.app.toast(t("Copia creada en tus roles."))
        self.app._rebuild_all()

    async def delete_role(self, r: Role) -> None:
        if await self._confirm(t("¿Borrar el rol «{nombre}»? Las IAs que lo usen volverán a «Neutral».", nombre=r.nombre)):
            self.lib.delete_role(r.id)
            self.app._rebuild_all()

    async def edit_role(self, r: Role | None) -> None:
        nombre = ft.TextField(label=t("Nombre"), value=r.nombre if r else "", dense=True)
        desc = ft.TextField(label=t("Descripción"), value=r.descripcion if r else "", dense=True)
        ambito = ft.Dropdown(label=t("Ámbito"), value=r.ambito if r else "mesa", dense=True, options=[
            ft.DropdownOption(key="mesa", text=t("Mesa")), ft.DropdownOption(key="director", text=t("Director"))])
        base = ft.TextField(label=t("Instrucción base del rol"), value=r.prompt_base if r else "", multiline=True, min_lines=4, max_lines=12)
        note = [ft.Text(t("Es un rol por defecto: al guardar se crea tu propia copia."), size=12)] if r and r.origen == "default" else []
        head = t("Nuevo rol") if r is None else t("Editar rol")
        if not await self.app.dialog(head, [*note, nombre, desc, ambito, base], [(t("Cancelar"), False, False), (t("Guardar"), True, True)]):
            return
        values = dict(nombre=nombre.value or "", descripcion=desc.value or "", ambito=ambito.value or "mesa", prompt_base=base.value or "")
        try:
            if r is None:
                self.lib.add_role(values["nombre"], values["prompt_base"], values["descripcion"], values["ambito"])
            else:
                target = self.lib.edit_role(r.id)
                self.lib.update_role(target.id, **values)
        except ValueError as e:
            self.app.toast(t("No se pudo guardar: {error}", error=str(e)))
            return
        self.app._rebuild_all()

    async def export_lib(self) -> None:
        path = await self.app.picker.save_file(dialog_title=t("Exportar biblioteca"), file_name="nodeai_biblioteca.json",
                                               allowed_extensions=["json"])
        if not path:
            return
        try:
            self.lib.export_user(Path(path))
            self.app.toast(t("Biblioteca exportada a {path}", path=path))
        except OSError as e:
            self.app.toast(t("No se pudo exportar: {error}", error=str(e)))

    async def import_lib(self) -> None:
        files = await self.app.picker.pick_files(allow_multiple=False, allowed_extensions=["json"], dialog_title=t("Importar biblioteca"))
        f = files[0] if files else None
        if f is None or not f.path:
            return
        rep = await asyncio.to_thread(self.lib.import_user, Path(f.path))
        if rep.roles or rep.prompts:
            self.app.toast(t("Importados: {roles} roles y {prompts} prompts ({skipped} ya existían).",
                             roles=rep.roles, prompts=rep.prompts, skipped=rep.skipped))
        else:
            self.app.toast(rep.warnings[0] if rep.warnings else t("No había nada nuevo que importar."))
        self.app._rebuild_all()

    async def _confirm(self, question: str) -> bool:
        return bool(await self.app.dialog(t("Confirmar"), [ft.Text(question)], [(t("Cancelar"), False, False), (t("Borrar"), True, True)]))
