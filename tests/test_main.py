"""JMdown main.py 核心逻辑测试。

通过 sys.modules 注入 core 框架的轻量 mock，使 main.py 可在无 KiraAI 运行时
环境下导入，从而隔离测试插件自身逻辑（输入校验、去重、并发上限、跨会话拦截）。

Run:
    uv run python -m pytest tests/test_main.py -v
    # or
    uv run python -m unittest tests.test_main -v
"""
# jmdown 包与 core.* 在运行时由本文件注入 sys.modules，pyright 静态无法解析
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import logging
import sys
import types
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

# ── 在 import main 之前注入 core 框架 mock（仅当真实 core 不可用时）──
ROOT = Path(__file__).resolve().parent.parent


def _install_core_mock() -> None:
    """注入 core.* 桩模块，避免依赖完整 KiraAI 运行时。"""
    # Any 注解：ModuleType 实例需动态挂属性，pyright 下按 Any 处理
    core: Any = types.ModuleType("core")
    plugin_pkg: Any = types.ModuleType("core.plugin")

    class BasePlugin:
        def __init__(self, ctx, cfg):
            self.ctx = ctx
            self.plugin_cfg = cfg

        async def initialize(self) -> None:
            pass

        async def terminate(self) -> None:
            pass

    def register_tool(name: str, description: str, params: dict):
        # 装饰器只透传原函数，测试不关心工具注册
        def deco(f):
            return f

        return deco

    plugin_pkg.BasePlugin = BasePlugin
    plugin_pkg.PluginContext = object
    plugin_pkg.register_tool = register_tool
    plugin_pkg.logger = logging.getLogger("jmdown.test")
    core.plugin = plugin_pkg

    chat: Any = types.ModuleType("core.chat")
    me: Any = types.ModuleType("core.chat.message_elements")

    class Text:
        def __init__(self, text: str):
            self.text = text

    me.Text = Text
    mu: Any = types.ModuleType("core.chat.message_utils")

    class MessageChain:
        def __init__(self, message_list=None):
            self.message_list = message_list or []

    mu.MessageChain = MessageChain
    chat.message_elements = me
    chat.message_utils = mu

    sys.modules.update(
        {
            "core": core,
            "core.plugin": plugin_pkg,
            "core.chat": chat,
            "core.chat.message_elements": me,
            "core.chat.message_utils": mu,
        }
    )


_install_core_mock()

# 把插件根注册为 `jmdown` 包，使 main.py 的 `from .cache import ...` 相对导入生效
if "jmdown" not in sys.modules:
    _pkg: Any = types.ModuleType("jmdown")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["jmdown"] = _pkg

from jmdown.main import (  # noqa: E402
    JMDownError,
    JMdownPlugin,
    TaskState,
    _fmt,
    _generate_password,
    _parse_target,
)


def make_plugin() -> JMdownPlugin:
    """构造 plugin 实例，手动设置 send_jm_album 依赖的字段（绕过 initialize）。"""
    p = JMdownPlugin(MagicMock(), {})
    p._max_concurrent = 2
    p._upload_timeout = 300
    p._allow_cross_session = False
    return p


class ParseTargetTest(unittest.TestCase):
    def test_dm(self):
        self.assertEqual(_parse_target("qq:dm:123456"), ("123456", False, None))

    def test_gm(self):
        self.assertEqual(_parse_target("qq:gm:789"), ("789", True, "789"))

    def test_missing_parts(self):
        with self.assertRaises(JMDownError):
            _parse_target("qq:dm")

    def test_unknown_type(self):
        with self.assertRaises(JMDownError):
            _parse_target("qq:xx:123")


class PasswordTest(unittest.TestCase):
    def test_custom_passthrough(self):
        self.assertEqual(_generate_password("mypass"), "mypass")

    def test_random_length_16(self):
        self.assertEqual(len(_generate_password("")), 16)

    def test_random_unique(self):
        self.assertNotEqual(_generate_password(""), _generate_password(""))


