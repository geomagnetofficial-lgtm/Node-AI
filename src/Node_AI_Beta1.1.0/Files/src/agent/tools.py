from __future__ import annotations

import asyncio
import inspect
import os
import re
import signal
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from src.agent import apps
from src.agent.desktop import Desktop, DesktopError, Shot, UserAbort, diff_ratio, screen_changed
from src.agent.safety import EXEC_EXT, resolve_path

IS_WIN = os.name == "nt"
OUT_LIMIT = 6000
SKIP_DIRS = {".git", "node_modules", "__pycache__", "bin", "obj", ".venv", "venv", ".vs", "dist", "build"}

TOOL_DOCS: list[tuple[str, str, str]] = [
    ("screenshot", "{}", "Captura la pantalla. (Tras cada acción de ratón/teclado ya recibes una captura nueva.)"),
    ("click", '{"x":int,"y":int,"button":"left|right|middle"?,"double":bool?}', "Clic en coordenadas de la captura."),
    ("move", '{"x":int,"y":int}', "Mueve el ratón."),
    ("drag", '{"x1":int,"y1":int,"x2":int,"y2":int}', "Arrastra de un punto a otro."),
    ("scroll", '{"dy":int,"x":int?,"y":int?}', "Rueda del ratón: dy>0 sube, dy<0 baja (≈ líneas)."),
    ("type", '{"text":str,"window":str?}', "Escribe texto donde esté el foco (admite acentos). Haz clic en el campo antes; con 'window' se enfoca antes esa ventana."),
    ("key", '{"keys":"ctrl+s","window":str?}', "Teclas o atajos; varios separados por espacio: 'down down enter'. Con 'window' se enfoca antes esa ventana."),
    ("wait", '{"seconds":number}', "Espera (máx. 30) a que cargue algo."),
    ("windows", "{}", "Lista los títulos de las ventanas abiertas."),
    ("focus", '{"title":str}', "Trae al frente la ventana cuyo título contenga el texto."),
    ("open", '{"target":str,"args":[str]?}', "Abre una URL, archivo, carpeta o programa (por nombre o ruta: 'notepad', '7z', 'WinRAR', ruta a un .sln)."),
    ("run", '{"command":str,"cwd":str?,"timeout":int?}', "Ejecuta un comando en la terminal (PowerShell en Windows). Sin entrada interactiva."),
    ("read_file", '{"path":str,"start":int?,"end":int?}', "Lee un archivo de texto con números de línea (por defecto las primeras 400 líneas)."),
    ("write_file", '{"path":str,"content":str}', "Crea o sobrescribe un archivo de texto."),
    ("edit_file", '{"path":str,"old":str,"new":str,"replace_all":bool?}', "Reemplaza texto exacto en un archivo (old debe ser único salvo replace_all)."),
    ("list_dir", '{"path":str?}', "Lista una carpeta (por defecto el workspace)."),
    ("search", '{"pattern":regex,"path":str?,"glob":str?}', "Busca texto (regex) dentro de archivos, como grep."),
    ("archive", '{"op":"list|extract|create","archive":str,"files":[str]?,"dest":str?,"password":str?}',
     "7-Zip/WinRAR: listar, extraer (a dest) o crear (files -> archive). Formatos: .7z .zip .tar .gz .xz .bz2 .rar*  (*crear .rar requiere WinRAR)."),
    ("build", '{"project":str,"configuration":"Debug|Release"?,"tool":"auto|dotnet|msbuild|devenv"?,"platform":str?}',
     "Compila un .sln/.csproj/.vcxproj con dotnet, MSBuild o Visual Studio (devenv)."),
    ("find_app", '{"name":str}', "Localiza un programa instalado (7z, winrar, devenv, msbuild, code...)."),
    ("ask_table", '{"question":str}', "Consulta a la mesa de IAs para tener otras opiniones o revisar un plan."),
    ("ask_user", '{"question":str}', "Pregunta al usuario cuando falte información o necesites una decisión."),
    ("finish", '{"summary":str}', "Termina la tarea y entrega el resultado final al usuario."),
]

