from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

LANG = "Responde siempre en el idioma del usuario. "

USER_PREFIX = "usr-"
USER_FILE = "prompts_usuario.json"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_000_000
MAX_ITEMS = 1000
MAX_TEXT = 20_000
MAX_NAME = 120
MAX_DESC = 500
MAX_TAGS = 20
MAX_TAG = 40
AMBITOS = ("mesa", "director")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_FALLBACK = {"version": 1, "roles": [
    {"id": "neutral", "nombre": "Neutral", "prompt_base": "Eres un asistente útil, claro y conciso."},
    {"id": "director", "nombre": "Director", "ambito": "director",
     "prompt_base": "Eres el Director de NodeAi. Contrasta las respuestas de la mesa y entrega UNA respuesta final cohesiva."},
], "prompts": []}


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base.joinpath(*parts)


DEFAULTS_PATH = resource_path("src", "data", "prompts_default.json")


@dataclass
class Role:
    id: str
    nombre: str
    descripcion: str = ""
    prompt_base: str = ""
    ambito: str = "mesa"
    origen: str = "default"
    desde: int = 1

    def to_user_dict(self) -> dict[str, Any]:
        return {"id": self.id, "nombre": self.nombre, "descripcion": self.descripcion,
                "prompt_base": self.prompt_base, "ambito": self.ambito}


@dataclass
class Prompt:
    id: str
    titulo: str
    rol: str = ""
    categoria: str = ""
    texto: str = ""
    etiquetas: list[str] = field(default_factory=list)
    origen: str = "default"
    desde: int = 1

    def to_user_dict(self) -> dict[str, Any]:
        return {"id": self.id, "titulo": self.titulo, "rol": self.rol, "categoria": self.categoria,
                "texto": self.texto, "etiquetas": list(self.etiquetas)}


@dataclass
class ImportReport:
    roles: int = 0
    prompts: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)


def _text(d: dict, key: str, limit: int, *, required: bool = False, default: str = "") -> str:
    v = d.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        raise ValueError(f"'{key}' debe ser texto")
    v = v.strip()
    if required and not v:
        raise ValueError(f"'{key}' es obligatorio")
    if len(v) > limit:
        raise ValueError(f"'{key}' supera {limit} caracteres")
    return v


def _check_id(value: Any, *, user: bool | None) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValueError("'id' inválido (minúsculas, números, guiones; máx. 64)")
    if user is None:
        return value
    if user and not value.startswith(USER_PREFIX):
        raise ValueError(f"los ids del usuario deben empezar por '{USER_PREFIX}'")
    if not user and value.startswith(USER_PREFIX):
        raise ValueError(f"los ids por defecto no pueden empezar por '{USER_PREFIX}'")
    return value


def parse_role(d: Any, *, user: bool | None) -> Role:
    if not isinstance(d, dict):
        raise ValueError("la entrada no es un objeto")
    ambito = d.get("ambito", "mesa") or "mesa"
    if ambito not in AMBITOS:
        raise ValueError(f"'ambito' debe ser uno de {AMBITOS}")
    desde = d.get("desde", 1)
    return Role(_check_id(d.get("id"), user=user), _text(d, "nombre", MAX_NAME, required=True),
                _text(d, "descripcion", MAX_DESC), _text(d, "prompt_base", MAX_TEXT, required=True), ambito,
                "default" if user is False else "usuario", desde if isinstance(desde, int) and not isinstance(desde, bool) and desde >= 1 else 1)


def parse_prompt(d: Any, *, user: bool | None) -> Prompt:
    if not isinstance(d, dict):
        raise ValueError("la entrada no es un objeto")
    tags_raw = d.get("etiquetas", [])
    if tags_raw is None:
        tags_raw = []
    if not isinstance(tags_raw, list):
        raise ValueError("'etiquetas' debe ser una lista")
    tags: list[str] = []
    for t in tags_raw[:MAX_TAGS]:
        if isinstance(t, str) and t.strip() and len(t.strip()) <= MAX_TAG and t.strip() not in tags:
            tags.append(t.strip())
    desde = d.get("desde", 1)
    return Prompt(_check_id(d.get("id"), user=user), _text(d, "titulo", MAX_NAME, required=True),
                  _text(d, "rol", 64), _text(d, "categoria", 60), _text(d, "texto", MAX_TEXT, required=True), tags,
                  "default" if user is False else "usuario",
                  desde if isinstance(desde, int) and not isinstance(desde, bool) and desde >= 1 else 1)


