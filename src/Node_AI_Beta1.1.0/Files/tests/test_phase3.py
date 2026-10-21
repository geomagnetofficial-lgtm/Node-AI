from __future__ import annotations

import pytest
from PIL import Image

from src.agent import desktop as dk
from src.agent.desktop import Desktop, DesktopError, Shot, diff_ratio, make_signature, map_point, screen_changed
from src.agent.loops import LoopDetector
from src.agent.tools import Observation, Toolbox
from src.core.orchestrator import Orchestrator
from src.core.settings import MAX_ROUNDS, AgentSettings, AppSettings, ModelSlot
from tests.test_agent import FakeDesktop, act, runner


def sig_of(fill: int, box: tuple[int, int, int, int] | None = None, size=(640, 360), value: int = 255) -> bytes:
    img = Image.new("L", size, fill)
    if box:
        img.paste(value, box)
    return make_signature(img)


class SigDesktop(FakeDesktop):
    def __init__(self, changing: bool = False):
        super().__init__()
        self.n = 0
        self.changing = changing
        self.grid_boost = False
        self.focus_calls: list[str] = []
        self.active = "Bloc de notas"
        self.stable_calls: list[tuple] = []
        self.stable_result = (True, 0.1)

    def _cur(self) -> bytes:
        return sig_of(10 + ((self.n * 40) % 220 if self.changing else 0))

    def signature(self):
        return self._cur()

    def screenshot(self):
        return Shot("QUJD", "image/jpeg", 800, 450, 1600, 900, sig=self._cur())

    def click(self, x, y, button="left", double=False):
        self.n += 1
        return self._rec("click", x, y, button, double)

    def type_text(self, t):
        self.n += 1
        return self._rec("type", t)

    def ensure_focus(self, title):
        self.focus_calls.append(title)
        return f"Enfoqué '{title}'."

    def active_window(self):
        return self.active

    def wait_stable(self, min_wait=0.0, timeout=3.0, interval=0.2):
        self.stable_calls.append((min_wait, timeout))
        return self.stable_result


class CountingClient:
    def __init__(self):
        self.calls = 0

    async def generate(self, slot, system, messages, images=None):
        self.calls += 1
        return "ok"


@pytest.mark.parametrize("rounds,expected", [(1, 1), (4, 4), (10, 10), (MAX_ROUNDS + 40, MAX_ROUNDS), (0, 1), (-3, 1)])
async def test_rounds_have_no_cap_of_three(rounds, expected):
    s = AppSettings(rounds=rounds, slots=[ModelSlot(name="a", provider="custom", model="m", base_url="http://x")])
    client = CountingClient()
    turns, last = await Orchestrator(s, client)._table("hola", [], [], None)
    assert client.calls == expected and len(turns) == expected and last[0].round == expected


async def test_unlimited_finishes_with_finish_past_old_limit(tmp_path):
    script = [act("wait", seconds=i / 1000) for i in range(25)] + [act("finish", summary="listo")]
    r, client, events = runner(tmp_path, script, max_steps=3, unlimited_steps=True)
    res = await r.run("tarea")
    assert res.reason == "finished" and res.steps == 26 and res.final == "listo"
    last_obs = client.calls[-1]["messages"][-1]["content"]
    assert "(paso 25)" in last_obs and "/" not in last_obs.split("(paso")[-1]
    assert "Quedan pocos pasos" not in last_obs
    start = next(p for k, p in events if k == "start")
    assert start["unlimited"] is True and start["max_steps"] is None


async def test_unlimited_prompt_mentions_no_limit(tmp_path):
    r, client, _ = runner(tmp_path, [act("finish")], unlimited_steps=True)
    await r.run("t")
    assert "NO hay límite de pasos" in client.calls[0]["system"]


async def test_unlimited_stops_on_identical_repeats(tmp_path):
    r, client, _ = runner(tmp_path, [act("wait", seconds=0)] * 12, unlimited_steps=True, loop_repeat=5, max_steps=2)
    res = await r.run("t")
    assert res.reason == "loop" and res.steps == 5 and "repitió 5 veces" in res.final
    assert "misma acción" in client.calls[3]["messages"][-1]["content"]


async def test_unlimited_stops_when_screen_never_changes(tmp_path):
    d = SigDesktop(changing=False)
    script = [act("click", x=10 + i, y=20) for i in range(30)]
    r, _, _ = runner(tmp_path, script, desktop=d, unlimited_steps=True, stall_limit=4)
    res = await r.run("t")
    assert res.reason == "loop" and "sin ningún cambio visible" in res.final and res.steps == 4


async def test_changing_screen_is_not_a_loop(tmp_path):
    d = SigDesktop(changing=True)
    script = [act("click", x=10, y=20) for _ in range(12)] + [act("finish")]
    r, _, _ = runner(tmp_path, script, desktop=d, unlimited_steps=True, loop_repeat=5, stall_limit=4)
    res = await r.run("t")
    assert res.reason == "finished" and res.steps == 13


