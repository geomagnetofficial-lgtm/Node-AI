from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

import flet as ft

from src.core.i18n import t
from src.core.settings import ModelSlot
from src.llm import model_lister
from src.llm.model_lister import ModelListError, filter_models

if TYPE_CHECKING:  # pragma: no cover
    from src.ui import NodeAiApp

C = ft.Colors
MAX_SHOWN = 300


class ModelPicker:
    def __init__(self, app: "NodeAiApp"):
        self.app = app

    def button(self, slot: ModelSlot, on_pick: Callable[[str], None]) -> ft.IconButton:
        return ft.IconButton(icon=ft.Icons.SEARCH, tooltip=t("Elegir de la lista de modelos del proveedor"),
                             on_click=lambda e: asyncio.create_task(self.open(slot, on_pick)))

    async def open(self, slot: ModelSlot, on_pick: Callable[[str], None]) -> str | None:
        models: list[str] = list(model_lister.cached_models(slot.provider, slot.api_key, slot.base_url) or [])
        search = ft.TextField(label=t("Buscar modelo"), dense=True, autofocus=True, prefix_icon=ft.Icons.SEARCH)
        status = ft.Text("", size=12)
        listing = ft.ListView(height=320, spacing=0)
        state: dict = {"fut": None, "busy": False}

        def pick(name: str):
            return lambda e: state["fut"] is not None and not state["fut"].done() and state["fut"].set_result(name)

        def render() -> None:
            shown = filter_models(models, search.value or "")
            listing.controls = [ft.ListTile(title=ft.Text(m, size=13), dense=True, on_click=pick(m)) for m in shown[:MAX_SHOWN]]
            if models and not state["busy"]:
                extra = t(" (se muestran los primeros {n})", n=MAX_SHOWN) if len(shown) > MAX_SHOWN else ""
                status.value = t("{n} de {total} modelos", n=len(shown), total=len(models)) + extra
                status.color = None
            if not models and not status.value:
                status.value = t("Sin lista todavía: pulsa «Actualizar lista» o escribe el id a mano.")
            self.app.page.update()

        async def load(refresh: bool) -> None:
            nonlocal models
            state["busy"] = True
            status.value, status.color = t("Consultando la API…"), None
            self.app.page.update()
            try:
                models = await model_lister.get_models(slot.provider, slot.api_key, slot.base_url, refresh=refresh)
                state["busy"] = False
            except ModelListError as e:
                state["busy"] = False
                status.value, status.color = f"⚠ {e}", C.ERROR
            except Exception as e:
                state["busy"] = False
                status.value, status.color = f"⚠ {type(e).__name__}", C.ERROR
            render()

        search.on_change = lambda e: render()
        refresh_btn = ft.OutlinedButton(content=t("Actualizar lista"), icon=ft.Icons.REFRESH,
                                        on_click=lambda e: asyncio.create_task(load(True)))
        render()
        if not models:
            asyncio.create_task(load(False))
        chosen = await self.app.dialog(
            t("Modelos de {provider}", provider=model_lister.get_provider(slot.provider).label),
            [search, refresh_btn, status, listing], [(t("Cerrar"), None, False)], width=520,
            on_future=lambda f: state.__setitem__("fut", f))
        if chosen:
            on_pick(chosen)
        return chosen
