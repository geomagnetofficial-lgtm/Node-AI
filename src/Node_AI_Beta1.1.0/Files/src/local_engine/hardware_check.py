from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field

try:
    import psutil
except ImportError:
    psutil = None

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_VIRTUAL_GPU = ("microsoft basic", "remote", "virtual", "vmware", "virtualbox", "hyper-v", "parsec")


@dataclass
class Gpu:
    name: str
    vendor: str
    vram_gb: float | None = None
    vram_free_gb: float | None = None
    driver: str = ""
    backend: str = "other"


@dataclass
class Hardware:
    os: str = ""
    cpu_name: str = ""
    cores_physical: int = 1
    cores_logical: int = 1
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    gpus: list[Gpu] = field(default_factory=list)
    llama_cpp_installed: bool = False
    llama_cpp_gpu: bool = False

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpus)

    @property
    def total_vram_gb(self) -> float:
        return round(sum(g.vram_gb or 0.0 for g in self.gpus), 1)

    def summary_lines(self) -> list[str]:
        lines = [f"Sistema: {self.os} · CPU: {self.cpu_name} ({self.cores_physical} núcleos físicos / {self.cores_logical} hilos)",
                 f"RAM: {self.ram_total_gb} GB (libre: {self.ram_available_gb} GB)"]
        if not self.gpus:
            lines.append("GPU: ninguna detectada → se usará la CPU")
        for g in self.gpus:
            vram = f"{g.vram_gb} GB" if g.vram_gb else "VRAM no medible"
            free = f", libre {g.vram_free_gb} GB" if g.vram_free_gb is not None else ""
            lines.append(f"GPU: {g.name} · {vram}{free}" + (f" · driver {g.driver}" if g.driver else ""))
        if not self.llama_cpp_installed:
            lines.append("Motor local llama.cpp: no instalado (la nube, Ollama y LM Studio funcionan igual)")
        elif self.has_gpu and not self.llama_cpp_gpu:
            lines.append("Motor local llama.cpp: instalado SOLO para CPU (necesita build con CUDA/Metal para usar la GPU)")
        else:
            lines.append("Motor local llama.cpp: instalado" + (" con aceleración GPU" if self.llama_cpp_gpu else " (CPU)"))
        return lines


def _run(cmd: list[str], timeout: float = 6.0) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def _num(x: str) -> float | None:
    try:
        return float(x)
    except ValueError:
        return None


def _nvidia() -> list[Gpu]:
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        for base in (os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files")):
            for sub in (r"System32\nvidia-smi.exe", r"NVIDIA Corporation\NVSMI\nvidia-smi.exe"):
                cand = os.path.join(base, sub)
                if os.path.isfile(cand):
                    exe = cand
                    break
            if exe:
                break
    if not exe:
        return []
    out = _run([exe, "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits"])
    gpus: list[Gpu] = []
    for line in (out or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        total, used = _num(parts[1]), _num(parts[2])
        gpus.append(Gpu(
            name=parts[0], vendor="nvidia", backend="cuda", driver=parts[3],
            vram_gb=round(total / 1024, 1) if total else None,
            vram_free_gb=round((total - used) / 1024, 1) if total and used is not None else None,
        ))
    return gpus


def _vendor(name: str) -> str:
    n = name.lower()
    for key, vendor in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("amd", "amd"), ("radeon", "amd"),
                        ("intel", "intel"), ("apple", "apple")):
        if key in n:
            return vendor
    return "other"


def _other_gpus(skip_nvidia: bool) -> list[Gpu]:
    names: list[str] = []
    if os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"], timeout=10)
        names = [l.strip() for l in (out or "").splitlines() if l.strip()]
    elif platform.system() == "Linux" and shutil.which("lspci"):
        out = _run(["lspci"]) or ""
        for l in out.splitlines():
            if re.search(r"VGA compatible|3D controller|Display controller", l):
                names.append(l.split(": ", 1)[-1].strip())
    gpus = []
    for n in names:
        if any(v in n.lower() for v in _VIRTUAL_GPU):
            continue
        vendor = _vendor(n)
        if vendor == "nvidia" and skip_nvidia:
            continue
        gpus.append(Gpu(name=n, vendor=vendor, backend="cuda" if vendor == "nvidia" else "other"))
    return gpus


def _cpu_name() -> str:
    if platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "CPU desconocida"


def _llama_status() -> tuple[bool, bool]:
    try:
        import llama_cpp
    except Exception:
        return False, False
    try:
        return True, bool(llama_cpp.llama_supports_gpu_offload())
    except Exception:
        return True, False


_cache: Hardware | None = None


def detect(refresh: bool = False) -> Hardware:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    hw = Hardware(os=f"{platform.system()} {platform.release()}", cpu_name=_cpu_name())
    logical = os.cpu_count() or 1
    hw.cores_logical = logical
    hw.cores_physical = logical
    if psutil:
        hw.cores_physical = psutil.cpu_count(logical=False) or logical
        vm = psutil.virtual_memory()
        hw.ram_total_gb = round(vm.total / 1024**3, 1)
        hw.ram_available_gb = round(vm.available / 1024**3, 1)

    gpus = _nvidia()
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        gpus.append(Gpu(name="Apple Silicon (memoria unificada)", vendor="apple", backend="metal",
                        vram_gb=hw.ram_total_gb or None))
    else:
        gpus += _other_gpus(skip_nvidia=bool(gpus))
    hw.gpus = gpus
    hw.llama_cpp_installed, hw.llama_cpp_gpu = _llama_status()
    _cache = hw
    return hw


def resolve_gpu_layers(device: str, requested_layers: int, hw: Hardware) -> tuple[int, str]:
    if device == "cpu":
        return 0, "Modo CPU forzado."
    if not hw.llama_cpp_gpu:
        note = "" if device == "auto" else "Pediste GPU, pero este build de llama.cpp es solo CPU: se usa CPU."
        return 0, note
    if device == "auto" and not hw.has_gpu:
        return 0, "No hay GPU detectada: se usa CPU."
    return requested_layers, ""
