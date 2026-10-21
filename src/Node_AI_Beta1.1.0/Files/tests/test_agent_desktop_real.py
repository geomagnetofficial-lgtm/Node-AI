import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from src.agent.runner import AgentRunner
from src.core.settings import AgentSettings, AppSettings, ModelSlot

pytest.importorskip("mss")
pytest.importorskip("PIL")
if os.name != "nt" and not os.environ.get("DISPLAY"):
    pytest.skip("sin pantalla (DISPLAY)", allow_module_level=True)
try:
    import pyautogui  # noqa: F401
    import tkinter  # noqa: F401
except (Exception, SystemExit):
    pytest.skip("pyautogui/tkinter no disponibles", allow_module_level=True)

APP = textwrap.dedent("""
    import sys, tkinter as tk
    out, ready = sys.argv[1], sys.argv[2]
    root = tk.Tk(); root.geometry("500x300+600+300"); root.attributes("-topmost", True)
    e = tk.Entry(root, font=("TkDefaultFont", 14)); e.place(x=50, y=50, width=300, height=40)
    def done(_=None):
        open(out, "w", encoding="utf-8").write(e.get()); root.destroy()
    e.bind("<Return>", done); root.after(20000, root.destroy)
    root.update(); open(ready, "w").write("ok"); root.mainloop()
""")


class Scripted:
    def __init__(self, script): self.script, self.n = list(script), 0
    async def generate(self, slot, system, messages, images=None):
        self.n += 1
        return json.dumps(self.script.pop(0))


async def test_agent_clicks_types_and_submits_in_a_real_window(tmp_path):
    out, ready = tmp_path / "out.txt", tmp_path / "ready"
    proc = subprocess.Popen([sys.executable, "-c", APP, str(out), str(ready)])
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        assert ready.exists(), "la ventana de prueba no arrancó"
        time.sleep(0.5)
        s = AppSettings(director=ModelSlot(name="d", provider="custom", model="m"),
                        agent=AgentSettings(workspace=str(tmp_path / "ws"), shot_width=800, settle_s=0.2, max_steps=8))
        script = [{"action": "screenshot", "args": {}}]
        from src.agent.desktop import Desktop
        d = Desktop(shot_width=800)
        shot = d.screenshot()
        k = shot.width / shot.real_width
        cx, cy = round(800 * k), round(370 * k)
        script = [{"action": "click", "args": {"x": cx, "y": cy}},
                  {"action": "type", "args": {"text": "hola mundo"}},
                  {"action": "key", "args": {"keys": "enter"}},
                  {"action": "finish", "args": {"summary": "ok"}}]
        res = await AgentRunner(s, Scripted(script), desktop=d, log_dir=tmp_path / "logs").run("escribe")
        assert res.reason == "finished", res.final
        for _ in range(50):
            if out.exists():
                break
            time.sleep(0.1)
        assert out.read_text(encoding="utf-8") == "hola mundo"
    finally:
        proc.kill()