async def test_unlimited_still_stops_on_three_format_errors(tmp_path):
    r, _, _ = runner(tmp_path, ["no json", "tampoco", "nada"], unlimited_steps=True)
    assert (await r.run("t")).reason == "protocol"


async def test_unlimited_keeps_blocked_actions_blocked(tmp_path):
    r, _, events = runner(tmp_path, [act("run", command="rm -rf /"), act("finish")], unlimited_steps=True,
                          approval="autonomous")
    await r.run("t")
    assert [p.status for k, p in events if k == "observation"] == ["blocked"]


async def test_limited_mode_still_caps_steps_and_does_not_hard_stop_loops(tmp_path):
    r, client, _ = runner(tmp_path, [act("wait", seconds=0)] * 8, max_steps=8)
    res = await r.run("t")
    assert res.reason == "max_steps" and res.steps == 8
    last = client.calls[-1]["messages"][-1]["content"]
    assert "(paso 7/8)" in last and "Quedan pocos pasos" in last


def test_loop_detector_unit():
    d = LoopDetector(repeat_limit=3, stall_limit=3)
    same = Observation("igual")
    assert d.update("run", {"command": "x"}, same) is None
    assert d.update("run", {"command": "x"}, same) is None
    assert "repitió 3 veces" in d.update("run", {"command": "x"}, same)
    d = LoopDetector(repeat_limit=9, stall_limit=3)
    for i in range(2):
        assert d.update("click", {"x": i}, Observation(f"c{i}", changed=False)) is None
    assert d.update("click", {"x": 9}, Observation("c9", changed=True)) is None and d.stalled == 0
    for i in range(2):
        d.update("click", {"x": i}, Observation(f"d{i}", changed=False))
    assert "sin ningún cambio" in d.update("key", {"keys": "a"}, Observation("k", changed=False))
    stalled = d.stalled
    d.update("wait", {}, Observation("w", changed=False))
    assert d.stalled == stalled


def test_map_point_scales_per_axis_and_clamps():
    assert map_point(400, 225, (800, 450), (1600, 900), (0, 0)) == (800, 450)
    assert map_point(800, 450, (800, 450), (1600, 900), (0, 0)) == (1599, 899)
    assert map_point(0, 0, (1280, 720), (2560, 1440), (0, 0)) == (0, 0)
    assert map_point(100, 100, (1280, 721), (2560, 1442), (0, 0)) == (200, 200)


def test_map_point_multi_monitor_origins():
    assert map_point(100, 50, (800, 450), (1600, 900), (1920, 0)) == (2120, 100)
    assert map_point(100, 50, (800, 450), (1600, 900), (-1600, -200)) == (-1400, -100)


def test_to_real_errors_are_actionable():
    d = Desktop()
    with pytest.raises(DesktopError, match="screenshot"):
        d.to_real(1, 1)
    d._img_size, d._real_size, d._origin = (800, 450), (1600, 900), (1920, 0)
    assert d.to_real(10, 10) == (1940, 20)
    with pytest.raises(DesktopError, match="0..800"):
        d.to_real(900, 10)


def test_signature_detects_small_and_ignores_identical():
    base = sig_of(30)
    assert screen_changed(base, sig_of(30)) is False and diff_ratio(base, sig_of(30)) == 0
    assert screen_changed(base, sig_of(30, (100, 100, 120, 114))) is True
    assert screen_changed(None, base) is None and screen_changed(base, b"xx") is None


def test_wait_stable_detects_settling(monkeypatch):
    frames = [sig_of(10), sig_of(60), sig_of(120), sig_of(120), sig_of(120)]
    d = Desktop()
    monkeypatch.setattr(dk.time, "sleep", lambda s: None)
    monkeypatch.setattr(Desktop, "signature", lambda self: frames.pop(0) if frames else None)
    stable, _ = d.wait_stable(min_wait=0.0, timeout=5.0, interval=0.0)
    assert stable is True and len(frames) == 1


def test_wait_stable_times_out_if_always_changing(monkeypatch):
    n = {"i": 0}

    def sig(self):
        n["i"] += 1
        return sig_of(n["i"] * 40 % 250)
    clock = iter(range(0, 1000))
    monkeypatch.setattr(dk.time, "sleep", lambda s: None)
    monkeypatch.setattr(dk.time, "monotonic", lambda: next(clock) / 4)
    monkeypatch.setattr(Desktop, "signature", sig)
    stable, secs = Desktop().wait_stable(timeout=2.0, interval=0.0)
    assert stable is False and secs >= 2.0


