from __future__ import annotations


def walk(ctrl):
    yield ctrl
    for attr in ("controls", "content", "leading", "title", "subtitle", "actions"):
        child = getattr(ctrl, attr, None)
        if child is None:
            continue
        for c in (child if isinstance(child, (list, tuple)) else [child]):
            if hasattr(c, "__dict__") or hasattr(c, "_values"):
                yield from walk(c)


def walk_all(controls):
    for c in controls:
        yield from walk(c)


def find(controls, kind: str, **match):
    for c in walk_all(controls):
        if type(c).__name__ == kind and all(getattr(c, k, None) == v for k, v in match.items()):
            return c
    return None


def find_all(controls, kind: str):
    return [c for c in walk_all(controls) if type(c).__name__ == kind]


def texts(controls) -> list[str]:
    return [c.value for c in walk_all(controls) if type(c).__name__ in ("Text", "Markdown") and isinstance(getattr(c, "value", None), str)]
