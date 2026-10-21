
import pytest

flet = pytest.importorskip("flet")

from src import ui


class FakePage:
    def __init__(self, width=1200):
        self.width = width
        self.services, self.added, self.dialogs = [], [], []
        self.popped = 0

    def add(self, *c): self.added += c
    def update(self, *a): pass
    def show_dialog(self, d): self.dialogs.append(d)
    def pop_dialog(self): self.popped += 1


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("NODEAI_HOME", str(tmp_path))
    page = FakePage()
    return ui.NodeAiApp(page)


async def test_builds_first_run_and_all_providers(app):
    await app.start()
    assert app.view == "config" and not app.s.configured
    for key, p in ui.PROVIDERS.items():
        s = app.store.add_slot()
        s.provider, s.mode = key, ("local" if p.local else "cloud")
    app._rebuild_all()
    assert len(app.s.slots) == 14
    app.page.width = 400
    app._layout()
    assert app.navbar.visible and not app.rail.visible
    app.page.width = 1400
    app.go("chat")
    assert app.rail.visible and not hasattr(app, "side_panel") and not hasattr(app, "panel_view")
    assert [d.label for d in app.navbar.destinations] == ["Chat", "Configuración"]
    for tab in ("general", "director", "mesa", "agente", "biblioteca", "local"):
        app.go("config", tab)
        assert app.cfg_tab == tab and app.config_col.controls


async def test_hardware_scan_does_not_invent_gpu(app, monkeypatch):
    monkeypatch.setattr(ui.hardware_check, "_nvidia", lambda: [])
    monkeypatch.setattr(ui.hardware_check, "_other_gpus", lambda skip_nvidia: [])
    await app.start()
    await app._scan_hw(refresh=True)
    texts = " ".join(c.value for c in app.hw_box.controls)
    assert "ninguna detectada" in texts and "CUDA" not in texts and "12" not in texts.split("GPU:")[1][:30]


async def test_mode_switch_changes_provider_and_ready_checks(app):
    await app.start()
    s = app.store.add_slot()
    s.provider, s.model, s.api_key = "openai", "m", "k"
    assert s.is_ready()
    s.mode = "local"
    app._sync_mode(s)
    assert ui.PROVIDERS[s.provider].local and s.vision is False
    assert app.s.director.is_ready() is False
    app._refresh_setup_card()
    assert app.setup_card.visible


async def test_slot_limit_button_disabled(app):
    await app.start()
    for _ in range(25):
        app.store.add_slot()
    app._rebuild_all()
    assert len(app.s.slots) == 20


async def test_send_without_director_redirects(app):
    await app.start()
    app.go("chat")
    app.input.value = "hola"
    await app.on_send(None)
    assert app.view == "config" and app.task is None
