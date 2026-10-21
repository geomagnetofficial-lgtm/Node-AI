from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SAFE, NORMAL, RISKY, BLOCKED = "safe", "normal", "risky", "blocked"
MODES = ("strict", "balanced", "autonomous")

SAFE_ACTIONS = {"screenshot", "wait", "windows", "list_dir", "read_file", "search", "find_app",
                "ask_table", "ask_user", "finish", "scroll", "move"}
GUI_ACTIONS = {"click", "drag", "type", "key", "focus"}
EXEC_EXT = {".exe", ".bat", ".cmd", ".ps1", ".msi", ".vbs", ".js", ".jse", ".wsf", ".scr", ".com", ".sh",
            ".reg", ".lnk", ".jar", ".py"}


@dataclass
class Verdict:
    level: str
    reason: str = ""


_BLOCKED = [
    (r"\bformat(\.com)?\s+[a-z]:", "formatear una unidad"),
    (r"\b(diskpart|bcdedit|mkfs(\.\w+)?|fdisk|parted)\b", "herramienta de particiones/arranque"),
    (r"\bcipher\s+/w", "borrado seguro de disco"),
    (r"\b(vssadmin|wbadmin|wmic\s+shadowcopy)\b.*\bdelete\b", "borrar copias de seguridad/instantáneas"),
    (r"\breg(\.exe)?\s+(delete|add)\s+[\"']?hk(lm|ey_local_machine)", "modificar el registro del sistema"),
    (r"\b(shutdown|restart-computer|stop-computer|poweroff|reboot|halt)\b", "apagar/reiniciar el equipo"),
    (r"\brm\s+(-\w+\s+)*-\w*[rR]\w*\s+(-\w+\s+)*(/|~|\*|\$home|/\*)(\s|$)", "borrado recursivo de raíz/home"),
    (r"\bdd\s+.*\bof=/dev/", "escribir directamente en un dispositivo"),
    (r"(del|erase|rd|rmdir)\s+.*(/s|/q).*\b[a-z]:\\(windows|users|program files)?\\?\s*$", "borrado masivo de sistema"),
    (r"remove-item(?=.*-recurse)(?=.*([a-z]:\\?(\s|$)|[a-z]:\\windows|[a-z]:\\users\\?(\s|$)|\$env:(systemroot|userprofile|windir)))",
     "borrado recursivo de carpetas del sistema"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"\bnet\s+(user|localgroup)\b.*(/add|/delete)", "gestionar usuarios del sistema"),
    (r"\bpowershell(\.exe)?\b.*\s-(e|ec|enc|encodedcommand)\s", "comando PowerShell ofuscado (-EncodedCommand)"),
    (r"(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|;&]*\|\s*(sh|bash|zsh|iex|invoke-expression|powershell|pwsh)\b",
     "descargar y ejecutar código remoto"),
    (r"\b(set-mppreference|add-mppreference)\b|disable.{0,20}defender|netsh\s+advfirewall\s+set.*off", "desactivar protecciones del sistema"),
    (r"\bschtasks\b.*\/create|\bsc(\.exe)?\s+(create|config)\b", "crear tareas/servicios persistentes"),
]
_BLOCKED_RE = [(re.compile(p, re.I), why) for p, why in _BLOCKED]

_READONLY: dict[str, set[str] | None] = {
    "dir": None, "ls": None, "pwd": None, "cd": None, "echo": None, "type": None, "cat": None, "where": None,
    "which": None, "whoami": None, "hostname": None, "tree": None, "findstr": None, "grep": None,
    "head": None, "tail": None, "wc": None, "get-childitem": None, "get-content": None, "get-location": None,
    "select-string": None, "test-path": None, "get-item": None, "get-process": None, "date": None,
    "git": {"status", "log", "diff", "show", "branch", "rev-parse", "remote", "ls-files", "describe"},
    "python": {"--version", "-v", "-V"}, "py": {"--version", "-V"}, "node": {"--version", "-v"},
    "npm": {"--version", "-v", "ls", "list"}, "dotnet": {"--info", "--version", "--list-sdks", "--list-runtimes"},
}
_SPLIT = re.compile(r"&&|\|\||;|\||&|\n")
_DANGEROUS_SYNTAX = re.compile(r">|`|\$\(|<\(|\bout-file\b|\bset-content\b|\badd-content\b|\bnew-item\b", re.I)


