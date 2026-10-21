from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

IS_WIN = os.name == "nt"


class AppError(RuntimeError):
    pass


def _env_dirs() -> list[Path]:
    out = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        v = os.environ.get(var)
        if v:
            out.append(Path(v))
    return out


def _first_existing(paths) -> str | None:
    for p in paths:
        if p and Path(p).is_file():
            return str(p)
    return None


def _app_paths_registry(exe: str) -> str | None:
    if not IS_WIN:
        return None
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val and Path(val).is_file():
                        return val
            except OSError:
                continue
    except Exception:
        pass
    return None


def find_7zip() -> str | None:
    cands = [shutil.which(n) for n in ("7z", "7za", "7zz", "7zr")]
    cands += [d / "7-Zip" / "7z.exe" for d in _env_dirs()]
    found = _first_existing(cands)
    if found:
        return found
    reg = _app_paths_registry("7zFM.exe")
    return _first_existing([Path(reg).with_name("7z.exe")]) if reg else None


def find_winrar() -> dict[str, str | None]:
    dirs = [d / "WinRAR" for d in _env_dirs()]
    reg = _app_paths_registry("WinRAR.exe")
    if reg:
        dirs.insert(0, Path(reg).parent)
    res = {}
    for key, exe in (("winrar", "WinRAR.exe"), ("rar", "Rar.exe"), ("unrar", "UnRAR.exe")):
        res[key] = _first_existing([shutil.which(exe)] + [d / exe for d in dirs])
    return res


def find_vswhere() -> str | None:
    cands = [shutil.which("vswhere")] + [d / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
                                         for d in _env_dirs()]
    return _first_existing(cands)


def _vswhere(*args: str) -> str:
    vw = find_vswhere()
    if not vw:
        return ""
    try:
        r = subprocess.run([vw, "-latest", "-products", "*", *args], capture_output=True, text=True,
                           timeout=30, encoding="utf-8", errors="replace")
        return r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
    except Exception:
        return ""


def find_visual_studio() -> dict[str, str | None]:
    install = _vswhere("-property", "installationPath")
    devenv = _first_existing([Path(install) / "Common7" / "IDE" / "devenv.exe"]) if install else None
    msb = _vswhere("-requires", "Microsoft.Component.MSBuild", "-find", r"MSBuild\**\Bin\MSBuild.exe")
    msbuild = msb if msb and Path(msb).is_file() else shutil.which("msbuild")
    return {"install": install or None, "devenv": devenv, "msbuild": msbuild}


def find_app(name: str) -> str | None:
    n = name.strip().strip('"')
    if not n:
        return None
    if Path(n).is_file():
        return str(Path(n))
    low = n.lower().removesuffix(".exe")
    if low in ("7z", "7zip", "7-zip", "7za"):
        return find_7zip()
    if low in ("winrar", "rar", "unrar"):
        w = find_winrar()
        return w["winrar" if low == "winrar" else low]
    if low in ("visualstudio", "visual studio", "vs", "devenv"):
        return find_visual_studio()["devenv"]
    if low == "msbuild":
        return find_visual_studio()["msbuild"]
    exe = n if n.lower().endswith(".exe") else n + ".exe"
    found = shutil.which(n) or shutil.which(exe) or _app_paths_registry(exe)
    if found:
        return found
    for d in _env_dirs():
        for sub in (d / n / exe, d / n.title() / exe):
            if sub.is_file():
                return str(sub)
    return None


def detect_all() -> dict[str, str]:
    vs = find_visual_studio()
    wr = find_winrar()
    rows = {
        "7-Zip": find_7zip(), "WinRAR": wr["winrar"], "Rar.exe (crear .rar)": wr["rar"],
        "Visual Studio (devenv)": vs["devenv"], "MSBuild": vs["msbuild"],
        "dotnet": shutil.which("dotnet"), "git": shutil.which("git"), "VS Code": shutil.which("code"),
    }
    return {k: v or "no encontrado" for k, v in rows.items()}