GUI_SHOT = {"click", "move", "drag", "scroll", "type", "key", "wait", "focus", "open", "screenshot"}
GUI_EFFECT = {"click", "move", "drag", "scroll", "type", "key", "focus", "open"}
NO_EFFECT_WARN = GUI_EFFECT - {"move"}
USAGE = {name: sig for name, sig, _ in TOOL_DOCS}


class ToolError(Exception):
    pass


class ArgError(ToolError):
    pass


@dataclass
class Observation:
    text: str
    image: Shot | None = None
    final: str | None = None
    error: bool = False
    changed: bool | None = None
    change_ratio: float | None = None


def clip(text: str, limit: int = OUT_LIMIT) -> str:
    text = text.strip("\n")
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.4), int(limit * 0.6)
    return f"{text[:head]}\n… [{len(text) - head - tail} caracteres omitidos] …\n{text[-tail:]}"


_TYPE_NAMES = {str: "un texto", int: "un número entero", float: "un número"}


def _need(args: dict, key: str, typ=str):
    v = args.get(key)
    if v is None or (typ is str and not str(v).strip() and key != "text"):
        raise ArgError(f"Falta el argumento '{key}'.")
    try:
        return typ(v)
    except (TypeError, ValueError):
        raise ArgError(f"El argumento '{key}' debe ser {_TYPE_NAMES.get(typ, typ.__name__)} (recibí {v!r}).")


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _shell_argv(command: str) -> list[str]:
    if IS_WIN:
        import shutil
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-Command",
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
        return ["cmd", "/c", command]
    return ["/bin/bash", "-lc", command]


def run_blocking(argv: list[str], cwd: Path, timeout: float, cancel: threading.Event) -> tuple[int | None, str, bool]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "GIT_TERMINAL_PROMPT": "0"}
    kw: dict = {}
    if IS_WIN:
        kw["creationflags"] = 0x08000000 | 0x00000200
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env=env, **kw)
    except (FileNotFoundError, PermissionError, NotADirectoryError, OSError) as e:
        raise ToolError(f"No se pudo ejecutar: {e}")
    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        try:
            out, _ = proc.communicate(timeout=0.3)
            break
        except subprocess.TimeoutExpired:
            if cancel.is_set() or time.monotonic() > deadline:
                timed_out = not cancel.is_set()
                _kill_tree(proc)
                out, _ = proc.communicate()
                break
    return proc.returncode, out or "", timed_out