def shell_verdict(cmd: str) -> Verdict:
    for rx, why in _BLOCKED_RE:
        if rx.search(cmd):
            return Verdict(BLOCKED, f"Comando bloqueado ({why}).")
    if _DANGEROUS_SYNTAX.search(cmd):
        return Verdict(RISKY, "El comando redirige salida, escribe archivos o usa sustitución.")
    for part in _SPLIT.split(cmd):
        toks = part.strip().lstrip("&").strip().split()
        if not toks:
            continue
        first = toks[0].strip("\"'").replace("\\", "/").split("/")[-1].lower().removesuffix(".exe")
        if first not in _READONLY:
            return Verdict(RISKY, f"'{first}' no está en la lista de comandos de solo lectura.")
        allowed = _READONLY[first]
        if allowed is not None and (len(toks) < 2 or toks[1] not in allowed):
            sub = " ".join(toks[:2])
            return Verdict(RISKY, f"'{sub}' puede modificar cosas.")
    return Verdict(SAFE if cmd.strip() else NORMAL, "solo lectura")


def resolve_path(p: str, workspace: Path) -> Path:
    q = Path(os.path.expandvars(os.path.expanduser(str(p).strip().strip('"'))))
    if not q.is_absolute():
        q = workspace / q
    return q.resolve()


def inside(p: Path, workspace: Path) -> bool:
    try:
        p.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def classify(action: str, args: dict, workspace: Path) -> Verdict:
    a = args or {}
    ws = Path(workspace)
    if action in SAFE_ACTIONS:
        return Verdict(SAFE)
    if action in GUI_ACTIONS:
        return Verdict(NORMAL, "interacción con la pantalla")
    if action == "run":
        v = shell_verdict(str(a.get("command", "")))
        cwd = a.get("cwd")
        if v.level == SAFE and cwd and not inside(resolve_path(cwd, ws), ws):
            return Verdict(NORMAL, "lectura fuera del workspace")
        return v
    if action in ("write_file", "edit_file"):
        target = resolve_path(a.get("path", ""), ws)
        if not inside(target, ws):
            return Verdict(RISKY, f"escribe fuera del workspace ({target})")
        return Verdict(NORMAL, "edita un archivo del workspace")
    if action == "archive":
        op = a.get("op")
        if op == "list":
            return Verdict(SAFE)
        paths = [a.get("dest")] if op == "extract" else [a.get("archive")]
        for p in paths:
            if p and not inside(resolve_path(p, ws), ws):
                return Verdict(RISKY, f"{op} fuera del workspace ({p})")
        return Verdict(NORMAL, f"{op} dentro del workspace")
    if action == "build":
        return Verdict(NORMAL, "compila un proyecto (puede ejecutar pasos de build)")
    if action == "open":
        t = str(a.get("target", ""))
        if t.lower().startswith(("http://", "https://")):
            return Verdict(NORMAL, "abre una URL")
        if Path(t.strip('"')).suffix.lower() in EXEC_EXT:
            return Verdict(RISKY, f"lanza/abre un ejecutable o script ({t})")
        return Verdict(NORMAL, "abre una aplicación o documento")
    return Verdict(RISKY, f"acción desconocida '{action}'")


def needs_approval(v: Verdict, mode: str) -> bool:
    if v.level in (SAFE, BLOCKED):
        return False
    if mode == "autonomous":
        return False
    if mode == "strict":
        return True
    return v.level == RISKY


def describe(action: str, args: dict) -> str:
    a = dict(args or {})
    if "password" in a:
        a["password"] = "***"
    if action == "run":
        return f"Ejecutar: {a.get('command', '')}" + (f"  (en {a['cwd']})" if a.get("cwd") else "")
    if action == "write_file":
        return f"Escribir {len(str(a.get('content', '')))} caracteres en {a.get('path')}"
    if action == "edit_file":
        return f"Editar {a.get('path')}"
    if action == "type":
        t = str(a.get("text", ""))
        return f"Teclear: {t[:80]}{'…' if len(t) > 80 else ''}"
    items = ", ".join(f"{k}={str(v)[:60]}" for k, v in a.items())
    return f"{action}({items})"
