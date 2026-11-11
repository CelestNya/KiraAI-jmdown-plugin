"""JMComic 下载器 — KiraAI 插件。

KiraAI 的入口优先级是 ``main.py`` → ``plugin.py`` → ``__init__.py``，本插件
始终命中 ``main.py``；本文件只为「把插件目录当包导入」的场景保留导出。

为什么惰性导出而不是模块层 ``from .main import``：``main.py`` 在导入期就依赖
``core`` 与 ``jmcomic``。一旦本文件被当作顶层模块执行（例如测试收集器以
``__init__`` 名义加载它），相对导入会直接抛 ImportError，把「测试收集失败」
伪装成插件缺陷。惰性化后 ``import`` 无副作用，只在取属性时付出代价。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .main import JMdownPlugin

__all__ = ["JMdownPlugin"]


def __getattr__(name: str) -> Any:
    if name == "JMdownPlugin":
        from .main import JMdownPlugin

        return JMdownPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
