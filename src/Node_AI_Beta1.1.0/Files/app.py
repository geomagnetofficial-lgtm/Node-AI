"""NodeAi — Geo Magnet Studio © 2026 Geo Magnet. Todos los derechos reservados."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.core.settings import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import flet as ft  # noqa: E402

from src.ui import main  # noqa: E402

if __name__ == "__main__":
    ft.run(main)