class FmtTest(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(_fmt(0), "0 B")
        self.assertEqual(_fmt(512), "512 B")

    def test_kb(self):
        self.assertEqual(_fmt(2048), "2.0 KB")

    def test_mb(self):
        self.assertEqual(_fmt(5 * 1024 * 1024), "5.0 MB")


class SendJmAlbumValidationTest(unittest.IsolatedAsyncioTestCase):
    """send_jm_album 输入校验路径，不触发真实下载/预查。"""

    async def test_invalid_album_id(self):
        p = make_plugin()
        ret = await p.send_jm_album(MagicMock(), 0, "qq:dm:1")
        self.assertIn("正整数", ret)

    async def test_orphan_blocked(self):
        p = make_plugin()
        p._orphan_aids.add(42)
        ret = await p.send_jm_album(MagicMock(), 42, "qq:dm:1")
        self.assertIn("清理", ret)

    async def test_concurrent_limit(self):
        p = make_plugin()
        p._running_tasks = {
            1: MagicMock(done=lambda: False),
            2: MagicMock(done=lambda: False),
        }
        ret = await p.send_jm_album(MagicMock(), 3, "qq:dm:1")
        self.assertIn("过多", ret)

    async def test_bad_target(self):
        p = make_plugin()
        ret = await p.send_jm_album(MagicMock(), 7, "qq-xx-1")
        self.assertIn("错误", ret)

    async def test_cross_session_blocked(self):
        p = make_plugin()
        p._allow_cross_session = False
        ev = MagicMock()
        ev.session.sid = "qq:dm:999"
        ret = await p.send_jm_album(ev, 7, "qq:dm:111")
        self.assertIn("不允许转发", ret)

    async def test_cross_session_allowed(self):
        p = make_plugin()
        p._allow_cross_session = True
        ev = MagicMock()
        ev.session.sid = "qq:dm:999"
        with (
            patch("jmdown.main._fetch_album_meta", return_value={}),
            patch.object(JMdownPlugin, "_task_runner", new=AsyncMock()),
        ):
            ret = await p.send_jm_album(ev, 7, "qq:dm:111")
        self.assertIn("队列", ret)
        self.assertEqual(len(p._running_tasks), 1)

    async def test_dedup_running(self):
        p = make_plugin()
        state = TaskState(job_id="JOB-x", album_id=8, target="qq:dm:1")
        p._task_registry["JOB-x"] = state
        p._running_tasks[8] = MagicMock(done=lambda: False)
        ret = await p.send_jm_album(MagicMock(), 8, "qq:dm:1")
        self.assertIn("队列中", ret)
        self.assertIn("JOB-x", ret)

    async def test_dead_task_superseded(self):
        """超过 upload_timeout+120s 的运行中任务视为死任务，cancel 后允许重新提交。"""
        p = make_plugin()
        state = TaskState(job_id="JOB-old", album_id=9, target="qq:dm:1")
        state.started_at = time.time() - (p._upload_timeout + 200)
        p._task_registry["JOB-old"] = state
        old_task = MagicMock(done=lambda: False)
        p._running_tasks[9] = old_task
        ev = MagicMock()
        ev.session.sid = "qq:dm:1"  # 同会话，避免跨会话拦截
        with (
            patch("jmdown.main._fetch_album_meta", return_value={}),
            patch.object(JMdownPlugin, "_task_runner", new=AsyncMock()),
        ):
            ret = await p.send_jm_album(ev, 9, "qq:dm:1")
        self.assertIn("队列", ret)
        old_task.cancel.assert_called_once()


class ZipCleanupTest(unittest.TestCase):
    """B2: 任务结束（含失败）后临时 ZIP 应被清理。"""

    def test_cleanup_zip_on_success(self):
        p = make_plugin()
        # _cleanup_zip 直接删 aid 对应 zip
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p._cache_dir = Path(d)
            zip_path = Path(d) / "9.zip"
            zip_path.write_bytes(b"fake")
            self.assertTrue(zip_path.exists())
            p._cleanup_zip(9)
            self.assertFalse(zip_path.exists())

    def test_cleanup_zip_missing_is_noop(self):
        p = make_plugin()
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p._cache_dir = Path(d)
            # 不存在不应抛异常
            p._cleanup_zip(123)

    def test_cleanup_zip_none_cache_dir_is_noop(self):
        """_cache_dir 未初始化（None）时不应抛异常。"""
        p = make_plugin()
        p._cache_dir = None
        p._cleanup_zip(1)  # 不应抛 TypeError


class QuerySearchToolTest(unittest.IsolatedAsyncioTestCase):
    """B1: query_jm_album / search_jm_album 应通过 to_thread 调用同步 IO。"""

    async def test_query_jm_album_calls_meta(self):
        p = make_plugin()
        p._content_query = True
        meta = {"album_id": 7, "title": "MyTitle", "description": ""}
        with patch("jmdown.main._fetch_album_meta", return_value=meta) as m:
            ret = await p.query_jm_album(MagicMock(), 7)
        m.assert_called_once_with(7)
        self.assertIn("MyTitle", ret)

    async def test_query_jm_album_blocked_when_off(self):
        p = make_plugin()
        p._content_query = False
        with patch("jmdown.main._fetch_album_meta") as m:
            ret = await p.query_jm_album(MagicMock(), 7)
        m.assert_not_called()
        self.assertIn("关闭", ret)

    async def test_search_jm_album_calls_search(self):
        p = make_plugin()
        p._content_query = True
        results = [(7, "TitleSeven", ["tag1"])]
        with patch("jmdown.main._search_albums", return_value=(1, 1, results)) as m:
            ret = await p.search_jm_album(MagicMock(), keyword="k")
        self.assertTrue(m.called)
        self.assertIn("TitleSeven", ret)

    async def test_search_requires_at_least_one_field(self):
        p = make_plugin()
        p._content_query = True
        ret = await p.search_jm_album(MagicMock())
        self.assertIn("错误", ret)


class BgTaskTest(unittest.IsolatedAsyncioTestCase):
    """B3: _spawn_bg_task 持有引用；terminate 取消后台 task。"""

    async def test_spawn_holds_and_releases(self):
        p = make_plugin()

        async def short():
            await asyncio.sleep(0.01)

        t = p._spawn_bg_task(short())
        self.assertIn(t, p._bg_tasks)
        await t
        # done_callback 应已将其从集合移除
        self.assertNotIn(t, p._bg_tasks)

    async def test_terminate_cancels_bg_tasks(self):
        p = make_plugin()

        async def long():
            await asyncio.sleep(100)

        t = p._spawn_bg_task(long())
        self.assertIn(t, p._bg_tasks)
        await p.terminate()
        self.assertTrue(t.done())
        self.assertEqual(len(p._bg_tasks), 0)


class CleanupTaskTest(unittest.IsolatedAsyncioTestCase):
    """M5: _cleanup_task 自我保护——current_task 不匹配时不误弹。"""

    async def test_pops_self(self):
        p = make_plugin()
        state = TaskState(job_id="JOB-x", album_id=5, target="qq:dm:1")
        p._running_tasks[5] = asyncio.current_task()
        p._cleanup_task(state)
        self.assertNotIn(5, p._running_tasks)

    async def test_skips_when_not_current(self):
        p = make_plugin()
        state = TaskState(job_id="JOB-x", album_id=5, target="qq:dm:1")
        p._running_tasks[5] = MagicMock()  # 非当前 task
        p._cleanup_task(state)
        # current_task != _running_tasks[5] → 不弹
        self.assertIn(5, p._running_tasks)

    async def test_registry_trimmed_to_30(self):
        p = make_plugin()
        # 塞 35 条
        for i in range(35):
            p._task_registry[f"JOB-{i}"] = TaskState(
                job_id=f"JOB-{i}", album_id=i, target="qq:dm:1"
            )
        state = TaskState(job_id="JOB-trim", album_id=99, target="qq:dm:1")
        p._running_tasks[99] = asyncio.current_task()  # 让自我保护放行
        p._cleanup_task(state)
        self.assertLessEqual(len(p._task_registry), 30)


class ConfigLoadTest(unittest.TestCase):
    """C1: _load_config 读取 section 分组配置，并兼容旧版平铺结构。"""

    def test_section_nested(self):
        p = JMdownPlugin(
            MagicMock(),
            {
                "download": {"download_threads": 30, "max_concurrent": 3},
                "content": {
                    "content_query": True,
                    "block_content_tools": False,
                    "allow_cross_session": True,
                },
                "encryption": {"zip_encrypt": False, "custom_password": "pw"},
                "quality": {"pdf_quality": 70},
                "upload": {"upload_timeout": 600, "chunk_size": 1024 * 1024},
                "cache": {"max_cache": 5},
                "notification": {"notify_llm": False},
            },
        )
        p._load_config()
        self.assertEqual(p._download_threads, 30)
        self.assertEqual(p._max_concurrent, 3)
        self.assertTrue(p._content_query)
        self.assertFalse(p._block_content_tools)
        self.assertTrue(p._allow_cross_session)
        self.assertEqual(p._custom_password, "pw")
        self.assertEqual(p._pdf_quality, 70)
        self.assertEqual(p._upload_timeout, 600)
        self.assertEqual(p._chunk_size, 1024 * 1024)
        self.assertEqual(p._max_cache, 5)
        self.assertFalse(p._notify_llm)

    def test_flat_legacy_fallback(self):
        """旧版平铺配置（<2.10.0 生成的 jmdown.json）仍可读取。"""
        p = JMdownPlugin(
            MagicMock(),
            {
                "download_threads": 30,
                "max_concurrent": 3,
                "content_query": True,
                "allow_cross_session": True,
            },
        )
        p._load_config()
        self.assertEqual(p._download_threads, 30)
        self.assertEqual(p._max_concurrent, 3)
        self.assertTrue(p._content_query)
        self.assertTrue(p._allow_cross_session)
        self.assertEqual(p._max_cache, 10)  # 未提供 → 默认

    def test_section_precedence_over_flat(self):
        p = JMdownPlugin(
            MagicMock(),
            {"download": {"download_threads": 60}, "download_threads": 30},
        )
        p._load_config()
        self.assertEqual(p._download_threads, 60)

    def test_section_not_dict_falls_back(self):
        p = JMdownPlugin(MagicMock(), {"download": "oops", "download_threads": 30})
        p._load_config()
        self.assertEqual(p._download_threads, 30)

    def test_defaults_when_empty(self):
        p = JMdownPlugin(MagicMock(), {})
        p._load_config()
        self.assertEqual(p._download_threads, 45)
        self.assertEqual(p._max_concurrent, 2)
        self.assertFalse(p._content_query)
        self.assertTrue(p._block_content_tools)
        self.assertFalse(p._allow_cross_session)
        self.assertFalse(p._zip_encrypt)
        self.assertEqual(p._custom_password, "")
        self.assertEqual(p._pdf_quality, 85)
        self.assertEqual(p._upload_timeout, 300)
        self.assertEqual(p._chunk_size, 512 * 1024)
        self.assertEqual(p._max_cache, 10)
        self.assertTrue(p._notify_llm)

    def test_clamping(self):
        p = JMdownPlugin(
            MagicMock(),
            {
                "download": {"download_threads": 0, "max_concurrent": 0},
                "upload": {"upload_timeout": 0, "chunk_size": 1},
            },
        )
        p._load_config()
        self.assertEqual(p._download_threads, 1)
        self.assertEqual(p._max_concurrent, 1)
        self.assertEqual(p._upload_timeout, 1)
        self.assertEqual(p._chunk_size, 4096)

    def test_zip_encrypt_requires_pyzipper(self):
        p = JMdownPlugin(MagicMock(), {"encryption": {"zip_encrypt": True}})
        with patch("builtins.__import__", side_effect=ImportError):
            with self.assertRaises(RuntimeError):
                p._load_config()


if __name__ == "__main__":
    unittest.main()