def read_json_limited(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as e:
        raise ValueError(f"no se puede leer el archivo ({e.strerror or type(e).__name__})") from None
    if size > MAX_FILE_BYTES:
        raise ValueError(f"el archivo es demasiado grande ({size // 1024} KB; máximo {MAX_FILE_BYTES // 1024} KB)")
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        raise ValueError("no es un JSON válido") from None


def parse_document(data: Any, *, user: bool | None, warnings: list[str], label: str) -> tuple[int, list[Role], list[Prompt]]:
    if not isinstance(data, dict):
        raise ValueError("el documento debe ser un objeto JSON")
    version = data.get("version", SCHEMA_VERSION if user is not False else None)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("falta 'version' (entero >= 1)")
    out: list[list] = []
    for key, parser, kind in (("roles", parse_role, "rol"), ("prompts", parse_prompt, "prompt")):
        items = data.get(key, [])
        if not isinstance(items, list):
            warnings.append(f"{label}: '{key}' debe ser una lista; se ignora.")
            items = []
        if len(items) > MAX_ITEMS:
            warnings.append(f"{label}: demasiados {key} ({len(items)}); solo se leen los primeros {MAX_ITEMS}.")
        parsed: list = []
        seen: set[str] = set()
        for i, raw in enumerate(items[:MAX_ITEMS]):
            try:
                obj = parser(raw, user=user)
            except ValueError as e:
                warnings.append(f"{label}: {kind} #{i + 1} ignorado ({e}).")
                continue
            if obj.id in seen:
                warnings.append(f"{label}: {kind} '{obj.id}' repetido; se ignora el duplicado.")
                continue
            seen.add(obj.id)
            parsed.append(obj)
        out.append(parsed)
    return version, out[0], out[1]


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


class Library:
    def __init__(self, user_path: Path | None = None, defaults_path: Path | None = None):
        self.user_path = user_path
        self.defaults_path = defaults_path or DEFAULTS_PATH
        self.warnings: list[str] = []
        self.defaults_version = 0
        self.seen_version = 0
        self.updated_from: int | None = None
        self.default_roles: dict[str, Role] = {}
        self.default_prompts: dict[str, Prompt] = {}
        self.user_roles: dict[str, Role] = {}
        self.user_prompts: dict[str, Prompt] = {}
        self.reload()

    def reload(self) -> None:
        self.warnings = []
        self._load_defaults()
        self._load_user()

    def _load_defaults(self) -> None:
        try:
            data = read_json_limited(self.defaults_path)
            version, roles, prompts = parse_document(data, user=False, warnings=self.warnings, label="prompts_default.json")
        except ValueError as e:
            self.warnings.append(f"prompts_default.json no se pudo cargar ({e}); uso valores mínimos.")
            version, roles, prompts = parse_document(_FALLBACK, user=False, warnings=[], label="fallback")
        self.defaults_version = version
        self.default_roles = {r.id: r for r in roles}
        self.default_prompts = {p.id: p for p in prompts}

    def _load_user(self) -> None:
        self.user_roles, self.user_prompts = {}, {}
        self.seen_version = self.defaults_version
        self.updated_from = None
        path = self.user_path
        if path is None or not path.is_file():
            return
        label = path.name
        try:
            data = read_json_limited(path)
            _, roles, prompts = parse_document(data, user=True, warnings=self.warnings, label=label)
        except ValueError as e:
            backup = path.with_name(path.name + ".corrupt")
            try:
                if not backup.exists():
                    shutil.copy2(path, backup)
            except OSError:
                pass
            self.warnings.append(f"{label} no se pudo leer ({e}); se ignora y se conserva una copia en {backup.name}.")
            return
        for r in roles:
            if r.id in self.default_roles:
                self.warnings.append(f"{label}: rol '{r.id}' choca con un id por defecto; se ignora.")
            else:
                self.user_roles[r.id] = r
        for p in prompts:
            if p.id in self.default_prompts:
                self.warnings.append(f"{label}: prompt '{p.id}' choca con un id por defecto; se ignora.")
            else:
                self.user_prompts[p.id] = p
        seen = data.get("defaults_vista")
        if isinstance(seen, int) and not isinstance(seen, bool) and seen >= 1:
            self.seen_version = seen
            if seen < self.defaults_version:
                self.updated_from = seen
        for p in self.user_prompts.values():
            if p.rol and p.rol not in self.roles_by_id():
                self.warnings.append(f"{label}: el prompt '{p.titulo}' usa el rol '{p.rol}', que ya no existe.")

    def new_defaults(self) -> list[Role | Prompt]:
        if self.updated_from is None:
            return []
        items: list[Role | Prompt] = [*self.default_roles.values(), *self.default_prompts.values()]
        return [i for i in items if i.desde > self.updated_from]

    def acknowledge_defaults(self) -> None:
        self.seen_version = self.defaults_version
        self.updated_from = None
        self.save()

    def to_document(self) -> dict[str, Any]:
        return {"version": SCHEMA_VERSION, "defaults_vista": self.seen_version,
                "roles": [r.to_user_dict() for r in self.user_roles.values()],
                "prompts": [p.to_user_dict() for p in self.user_prompts.values()]}

    def save(self) -> None:
        if self.user_path is None:
            return
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.user_path, json.dumps(self.to_document(), indent=2, ensure_ascii=False))

    def export_user(self, path: Path) -> None:
        doc = self.to_document()
        doc.pop("defaults_vista", None)
        _atomic_write(Path(path), json.dumps(doc, indent=2, ensure_ascii=False))

    def import_user(self, path: Path) -> ImportReport:
        rep = ImportReport()
        try:
            data = read_json_limited(Path(path))
            _, roles, prompts = parse_document(data, user=None, warnings=rep.warnings, label=Path(path).name)
        except ValueError as e:
            rep.warnings.append(f"No se importó nada: {e}.")
            return rep
        remap: dict[str, str] = {}
        for r in roles:
            if r.id in self.user_roles:
                rep.skipped += 1
                continue
            nid = r.id if r.id.startswith(USER_PREFIX) and r.id not in self.roles_by_id() else self._new_id()
            remap[r.id] = nid
            self.user_roles[nid] = replace(r, id=nid, origen="usuario")
            rep.roles += 1
        for p in prompts:
            if p.id in self.user_prompts:
                rep.skipped += 1
                continue
            nid = p.id if p.id.startswith(USER_PREFIX) and p.id not in self.prompts_by_id() else self._new_id()
            self.user_prompts[nid] = replace(p, id=nid, rol=remap.get(p.rol, p.rol), origen="usuario")
            rep.prompts += 1
        if rep.roles or rep.prompts:
            self.save()
        return rep

    def roles_by_id(self) -> dict[str, Role]:
        return {**self.default_roles, **self.user_roles}

    def prompts_by_id(self) -> dict[str, Prompt]:
        return {**self.default_prompts, **self.user_prompts}

    def roles(self, ambito: str | None = None) -> list[Role]:
        return [r for r in self.roles_by_id().values() if ambito is None or r.ambito == ambito]

    def get_role(self, role_id: str) -> Role | None:
        return self.roles_by_id().get(role_id)

    def get_prompt(self, prompt_id: str) -> Prompt | None:
        return self.prompts_by_id().get(prompt_id)

    def role_prompt(self, role_id: str, *, director: bool = False) -> str:
        wanted = "director" if director else "mesa"
        role = self.get_role(role_id)
        if role is None or role.ambito != wanted:
            role = self.get_role("director" if director else "neutral") or next(iter(self.roles_by_id().values()))
        return LANG + role.prompt_base

    def categorias(self) -> list[str]:
        return sorted({p.categoria for p in self.prompts_by_id().values() if p.categoria}, key=str.casefold)

    def etiquetas(self) -> list[str]:
        return sorted({t for p in self.prompts_by_id().values() for t in p.etiquetas}, key=str.casefold)

    def search_prompts(self, query: str = "", rol: str = "", categoria: str = "", etiqueta: str = "",
                       origen: str = "") -> list[Prompt]:
        terms = query.lower().split()
        out = []
        for p in self.prompts_by_id().values():
            if rol and p.rol != rol or categoria and p.categoria != categoria or etiqueta and etiqueta not in p.etiquetas:
                continue
            if origen and p.origen != origen:
                continue
            hay = " ".join([p.titulo, p.texto, p.categoria, p.rol, *p.etiquetas]).lower()
            if all(t in hay for t in terms):
                out.append(p)
        return sorted(out, key=lambda p: (p.categoria.casefold(), p.titulo.casefold()))

    def _new_id(self) -> str:
        while True:
            nid = USER_PREFIX + uuid.uuid4().hex[:8]
            if nid not in self.roles_by_id() and nid not in self.prompts_by_id():
                return nid

    def _user_role(self, role_id: str) -> Role:
        if role_id in self.default_roles:
            raise PermissionError("Los roles por defecto no se editan: duplícalo y edita tu copia.")
        if role_id not in self.user_roles:
            raise KeyError(role_id)
        return self.user_roles[role_id]

    def _user_prompt(self, prompt_id: str) -> Prompt:
        if prompt_id in self.default_prompts:
            raise PermissionError("Los prompts por defecto no se editan: duplícalo y edita tu copia.")
        if prompt_id not in self.user_prompts:
            raise KeyError(prompt_id)
        return self.user_prompts[prompt_id]

    def add_role(self, nombre: str, prompt_base: str, descripcion: str = "", ambito: str = "mesa") -> Role:
        role = parse_role({"id": self._new_id(), "nombre": nombre, "descripcion": descripcion,
                           "prompt_base": prompt_base, "ambito": ambito}, user=True)
        self.user_roles[role.id] = role
        self.save()
        return role

    def update_role(self, role_id: str, **changes: Any) -> Role:
        cur = self._user_role(role_id)
        new = parse_role({**cur.to_user_dict(), **{k: v for k, v in changes.items() if k != "id"}}, user=True)
        self.user_roles[role_id] = new
        self.save()
        return new

    def delete_role(self, role_id: str) -> None:
        self._user_role(role_id)
        del self.user_roles[role_id]
        self.save()

    def duplicate_role(self, role_id: str) -> Role:
        src = self.get_role(role_id)
        if src is None:
            raise KeyError(role_id)
        return self.add_role(f"{src.nombre} (copia)"[:MAX_NAME], src.prompt_base, src.descripcion, src.ambito)

    def edit_role(self, role_id: str) -> Role:
        return self.user_roles[role_id] if role_id in self.user_roles else self.duplicate_role(role_id)

    def add_prompt(self, titulo: str, texto: str, rol: str = "", categoria: str = "", etiquetas: list[str] | None = None) -> Prompt:
        p = parse_prompt({"id": self._new_id(), "titulo": titulo, "texto": texto, "rol": rol, "categoria": categoria,
                          "etiquetas": etiquetas or []}, user=True)
        self.user_prompts[p.id] = p
        self.save()
        return p

    def update_prompt(self, prompt_id: str, **changes: Any) -> Prompt:
        cur = self._user_prompt(prompt_id)
        new = parse_prompt({**cur.to_user_dict(), **{k: v for k, v in changes.items() if k != "id"}}, user=True)
        self.user_prompts[prompt_id] = new
        self.save()
        return new

    def delete_prompt(self, prompt_id: str) -> None:
        self._user_prompt(prompt_id)
        del self.user_prompts[prompt_id]
        self.save()

    def duplicate_prompt(self, prompt_id: str) -> Prompt:
        src = self.get_prompt(prompt_id)
        if src is None:
            raise KeyError(prompt_id)
        return self.add_prompt(f"{src.titulo} (copia)"[:MAX_NAME], src.texto, src.rol, src.categoria, list(src.etiquetas))

    def edit_prompt(self, prompt_id: str) -> Prompt:
        return self.user_prompts[prompt_id] if prompt_id in self.user_prompts else self.duplicate_prompt(prompt_id)

    def apply_prompt(self, slot: Any, prompt: Prompt, *, director: bool = False) -> None:
        slot.system_prompt = prompt.texto
        role = self.get_role(prompt.rol) if prompt.rol else None
        if not director and role is not None and role.ambito == "mesa":
            slot.role = role.id


_ACTIVE: Library | None = None


def get_library() -> Library:
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = Library()
    return _ACTIVE


def set_library(lib: Library | None) -> None:
    global _ACTIVE
    _ACTIVE = lib
