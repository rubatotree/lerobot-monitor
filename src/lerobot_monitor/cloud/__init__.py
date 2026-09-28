"""Standalone cloud model service; importing this package never starts Monitor."""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Construct the independent FastAPI service lazily."""
    from .app import create_app as factory

    return factory(*args, **kwargs)
