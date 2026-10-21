import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_global_state():
    from src.core import i18n
    from src.core.library import set_library
    i18n.set_language("es")
    set_library(None)
    yield
    i18n.set_language("es")
    set_library(None)