_SEVENZ_TYPES = {".7z": "7z", ".zip": "zip", ".tar": "tar", ".gz": "gzip", ".tgz": "tar", ".xz": "xz",
                 ".bz2": "bzip2", ".wim": "wim"}


def archive_command(op: str, archive: str, *, files: list[str] | None = None, dest: str | None = None,
                    password: str | None = None) -> list[str]:
    ext = Path(archive).suffix.lower()
    sz, wr = find_7zip(), find_winrar()
    if op == "list":
        if sz:
            return [sz, "l", "-slt", *([f"-p{password}"] if password else []), "--", archive]
        if wr["unrar"]:
            return [wr["unrar"], "l", archive]
        raise AppError("No encuentro 7-Zip ni UnRAR. Instala 7-Zip (7-zip.org).")
    if op == "extract":
        if not dest:
            raise AppError("Falta 'dest' (carpeta de destino).")
        if sz:
            return [sz, "x", "-y", f"-o{dest}", *([f"-p{password}"] if password else []), "--", archive]
        if wr["unrar"] and ext == ".rar":
            return [wr["unrar"], "x", "-y", *([f"-p{password}"] if password else []), archive,
                    dest.rstrip("\\/") + os.sep]
        raise AppError("No encuentro 7-Zip (ni UnRAR para .rar). Instala 7-Zip (7-zip.org).")
    if op == "create":
        if not files:
            raise AppError("Faltan 'files' (archivos o carpetas a comprimir).")
        if ext == ".rar":
            rar = wr["rar"]
            if not rar:
                raise AppError("Crear .rar requiere WinRAR (Rar.exe). 7-Zip no puede crear RAR; "
                               "usa .7z o .zip, o instala WinRAR.")
            return [rar, "a", "-r", "-ep1", "-y", *([f"-p{password}"] if password else []), archive, *files]
        if not sz:
            raise AppError("No encuentro 7-Zip. Instálalo (7-zip.org) o usa WinRAR para .rar.")
        t = _SEVENZ_TYPES.get(ext)
        if not t:
            raise AppError(f"Extensión '{ext}' no soportada para crear. Usa: .7z .zip .tar .gz .xz .bz2 .rar")
        return [sz, "a", f"-t{t}", "-y", *([f"-p{password}"] if password else []), archive, *files]
    raise AppError("op debe ser list, extract o create")


def build_command(project: str, configuration: str = "Debug", tool: str = "auto",
                  platform: str | None = None) -> list[str]:
    ext = Path(project).suffix.lower()
    vs = None
    if tool in ("auto", "msbuild", "devenv"):
        vs = find_visual_studio()
    dn = shutil.which("dotnet")
    if tool == "auto":
        tool = "dotnet" if dn and ext in (".csproj", ".fsproj", ".vbproj", ".sln") and not _is_legacy(project) \
            else "msbuild" if vs and vs["msbuild"] else "devenv" if vs and vs["devenv"] else "dotnet"
    if tool == "dotnet":
        if not dn:
            raise AppError("No encuentro 'dotnet'. Instala el SDK de .NET o usa tool='msbuild'.")
        return [dn, "build", project, "-c", configuration, "-nologo", "-v", "minimal"]
    if tool == "msbuild":
        if not vs or not vs["msbuild"]:
            raise AppError("No encuentro MSBuild (instala Visual Studio o las Build Tools).")
        cmd = [vs["msbuild"], project, "/restore", f"/p:Configuration={configuration}", "/v:minimal", "/nologo"]
        if platform:
            cmd.append(f"/p:Platform={platform}")
        return cmd
    if tool == "devenv":
        if not vs or not vs["devenv"]:
            raise AppError("No encuentro devenv.exe (Visual Studio).")
        return [vs["devenv"], project, "/Build", configuration]
    raise AppError("tool debe ser auto, dotnet, msbuild o devenv")


def _is_legacy(project: str) -> bool:
    try:
        p = Path(project)
        if p.suffix.lower() == ".sln":
            return ".vcxproj" in p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        pass
    return False