def test_dpi_awareness_is_set_before_any_capture(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(dk, "ensure_dpi_awareness", lambda: order.append("dpi"))
    try:
        Desktop()._grab()
    except DesktopError:
        pass
    assert order and order[0] == "dpi"


def test_ensure_dpi_awareness_noop_outside_windows(monkeypatch):
    monkeypatch.setattr(dk, "IS_WIN", False)
    monkeypatch.setattr(dk, "_DPI_DONE", False)
    assert dk.ensure_dpi_awareness() is False


class FakePG:
    KEYBOARD_KEYS = ["enter", "esc", "ctrl", "s", "tab"]

    class FailSafeException(Exception):
        pass

    def __init__(self, pos=(0, 0)):
        self.pos = pos
        self.pressed = []

    def position(self):
        return self.pos

    def press(self, *k):
        self.pressed.append(k)

    def hotkey(self, *k):
        self.pressed.append(k)


def test_press_unknown_key_suggests_close_match():
    d = Desktop()
    d._pg = FakePG()
    with pytest.raises(DesktopError, match=r"enter"):
        d.press("entr")


def test_pointer_mismatch_is_reported():
    d = Desktop()
    d._pg = FakePG(pos=(500, 500))
    assert "AVISO" in d._pointer_note(800, 450)
    d._pg = FakePG(pos=(801, 451))
    assert d._pointer_note(800, 450) == ""


def toolbox(tmp_path, desktop, **kw):
    cfg = AgentSettings(workspace=str(tmp_path / "ws"), settle_s=0, **kw)
    return Toolbox(cfg, tmp_path / "ws", desktop)


async def test_click_with_effect_reports_change(tmp_path):
    tb = toolbox(tmp_path, SigDesktop(changing=True))
    obs = await tb.execute("click", {"x": 5, "y": 5})
    assert obs.changed is True and "Efecto visible" in obs.text and obs.change_ratio > 0


async def test_click_without_effect_warns_and_enables_grid_then_disables(tmp_path):
    d = SigDesktop(changing=False)
    tb = toolbox(tmp_path, d)
    obs = await tb.execute("click", {"x": 5, "y": 5})
    assert obs.changed is False and "NO cambió" in obs.text and "rejilla" in obs.text and d.grid_boost is True
    d.changing = True
    obs = await tb.execute("click", {"x": 5, "y": 5})
    assert obs.changed is True and d.grid_boost is False


async def test_move_without_visible_change_is_not_a_warning(tmp_path):
    obs = await toolbox(tmp_path, SigDesktop()).execute("move", {"x": 1, "y": 1})
    assert "AVISO" not in obs.text


async def test_verification_can_be_disabled(tmp_path):
    obs = await toolbox(tmp_path, SigDesktop(), verify_actions=False).execute("click", {"x": 5, "y": 5})
    assert obs.changed is None and "AVISO" not in obs.text


async def test_click_error_attaches_grid_screenshot(tmp_path):
    d = SigDesktop()
    d.fail = DesktopError("Coordenadas fuera de la captura")
    obs = await toolbox(tmp_path, d).execute("click", {"x": 5, "y": 5})
    assert obs.error and obs.image is not None and "rejilla" in obs.text and d.grid_boost is True


async def test_argument_errors_include_usage(tmp_path):
    tb = toolbox(tmp_path, SigDesktop())
    obs = await tb.execute("click", {"y": 5})
    assert obs.error and "Falta el argumento 'x'" in obs.text and 'Uso: click {"x":int' in obs.text
    obs = await tb.execute("scroll", {"dy": "mucho"})
    assert "debe ser un número entero" in obs.text and "Uso: scroll" in obs.text


async def test_settle_waits_for_stability_when_available(tmp_path):
    d = SigDesktop(changing=True)
    d.stable_result = (False, 3.2)
    cfg = AgentSettings(workspace=str(tmp_path / "ws"), settle_s=0.5, settle_max_s=4.0)
    obs = await Toolbox(cfg, tmp_path / "ws", d).execute("click", {"x": 1, "y": 1})
    assert d.stable_calls == [(0.5, 4.0)] and "seguía cambiando" in obs.text


async def test_focus_is_enforced_before_typing_until_a_click(tmp_path):
    d = SigDesktop(changing=True)
    tb = toolbox(tmp_path, d)
    await tb.execute("focus", {"title": "Notepad"})
    obs = await tb.execute("type", {"text": "hola"})
    assert d.focus_calls == ["Notepad"] and "Enfoqué" in obs.text and "Ventana activa: Bloc de notas" in obs.text
    await tb.execute("click", {"x": 1, "y": 1})
    await tb.execute("type", {"text": "otra"})
    assert d.focus_calls == ["Notepad"]


async def test_window_arg_focuses_before_key(tmp_path):
    d = SigDesktop(changing=True)
    tb = toolbox(tmp_path, d)
    await tb.execute("key", {"keys": "ctrl+s", "window": "Editor"})
    assert d.focus_calls == ["Editor"]
