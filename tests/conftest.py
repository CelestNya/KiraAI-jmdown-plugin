"""pytest 全局夹具/引导。

职责边界：这里只做「让测试能在任意 CWD 下导入插件模块」，不改变运行
目录——测试自身全部使用临时目录，``os.chdir`` 反而会把相对路径写进
KiraAI 的 ``data/``。
"""

from __future__ import annotations

import sys
from pathlib import Path

# conftest 自身的 import 路径不依赖 pytest 的 rootdir 推断策略
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import _core_stub  # noqa: E402

# 在任何测试模块被收集前完成 sys.path / core 桩注入
_core_stub.bootstrap()
