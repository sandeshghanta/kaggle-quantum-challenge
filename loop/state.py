"""Small durable loop state (cursors), written atomically."""

from __future__ import annotations

import os
from pathlib import Path


def load_cursor(path: Path) -> int:
    try:
        return int(Path(path).read_text().strip() or 0)
    except FileNotFoundError:
        return 0


def save_cursor(path: Path, cursor: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{cursor}\n")
    os.replace(tmp, path)
