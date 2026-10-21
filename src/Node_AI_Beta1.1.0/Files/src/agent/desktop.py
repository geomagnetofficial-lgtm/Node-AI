from __future__ import annotations

import base64
import difflib
import io
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

IS_WIN = os.name == "nt"

SIG_W, SIG_H = 160, 90
PIXEL_DELTA = 12
CHANGE_MIN = 2 / (SIG_W * SIG_H)
POINTER_TOLERANCE = 3
_DPI_DONE = False


def ensure_dpi_awareness() -> bool:
    global _DPI_DONE
    if _DPI_DONE or not IS_WIN:
        return _DPI_DONE
    _DPI_DONE = True
    try:
        import ctypes
        for call in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
                     lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
                     lambda: ctypes.windll.user32.SetProcessDPIAware()):
            try:
                call()
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def make_signature(img) -> bytes:
    from PIL import Image
    return img.convert("L").resize((SIG_W, SIG_H), Image.BOX).tobytes()


def diff_ratio(a: bytes | None, b: bytes | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    n = sum(1 for x, y in zip(a, b) if abs(x - y) > PIXEL_DELTA)
    return n / len(a)


def screen_changed(a: bytes | None, b: bytes | None) -> bool | None:
    r = diff_ratio(a, b)
    return None if r is None else r >= CHANGE_MIN


def map_point(x: float, y: float, img_size: tuple[int, int], real_size: tuple[int, int],
              origin: tuple[int, int]) -> tuple[int, int]:
    w, h = img_size
    rw, rh = real_size
    rx = min(rw - 1, max(0, round(x * rw / w)))
    ry = min(rh - 1, max(0, round(y * rh / h)))
    return origin[0] + rx, origin[1] + ry


class DesktopError(RuntimeError):
    pass


class UserAbort(Exception):
    pass


@dataclass
class Shot:
    b64: str
    mime: str
    width: int
    height: int
    real_width: int
    real_height: int
    sig: bytes | None = None
    monitor: int = 1
    note: str = ""

    def describe(self) -> str:
        txt = (f"Captura {self.width}x{self.height}px del monitor {self.monitor} "
               f"(pantalla real {self.real_width}x{self.real_height}). "
               f"Las coordenadas x,y que uses deben estar en el espacio de esta captura.")
        return f"{txt} {self.note}".strip()


_KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "windows": "win", "super": "win", "meta": "win", "cmd": "win",
    "escape": "esc", "return": "enter", "del": "delete", "ins": "insert", "pgup": "pageup",
    "pgdn": "pagedown", "pagedn": "pagedown", "spacebar": "space", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right", "option": "alt",
}


def normalize_keys(spec: str) -> list[list[str]]:
    groups = []
    for chunk in (spec or "").split():
        keys = [_KEY_ALIASES.get(k.strip().lower(), k.strip().lower()) for k in chunk.split("+") if k.strip()]
        if keys:
            groups.append(keys)
    return groups


