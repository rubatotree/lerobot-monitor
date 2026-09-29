"""Standalone cloud model service; importing this package never starts Monitor."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def code_hash() -> str:
    """Fingerprint the installed package; servers and managers compare builds with it.

    The root stays the whole ``lerobot_monitor`` package so hashes compare equal
    across the service, the manager and every earlier deployment.
    """
    digest = hashlib.sha256()
    package = Path(__file__).parent.parent
    paths = set(package.rglob("*.py")) | {path for path in (package / "cloud" / "web").rglob("*") if path.is_file()}
    for path in sorted(paths):
        digest.update(str(path.relative_to(package)).replace("\\", "/").encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Construct the independent FastAPI service lazily."""
    from .app import create_app as factory

    return factory(*args, **kwargs)