class Toolbox:
    def __init__(self, cfg, workspace: Path, desktop: Desktop, *,
                 consult: Callable[[str], Awaitable[str]] | None = None,
                 ask_user: Callable[[str], Awaitable[str]] | None = None):
        self.cfg = cfg
        self.ws = Path(workspace)
        self.ws.mkdir(parents=True, exist_ok=True)
        self.desktop = desktop
        self.consult = consult
        self.ask_user = ask_user
        self.target_window = ""
        self.handlers: dict[str, Callable] = {
            "screenshot": lambda a: "captura", "click": self._click, "move": self._move, "drag": self._drag,
            "scroll": self._scroll, "type": self._type, "key": self._key, "wait": self._wait,
            "windows": self._windows, "focus": self._focus, "open": self._open, "run": self._run,
            "read_file": self._read_file, "write_file": self._write_file, "edit_file": self._edit_file,
            "list_dir": self._list_dir, "search": self._search, "archive": self._archive, "build": self._build,
            "find_app": self._find_app, "ask_table": self._ask_table, "ask_user": self._ask_user,
            "finish": self._finish}

    async def execute(self, action: str, args: dict) -> Observation:
        h = self.handlers.get(action)
        if h is None:
            return Observation(f"Acción desconocida '{action}'. Válidas: {', '.join(self.handlers)}.", error=True)
        if not isinstance(args, dict):
            return Observation("'args' debe ser un objeto JSON.", error=True)
        verify = action in GUI_EFFECT and bool(getattr(self.cfg, "verify_actions", True))
        before = None
        if verify and hasattr(self.desktop, "signature"):
            before = await asyncio.to_thread(self.desktop.signature)
        try:
            res = await (h(args) if inspect.iscoroutinefunction(h) else asyncio.to_thread(h, args))
            if isinstance(res, Observation):
                return res
            text = str(res)
        except ArgError as e:
            return Observation(f"{e} Uso: {action} {USAGE.get(action, '{}')}", error=True)
        except ToolError as e:
            return Observation(str(e), error=True)
        except (DesktopError, apps.AppError) as e:
            return await self._failed_gui(action, str(e))
        except UserAbort:
            raise
        except (OSError, ValueError, re.error) as e:
            return Observation(f"{type(e).__name__}: {e}", error=True)
        if action not in GUI_SHOT:
            return Observation(text)
        return await self._observe(action, text, before, verify)

    async def _failed_gui(self, action: str, message: str) -> Observation:
        obs = Observation(message, error=True)
        if action in ("click", "drag", "move"):
            self.desktop.grid_boost = True
            try:
                obs.image = await asyncio.to_thread(self.desktop.screenshot)
                obs.text += " Te adjunto la captura actual con rejilla de coordenadas para que corrijas la puntería."
            except DesktopError:
                pass
        return obs

    async def _observe(self, action: str, text: str, before: bytes | None, verify: bool) -> Observation:
        cfg = self.cfg
        settle = max(0.0, float(cfg.settle_s))
        extra: list[str] = []
        if action in GUI_EFFECT and settle > 0:
            if hasattr(self.desktop, "wait_stable"):
                stable, secs = await asyncio.to_thread(self.desktop.wait_stable, settle, max(settle, float(cfg.settle_max_s)))
                if not stable:
                    extra.append(f"La pantalla seguía cambiando tras {secs:.1f}s (¿cargando?). Usa wait y revisa de nuevo.")
            else:
                await asyncio.sleep(settle)
        try:
            shot = await asyncio.to_thread(self.desktop.screenshot)
        except DesktopError as e:
            return Observation(f"{text}\n(No se pudo capturar la pantalla: {e})", error=action == "screenshot")
        changed = ratio = None
        if verify and before is not None:
            changed, ratio = screen_changed(before, shot.sig), diff_ratio(before, shot.sig)
            if changed:
                extra.insert(0, f"Efecto visible: la pantalla cambió (~{(ratio or 0):.1%} de la imagen).")
            elif changed is False and action in NO_EFFECT_WARN:
                extra.insert(0, "AVISO: la pantalla NO cambió tras la acción; puede que no haya tenido efecto. Verifícalo antes de seguir.")
        if action == "click":
            failed = changed is False
            if failed != bool(getattr(self.desktop, "grid_boost", False)):
                self.desktop.grid_boost = failed
                if failed:
                    try:
                        shot = await asyncio.to_thread(self.desktop.screenshot)
                    except DesktopError:
                        pass
                    extra.append("Clic sin efecto visible: te muestro la captura con rejilla (líneas cada 50 px, etiquetas x,y) "
                                 "para ajustar las coordenadas; si el control no responde, prueba focus, doble clic o teclado.")
        if action in ("click", "open"):
            self.target_window = ""
        full = text + ("\n" + "\n".join(extra) if extra else "")
        return Observation(full, image=shot, changed=changed, change_ratio=ratio)

    def _click(self, a):
        return self.desktop.click(_need(a, "x", float), _need(a, "y", float), str(a.get("button", "left")),
                                  bool(a.get("double", False)))

    def _move(self, a):
        return self.desktop.move(_need(a, "x", float), _need(a, "y", float))

    def _drag(self, a):
        return self.desktop.drag(*(_need(a, k, float) for k in ("x1", "y1", "x2", "y2")))

    def _scroll(self, a):
        x, y = a.get("x"), a.get("y")
        return self.desktop.scroll(_need(a, "dy", int), None if x is None else float(x), None if y is None else float(y))

    def _prepare_typing(self, a) -> str:
        win = str(a.get("window") or "").strip()
        if win:
            self.target_window = win
        if not self.target_window or not hasattr(self.desktop, "ensure_focus"):
            return ""
        note = self.desktop.ensure_focus(self.target_window)
        return f" {note}" if note else ""

    def _typing_report(self) -> str:
        active = self.desktop.active_window() if hasattr(self.desktop, "active_window") else None
        return f" Ventana activa: {active}." if active else ""

    def _type(self, a):
        text = _need(a, "text")
        if not text:
            raise ToolError("'text' está vacío.")
        note = self._prepare_typing(a)
        return self.desktop.type_text(text) + note + self._typing_report()

    def _key(self, a):
        note = self._prepare_typing(a)
        return self.desktop.press(_need(a, "keys")) + note + self._typing_report()

    async def _wait(self, a):
        s = min(30.0, max(0.0, _need(a, "seconds", float)))
        await asyncio.sleep(s)
        return f"esperé {s:g}s"

    def _windows(self, a):
        titles = self.desktop.windows()
        return "Ventanas abiertas:\n" + "\n".join(f"- {t}" for t in titles[:60]) if titles else "Sin ventanas visibles."

    def _focus(self, a):
        title = _need(a, "title")
        msg = self.desktop.focus(title)
        self.target_window = title
        return msg

    def _open(self, a):
        target = _need(a, "target").strip().strip('"')
        args = [str(x) for x in (a.get("args") or [])]
        if target.lower().startswith(("http://", "https://")):
            webbrowser.open(target)
            return f"abierta la URL {target}"
        p = resolve_path(target, self.ws)
        if p.exists():
            if p.is_file() and p.suffix.lower() in EXEC_EXT - {".lnk"} and (IS_WIN or os.access(p, os.X_OK)):
                self._spawn([str(p), *args], p.parent)
            elif IS_WIN:
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                self._spawn(["xdg-open", str(p)], self.ws)
            return f"abierto: {p}"
        exe = apps.find_app(target)
        if exe:
            self._spawn([exe, *args], self.ws)
            return f"lanzado: {exe}"
        raise ToolError(f"No encuentro '{target}'. Prueba find_app, o run con 'where {target}'.")

    @staticmethod
    def _spawn(argv: list[str], cwd: Path) -> None:
        kw = ({"creationflags": 0x00000008 | 0x00000200} if IS_WIN else {"start_new_session": True})
        try:
            subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kw)
        except OSError as e:
            raise ToolError(f"No se pudo lanzar {argv[0]}: {e}")

    async def _proc(self, argv: list[str], cwd: Path, timeout: float) -> str:
        cancel = threading.Event()
        try:
            code, out, timed_out = await asyncio.to_thread(run_blocking, argv, cwd, timeout, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise
        head = f"[tiempo agotado tras {timeout:g}s: proceso terminado]" if timed_out else f"[código de salida {code}]"
        return f"{head}\n{clip(out)}" if out.strip() else head

    async def _run(self, a):
        cmd = _need(a, "command")
        cwd = resolve_path(a["cwd"], self.ws) if a.get("cwd") else self.ws
        if not cwd.is_dir():
            raise ToolError(f"cwd no es una carpeta existente: {cwd}")
        timeout = min(3600.0, max(1.0, float(a.get("timeout") or self.cfg.shell_timeout_s)))
        return await self._proc(_shell_argv(cmd), cwd, timeout)

    def _p(self, a, key="path", default=None) -> Path:
        v = a.get(key) or default
        if v is None:
            raise ToolError(f"Falta el argumento '{key}'.")
        return resolve_path(str(v), self.ws)

    def _read_file(self, a):
        p = self._p(a)
        if p.is_dir():
            return self._list_dir(a)
        if not p.is_file():
            raise ToolError(f"No existe el archivo: {p}")
        raw = p.read_bytes()
        if b"\0" in raw[:4096]:
            raise ToolError(f"{p.name} parece binario ({len(raw)} bytes); no se puede mostrar como texto.")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, int(a.get("start") or 1))
        end = min(len(lines), int(a.get("end") or start + 399))
        body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
        return f"{p} — líneas {start}-{end} de {len(lines)}\n{clip(body, 20000)}"

    def _write_file(self, a):
        p = self._p(a)
        content = a.get("content")
        if content is None:
            raise ToolError("Falta 'content'.")
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = str(content).encode("utf-8")
        p.write_bytes(data)
        return f"{'sobrescrito' if existed else 'creado'}: {p} ({len(data)} bytes)"

    def _edit_file(self, a):
        p = self._p(a)
        old, new = a.get("old"), a.get("new")
        if not p.is_file():
            raise ToolError(f"No existe el archivo: {p}")
        if not old:
            raise ToolError("'old' está vacío.")
        if new is None:
            raise ToolError("Falta 'new'.")
        text = p.read_bytes().decode("utf-8", errors="replace")
        crlf = "\r\n" in text
        text = text.replace("\r\n", "\n")
        old, new = str(old).replace("\r\n", "\n"), str(new).replace("\r\n", "\n")
        n = text.count(old)
        if n == 0:
            raise ToolError("No encontré 'old' en el archivo (¿espacios/saltos distintos?). Usa read_file y copia el texto exacto.")
        if n > 1 and not a.get("replace_all"):
            raise ToolError(f"'old' aparece {n} veces; añade contexto para hacerlo único o usa replace_all.")
        text = text.replace(old, new)
        p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
        return f"editado: {p} ({n} reemplazo{'s' if n > 1 else ''})"

    def _list_dir(self, a):
        p = self._p(a, default=".")
        if not p.is_dir():
            raise ToolError(f"No es una carpeta: {p}")
        rows = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:200]:
            rows.append(f"d {e.name}/" if e.is_dir() else f"f {e.name}  ({e.stat().st_size} B)")
        return f"{p}\n" + ("\n".join(rows) if rows else "(vacía)")

    def _search(self, a):
        rx = re.compile(_need(a, "pattern"), re.I)
        root = self._p(a, default=".")
        glob = str(a.get("glob") or "*")
        hits: list[str] = []
        files = [root] if root.is_file() else root.rglob(glob)
        for f in files:
            if len(hits) >= 80:
                break
            if not f.is_file() or any(part in SKIP_DIRS for part in f.relative_to(root.parent if root.is_file() else root).parts[:-1]):
                continue
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:2048]:
                continue
            for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 80:
                        break
        return "\n".join(hits) + ("\n(límite de 80 resultados)" if len(hits) >= 80 else "") if hits else "Sin coincidencias."

    async def _archive(self, a):
        op = _need(a, "op")
        arch = str(self._p(a, "archive"))
        files = [str(resolve_path(f, self.ws)) for f in (a.get("files") or [])]
        dest = str(resolve_path(a["dest"], self.ws)) if a.get("dest") else None
        if op == "extract" and dest:
            Path(dest).mkdir(parents=True, exist_ok=True)
        if op in ("list", "extract") and not Path(arch).is_file():
            raise ToolError(f"No existe el archivo comprimido: {arch}")
        argv = apps.archive_command(op, arch, files=files, dest=dest, password=a.get("password"))
        return await self._proc(argv, self.ws, float(a.get("timeout") or 600))

    async def _build(self, a):
        proj = self._p(a, "project")
        if not proj.is_file():
            raise ToolError(f"No existe el proyecto/solución: {proj}")
        argv = apps.build_command(str(proj), str(a.get("configuration") or "Debug"),
                                  str(a.get("tool") or "auto"), a.get("platform"))
        return await self._proc(argv, proj.parent, float(a.get("timeout") or 900))

    def _find_app(self, a):
        name = _need(a, "name")
        found = apps.find_app(name)
        return f"{name}: {found}" if found else f"{name}: no encontrado"

    async def _ask_table(self, a):
        if not self.consult:
            raise ToolError("La mesa no está disponible en este modo.")
        return clip(await self.consult(_need(a, "question")), 8000)

    async def _ask_user(self, a):
        if not self.ask_user:
            raise ToolError("No hay forma de preguntar al usuario aquí; decide con lo que sabes o usa finish.")
        ans = await self.ask_user(_need(a, "question"))
        return f"Respuesta del usuario: {ans}"

    def _finish(self, a):
        return Observation("", final=str(a.get("summary") or "Tarea terminada."))
