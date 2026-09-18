"""Make the sibling HuggingFace `lerobot` package importable when not installed."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_lerobot_on_path() -> Path | None:
    """Insert a `lerobot/src` directory onto sys.path.

    The monitor is often installed into site-packages, so we cannot rely on
    ``__file__`` walking up to the git checkout. Search env, cwd, and a few
    well-known sibling layouts instead.
    """
    here = Path(__file__).resolve()
    cwd = Path.cwd()
    env = os.environ.get("LEROBOT_SRC")
    candidates = [
        Path(env) if env else None,
        here.parents[2] / "lerobot" / "src" if len(here.parents) >= 3 else None,
        here.parents[2].parent / "lerobot" / "src" if len(here.parents) >= 3 else None,
        cwd / "lerobot" / "src",
        cwd.parent / "lerobot" / "src",
        cwd.parent.parent / "lerobot" / "src",
    ]
    for path in candidates:
        if path is None:
            continue
        if (path / "lerobot" / "__init__.py").is_file():
            resolved = str(path)
            if resolved not in sys.path:
                sys.path.insert(0, resolved)
            return path
    return None