class Desktop:
    def __init__(self, monitor: int = 1, shot_width: int = 1280, grid: bool = False):
        self.monitor = monitor
        self.shot_width = max(320, shot_width)
        self.grid = grid
        self._pg = None
        self._real_size = (0, 0)
        self._origin = (0, 0)
        self._img_size = (0, 0)
        self.grid_boost = False

    def _pyautogui(self):
        if self._pg is None:
            ensure_dpi_awareness()
            try:
                import pyautogui
            except (Exception, SystemExit) as e:
                raise DesktopError(f"No se puede controlar el ratón/teclado ({type(e).__name__}: {e}). "
                                   "Instala: pip install pyautogui pyperclip (en Linux además python3-tk)") from e
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.05
            self._pg = pyautogui
        return self._pg

    def _guard(self, fn, *a, **kw):
        pg = self._pyautogui()
        try:
            return fn(*a, **kw)
        except pg.FailSafeException as e:
            raise UserAbort("Abortado: ratón en la esquina superior izquierda.") from e

    def _grab(self):
        ensure_dpi_awareness()
        try:
            import mss
            from PIL import Image
        except Exception as e:
            raise DesktopError(f"Falta una dependencia para capturar pantalla ({e}). "
                               "Instala: pip install mss pillow") from e
        factory = getattr(mss, "MSS", None) or mss.mss
        try:
            with factory() as sct:
                mons = sct.monitors
                n_real = max(1, len(mons) - 1)
                note, idx = "", self.monitor
                if not 0 < idx < len(mons):
                    idx = 1 if len(mons) > 1 else 0
                    note = f"(El monitor {self.monitor} no existe: hay {n_real}. Uso el {idx or 1}.)"
                elif n_real > 1:
                    note = f"(Hay {n_real} monitores; esta captura es solo del {idx}.)"
                mon = mons[idx]
                raw = sct.grab(mon)
        except Exception as e:
            raise DesktopError(f"No se pudo capturar la pantalla: {e}") from e
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX"), mon, note, idx or 1

    def signature(self) -> bytes | None:
        try:
            sig = make_signature(self._grab()[0])
        except Exception:
            return None
        return sig

    def screenshot(self) -> Shot:
        from PIL import Image
        img, mon, note, idx = self._grab()
        rw, rh = img.size
        sig = make_signature(img)
        if rw > self.shot_width:
            img = img.resize((self.shot_width, max(1, round(rh * self.shot_width / rw))), Image.LANCZOS)
        self._img_size = img.size
        self._real_size = (rw, rh)
        self._origin = (mon["left"], mon["top"])
        self._decorate(img)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=72)
        return Shot(base64.b64encode(buf.getvalue()).decode(), "image/jpeg", img.size[0], img.size[1], rw, rh,
                    sig=sig, monitor=idx, note=note)

    def wait_stable(self, min_wait: float = 0.0, timeout: float = 3.0, interval: float = 0.2) -> tuple[bool, float]:
        t0 = time.monotonic()
        if min_wait > 0:
            time.sleep(min(min_wait, timeout))
        prev = self.signature()
        while time.monotonic() - t0 < timeout:
            time.sleep(interval)
            cur = self.signature()
            if prev is not None and cur is not None and screen_changed(prev, cur) is False:
                return True, time.monotonic() - t0
            prev = cur
        return False, time.monotonic() - t0

    def _decorate(self, img) -> None:
        from PIL import Image, ImageDraw
        w, h = img.size
        if self.grid or self.grid_boost:
            ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            step = 50 if self.grid_boost else 100
            alpha = 110 if self.grid_boost else 70
            for x in range(0, w, step):
                d.line([(x, 0), (x, h)], fill=(255, 255, 0, alpha), width=1)
            for y in range(0, h, step):
                d.line([(0, y), (w, y)], fill=(255, 255, 0, alpha), width=1)
            lab = step * 2
            for x in range(0, w, lab):
                for y in range(0, h, lab):
                    d.text((x + 2, y + 2), f"{x},{y}", fill=(255, 255, 0, 220))
            img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"))
        try:
            px, py = self._pyautogui().position()
            cx = (px - self._origin[0]) * w / self._real_size[0]
            cy = (py - self._origin[1]) * h / self._real_size[1]
            if 0 <= cx < w and 0 <= cy < h:
                d = ImageDraw.Draw(img)
                d.ellipse([cx - 9, cy - 9, cx + 9, cy + 9], outline=(255, 0, 0), width=2)
                d.line([(cx - 14, cy), (cx + 14, cy)], fill=(255, 0, 0), width=1)
                d.line([(cx, cy - 14), (cx, cy + 14)], fill=(255, 0, 0), width=1)
        except Exception:
            pass

    def to_real(self, x: float, y: float) -> tuple[int, int]:
        w, h = self._img_size
        if not w:
            raise DesktopError("Aún no hay captura: usa la acción 'screenshot' antes de hacer clic.")
        x, y = float(x), float(y)
        if not (0 <= x <= w and 0 <= y <= h):
            raise DesktopError(f"Coordenadas ({x:.0f},{y:.0f}) fuera de la captura: x debe estar en 0..{w} e y en 0..{h}. "
                               "Mira la captura más reciente y usa sus coordenadas.")
        return map_point(x, y, self._img_size, self._real_size, self._origin)

    def _pointer_note(self, rx: int, ry: int) -> str:
        try:
            px, py = self._pyautogui().position()
        except Exception:
            return ""
        if abs(px - rx) > POINTER_TOLERANCE or abs(py - ry) > POINTER_TOLERANCE:
            return (f" AVISO: el ratón quedó en ({px},{py}) y no en ({rx},{ry}) reales: posible desajuste de escala/monitor; "
                    "verifica en la captura.")
        return ""

    def click(self, x, y, button: str = "left", double: bool = False) -> str:
        if button not in ("left", "right", "middle"):
            raise DesktopError("button debe ser left, right o middle")
        rx, ry = self.to_real(x, y)
        pg = self._pyautogui()
        self._guard(pg.moveTo, rx, ry, duration=0.15)
        note = self._pointer_note(rx, ry)
        self._guard(pg.click, button=button, clicks=2 if double else 1, interval=0.08)
        return f"{'doble ' if double else ''}clic {button} en ({x:.0f},{y:.0f}){note}"

    def move(self, x, y) -> str:
        rx, ry = self.to_real(x, y)
        self._guard(self._pyautogui().moveTo, rx, ry, duration=0.15)
        return f"ratón movido a ({x:.0f},{y:.0f}){self._pointer_note(rx, ry)}"

    def drag(self, x1, y1, x2, y2) -> str:
        a, b = self.to_real(x1, y1), self.to_real(x2, y2)
        pg = self._pyautogui()
        self._guard(pg.moveTo, *a, duration=0.15)
        self._guard(pg.dragTo, *b, duration=0.5, button="left")
        return f"arrastrado de ({x1:.0f},{y1:.0f}) a ({x2:.0f},{y2:.0f})"

    def scroll(self, dy: int, x=None, y=None) -> str:
        pg = self._pyautogui()
        if x is not None and y is not None:
            self._guard(pg.moveTo, *self.to_real(x, y), duration=0.1)
        self._guard(pg.scroll, int(dy))
        return f"scroll {int(dy):+d}"

    def type_text(self, text: str) -> str:
        pg = self._pyautogui()
        if text.isascii() and len(text) <= 200 and "\t" not in text:
            self._guard(pg.write, text, interval=0.01)
            return f"escrito ({len(text)} caracteres)"
        try:
            import pyperclip
        except Exception as e:
            raise DesktopError("Para escribir acentos/texto largo hace falta: pip install pyperclip") from e
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
        pyperclip.copy(text)
        self._guard(pg.hotkey, "command" if sys.platform == "darwin" else "ctrl", "v")
        time.sleep(0.25)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
        return f"pegado ({len(text)} caracteres, vía portapapeles)"

    def press(self, spec: str) -> str:
        groups = normalize_keys(spec)
        if not groups:
            raise DesktopError("keys vacío. Ejemplos: 'enter', 'ctrl+s', 'alt+f4', 'win+r'.")
        pg = self._pyautogui()
        valid = set(pg.KEYBOARD_KEYS)
        for g in groups:
            bad = [k for k in g if k not in valid]
            if bad:
                hints = []
                for k in bad:
                    close = difflib.get_close_matches(k, valid, n=3)
                    hints.append(f"'{k}'" + (f" (¿quisiste decir {', '.join(repr(c) for c in close)}?)" if close else ""))
                raise DesktopError(f"Tecla(s) desconocida(s): {', '.join(hints)}. Ejemplos válidos: 'enter', 'ctrl+s', 'alt+f4', 'win+r'.")
        for g in groups:
            self._guard(pg.hotkey if len(g) > 1 else pg.press, *g)
            time.sleep(0.05)
        return f"teclas: {spec}"

    def windows(self) -> list[str]:
        if IS_WIN:
            return _win_titles()
        if shutil.which("wmctrl"):
            out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
            return [l.split(None, 3)[-1] for l in out.splitlines() if l.strip()]
        raise DesktopError("Listar ventanas solo está soportado en Windows (o Linux con wmctrl).")

    def active_window(self) -> str | None:
        try:
            if IS_WIN:
                return _win_foreground()
            if shutil.which("xdotool"):
                r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=5)
                return r.stdout.strip() or None
        except Exception:
            return None
        return None

    def ensure_focus(self, title: str) -> str | None:
        want = title.strip().lower()
        if not want:
            return None
        cur = self.active_window()
        if cur and want in cur.lower():
            return None
        self.focus(title)
        time.sleep(0.25)
        return f"Enfoqué la ventana '{title}' antes de actuar (estaba activa: {cur or 'desconocida'})."

    def focus(self, title: str) -> str:
        if IS_WIN:
            return _win_focus(title)
        if shutil.which("wmctrl"):
            r = subprocess.run(["wmctrl", "-a", title], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return f"ventana activada: {title}"
            raise DesktopError(f"No hay ninguna ventana con '{title}'.")
        raise DesktopError("Enfocar ventanas solo está soportado en Windows (o Linux con wmctrl).")


def _win_enum() -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    cb_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                found.append((hwnd, buf.value))
        return True
    u.EnumWindows(cb_t(cb), 0)
    return found


def _win_foreground() -> str | None:
    import ctypes
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(hwnd)
    if not n:
        return None
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _win_titles() -> list[str]:
    return [t for _, t in _win_enum()]


def _win_focus(title: str) -> str:
    import ctypes
    u = ctypes.windll.user32
    want = title.lower()
    for hwnd, t in _win_enum():
        if want in t.lower():
            u.ShowWindow(hwnd, 9)
            u.keybd_event(0x12, 0, 0, 0)
            u.SetForegroundWindow(hwnd)
            u.keybd_event(0x12, 0, 2, 0)
            return f"ventana activada: {t}"
    raise DesktopError(f"No hay ninguna ventana visible con '{title}'.")
