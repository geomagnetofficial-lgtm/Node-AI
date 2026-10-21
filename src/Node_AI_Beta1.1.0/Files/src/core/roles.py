from __future__ import annotations

from src.core.library import LANG, Library, get_library

_DEFAULTS = Library()

ROLES: dict[str, tuple[str, str]] = {r.id: (r.nombre, r.prompt_base) for r in _DEFAULTS.roles("mesa")}
DIRECTOR: str = _DEFAULTS.get_role("director").prompt_base


def role_prompt(role: str) -> str:
    return get_library().role_prompt(role)


def director_prompt(role: str = "director") -> str:
    return get_library().role_prompt(role, director=True)


__all__ = ["DIRECTOR", "LANG", "ROLES", "director_prompt", "role_prompt"]
