from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "NodeAi"
MAX_MODELS = 20
MAX_ROUNDS = 50
ROUNDS_WARN = 5
USER_DIRNAME = "usuario"
SUPPORTED_LANGS = ("es", "en", "pt", "fr", "de")
PLACEHOLDERS = {"", "tu_clave_aqui", "your_key_here", "changeme"}


def app_dir() -> Path:
    base = os.environ.get("NODEAI_HOME") or os.environ.get("FLET_APP_STORAGE_DATA")
    if base:
        p = Path(base)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / APP_NAME.lower()
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_dir() -> Path:
    p = app_dir() / USER_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def parse_int(text: Any, lo: int = 1, hi: int | None = None) -> int | None:
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        value = text
    else:
        t = str(text).strip()
        if not t or len(t) > 12 or not (t.isascii() and t.isdigit()):
            return None
        value = int(t)
    if value < lo:
        return None
    if hi is not None and value > hi:
        return hi
    return value


def clamp_rounds(rounds: Any) -> int:
    try:
        n = int(rounds)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_ROUNDS, n))


def load_dotenv(*paths: Path) -> None:
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if val.lower() in PLACEHOLDERS:
                continue
            os.environ.setdefault(key, val)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class ModelSlot:
    id: str = field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    mode: str = "cloud"
    provider: str = "openai"
    model: str = ""
    gguf_path: str = ""
    api_key: str = field(default="", repr=False)
    save_key: bool = False
    base_url: str = ""
    role: str = "debater"
    system_prompt: str = ""
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 1024
    vision: bool = True

    def label(self) -> str:
        return self.name or self.model or Path(self.gguf_path).stem or self.provider

    def is_ready(self) -> bool:
        from src.llm.providers import PROVIDERS
        p = PROVIDERS.get(self.provider)
        if p is None:
            return False
        if p.protocol == "gguf":
            return bool(self.gguf_path)
        if not self.model:
            return False
        if p.key == "custom" and not self.base_url:
            return False
        if p.needs_key:
            return bool(self.api_key or os.environ.get(p.env_key, ""))
        return True


@dataclass
class HardwareSettings:
    device: str = "auto"
    n_gpu_layers: int = -1
    n_threads: int = 0
    n_ctx: int = 4096
    max_loaded_models: int = 1


@dataclass
class AgentSettings:
    enabled: bool = False
    workspace: str = ""
    approval: str = "balanced"
    max_steps: int = 30
    unlimited_steps: bool = False
    loop_repeat: int = 5
    stall_limit: int = 8
    shot_width: int = 1280
    grid: bool = False
    settle_s: float = 0.7
    settle_max_s: float = 3.0
    verify_actions: bool = True
    monitor: int = 1
    shell_timeout_s: int = 120
    temperature: float = 0.2


@dataclass
class AppSettings:
    configured: bool = False
    models_dir: str = ""
    global_system_prompt: str = ""
    rounds: int = 1
    language: str = "es"
    keys_notice_shown: bool = False
    director_role: str = "director"
    timeout_s: int = 120
    max_parallel: int = 5
    history_messages: int = 6
    director: ModelSlot = field(default_factory=lambda: ModelSlot(name="Director", role="moderator", max_tokens=2048))
    slots: list[ModelSlot] = field(default_factory=list)
    hardware: HardwareSettings = field(default_factory=HardwareSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)

    def active_slots(self) -> list[ModelSlot]:
        return [s for s in self.slots if s.enabled][:MAX_MODELS]


def _build(cls, data: Any):
    if not isinstance(data, dict):
        return cls()
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def settings_to_dict(settings: AppSettings) -> dict[str, Any]:
    d = asdict(settings)
    for slot in [d["director"], *d["slots"]]:
        if not slot.get("save_key"):
            slot.pop("api_key", None)
    return d


def detect_language() -> str:
    import locale
    try:
        code = (locale.getlocale()[0] or os.environ.get("LANG", "") or "es")[:2].lower()
    except ValueError:
        code = "es"
    return code if code in SUPPORTED_LANGS else "es"


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self._default_location = path is None
        self.path = path or user_dir() / "config.json"
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self.legacy_files: list[Path] = []
        self.migrated_from: Path | None = None
        self.legacy_key_slots: list[str] = []
        if self._default_location:
            self._migrate_legacy()
        self.settings = self.load()

    def _migrate_legacy(self) -> None:
        old = app_dir() / "config.json"
        if not old.is_file():
            return
        bak = old.with_name("config.json.bak")
        if not self.path.exists():
            try:
                data = old.read_bytes()
                if not bak.exists():
                    self._write_bytes(bak, data)
                self._write_bytes(self.path, data)
                self.migrated_from = old
            except OSError:
                self.migrated_from = None
        self.legacy_files = [f for f in (old, bak) if f.exists()]

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def load(self) -> AppSettings:
        existed = self.path.is_file()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        s = _build(AppSettings, {k: v for k, v in raw.items() if k not in ("director", "slots", "hardware", "agent")})
        raw_dir = raw.get("director") if isinstance(raw.get("director"), dict) else None
        s.director = _build(ModelSlot, raw_dir) if raw_dir else AppSettings().director
        raw_slots = [d for d in raw.get("slots", []) if isinstance(d, dict)][:MAX_MODELS]
        s.slots = [_build(ModelSlot, d) for d in raw_slots]
        s.hardware = _build(HardwareSettings, raw.get("hardware"))
        s.agent = _build(AgentSettings, raw.get("agent"))
        if not s.agent.workspace:
            s.agent.workspace = str(app_dir() / "workspace")
        if not s.models_dir:
            s.models_dir = str(app_dir() / "models")
        if "language" not in raw:
            s.language = "es" if existed else detect_language()
        if s.language not in SUPPORTED_LANGS:
            s.language = "es"
        self.legacy_key_slots = []
        pairs = [(s.director, raw_dir or {})] + list(zip(s.slots, raw_slots))
        for slot, d in pairs:
            if slot.api_key and "save_key" not in d:
                slot.save_key = True
                self.legacy_key_slots.append(slot.id)
        return s

    @property
    def needs_key_notice(self) -> bool:
        return bool(self.legacy_key_slots) and not self.settings.keys_notice_shown

    def keep_legacy_keys(self) -> None:
        self.settings.keys_notice_shown = True
        self.save()

    def drop_legacy_keys(self) -> list[Path]:
        for slot in [self.settings.director, *self.settings.slots]:
            if slot.id in self.legacy_key_slots:
                slot.save_key = False
        self.settings.keys_notice_shown = True
        self.legacy_key_slots = []
        self.save()
        removed: list[Path] = []
        for f in self.legacy_files:
            try:
                f.unlink()
                removed.append(f)
            except OSError:
                pass
        self.legacy_files = []
        return removed

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings_to_dict(self.settings), indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)

    def save_soon(self, delay: float = 0.8) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(delay, self.save)
        self._timer.daemon = True
        self._timer.start()

    def add_slot(self) -> ModelSlot | None:
        if len(self.settings.slots) >= MAX_MODELS:
            return None
        slot = ModelSlot(name=f"IA {len(self.settings.slots) + 1}")
        self.settings.slots.append(slot)
        return slot
