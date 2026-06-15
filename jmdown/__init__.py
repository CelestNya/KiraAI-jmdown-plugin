"""JMComic 下载器 — KiraAI 插件."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import JMdownPlugin

__all__ = ["JMdownPlugin"]


def __getattr__(name):
    if name == "JMdownPlugin":
        from .main import JMdownPlugin as cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
