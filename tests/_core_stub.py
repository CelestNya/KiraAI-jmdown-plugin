"""pytest 引导层：给插件模块提供最小 ``core`` 契约桩。

为什么需要它：``main.py`` / ``napcat_stream.py`` 在模块层硬依赖
``core.plugin``，而单元测试只想验证插件自身逻辑，不该为此拉起整个
KiraAI 运行时。

为什么无条件注入桩（而不是「有真 core 就用真 core」）：若同一份用例
随环境在真 core 与桩之间漂移，本地与 CI 的语义就不一致，改 core 时会
出现「本地绿、CI 红」的假信号。插件与真实框架的契约由
``scripts/local_smoke_test.py`` 在真实 KiraAI 上验证，两者职责不重叠。

维护提示：插件开始使用 core 的新符号时，同步在这里补桩。宁可 import
阶段直接失败，也不要带着半个契约跑过。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent  # 插件根
TESTS_DIR = Path(__file__).resolve().parent


def ensure_sys_path() -> None:
    """把插件根与测试目录放进 sys.path，使结果与 CWD 无关。"""
    for entry in (str(ROOT), str(TESTS_DIR)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def ensure_plugin_package() -> None:
    """注册合成 ``jmdown`` 包。

    插件以源码目录形式存放，自身目录名即包名；若不把父目录
    (``data/plugins``) 放进 sys.path，``import jmdown`` 无法解析。合成包
    避免测试为了导入而侵入 KiraAI 的目录结构。
    """
    if "jmdown" in sys.modules:
        return
    pkg: Any = types.ModuleType("jmdown")
    pkg.__path__ = [str(ROOT)]
    sys.modules["jmdown"] = pkg


def install_core_mock() -> None:
    """注入 ``core.*`` 桩；已存在则跳过，保证多次调用幂等。"""
    if "core.plugin" in sys.modules:
        return

    plugin_pkg: Any = types.ModuleType("core.plugin")

    class BasePlugin:
        def __init__(self, ctx: Any, cfg: Any) -> None:
            self.ctx = ctx
            self.plugin_cfg = cfg

        async def initialize(self) -> None:
            return None

        async def terminate(self) -> None:
            return None

    def register_tool(name: str, description: str, params: dict) -> Any:
        # 装饰器只透传原函数：单元测试不关心工具注册表
        def deco(func: Any) -> Any:
            return func

        return deco

    plugin_pkg.BasePlugin = BasePlugin
    plugin_pkg.PluginContext = object
    plugin_pkg.register_tool = register_tool
    plugin_pkg.logger = logging.getLogger("jmdown.test")

    chat: Any = types.ModuleType("core.chat")
    me: Any = types.ModuleType("core.chat.message_elements")

    class Text:
        def __init__(self, text: str) -> None:
            self.text = text

    me.Text = Text

    mu: Any = types.ModuleType("core.chat.message_utils")

    class MessageChain:
        def __init__(self, message_list: list | None = None) -> None:
            self.message_list = message_list or []

    mu.MessageChain = MessageChain

    chat.message_elements = me
    chat.message_utils = mu

    core: Any = types.ModuleType("core")
    core.plugin = plugin_pkg
    core.chat = chat

    sys.modules.update(
        {
            "core": core,
            "core.plugin": plugin_pkg,
            "core.chat": chat,
            "core.chat.message_elements": me,
            "core.chat.message_utils": mu,
        }
    )


def bootstrap() -> None:
    """幂等引导：路径 → 包 → core 桩。测试模块与 conftest 都可直接调用。"""
    ensure_sys_path()
    install_core_mock()
    ensure_plugin_package()
