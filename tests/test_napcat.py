"""napcat_stream.py 测试 — 纯函数 + 分片上传链路 mock。

Run:
    uv run python -m pytest tests/test_napcat.py -v
    # or
    uv run python -m unittest tests.test_napcat -v
"""
# napcat_stream 依赖 core.plugin.logger，运行时由本文件注入 sys.modules
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import logging
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_core_mock() -> None:
    """注入 core.* 桩模块（仅当真实 core 不可用时）。"""
    existing = sys.modules.get("core")
    if existing is not None and hasattr(existing, "plugin"):
        return
    core: Any = types.ModuleType("core")
    plugin_pkg: Any = types.ModuleType("core.plugin")
    plugin_pkg.logger = logging.getLogger("napcat.test")
    plugin_pkg.BasePlugin = object
    plugin_pkg.PluginContext = object

    def register_tool(name: str, description: str, params: dict):
        def deco(f):
            return f

        return deco

    plugin_pkg.register_tool = register_tool
    core.plugin = plugin_pkg
    sys.modules["core"] = core
    sys.modules["core.plugin"] = plugin_pkg


_install_core_mock()

from napcat_stream import (  # noqa: E402
    _find_qq_adapter,
    _fmt,
    _sha256_file,
    send_file_via_stream,
    stream_upload_file,
)


class FmtTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_fmt(0), "0 B")
        self.assertEqual(_fmt(1023), "1023 B")
        self.assertEqual(_fmt(2048), "2.0 KB")
        self.assertEqual(_fmt(5 * 1024 * 1024), "5.0 MB")


class Sha256Test(unittest.TestCase):
    def test_hash_matches_stdlib(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.bin"
            data = b"hello world" * 1000
            p.write_bytes(data)
            self.assertEqual(_sha256_file(str(p)), hashlib.sha256(data).hexdigest())


class FindAdapterTest(unittest.TestCase):
    def test_finds_qq(self):
        adapter = MagicMock()
        adapter.info.platform = "QQ"
        mgr = MagicMock()
        mgr.get_adapters.return_value = {"qq_bot": adapter}
        self.assertIs(_find_qq_adapter(mgr), adapter)

    def test_returns_none_when_no_qq(self):
        adapter = MagicMock()
        adapter.info.platform = "Telegram"
        mgr = MagicMock()
        mgr.get_adapters.return_value = {"tg": adapter}
        self.assertIsNone(_find_qq_adapter(mgr))


class StreamUploadTest(unittest.IsolatedAsyncioTestCase):
    """分片上传核心链路：分片数 + is_complete 信号 + file_path 返回。"""

    async def test_chunks_and_complete(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            fp = Path(d) / "test.bin"
            data = b"x" * 40  # 40 字节，chunk_size=16 → 3 片
            fp.write_bytes(data)
            calls: list[tuple[str, dict]] = []

            async def fake_send_action(action, params, timeout=10):
                calls.append((action, params))
                if params.get("is_complete"):
                    return {"status": "ok", "data": {"file_path": "/remote/test.bin"}}
                return {"status": "ok", "data": {}}

            client = MagicMock()
            client.send_action = fake_send_action

            progress: list[int] = []

            async def cb(pct: int, spd: str):
                progress.append(pct)

            remote = await stream_upload_file(
                client, str(fp), timeout=5, progress_cb=cb, chunk_size=16
            )
            self.assertEqual(remote, "/remote/test.bin")
            # 3 分片 + 1 完成信号
            self.assertEqual(len(calls), 4)
            self.assertTrue(calls[-1][1].get("is_complete"))
            self.assertTrue(progress)  # 进度回调被触发

    async def test_raises_on_chunk_fail(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            fp = Path(d) / "t.bin"
            fp.write_bytes(b"x" * 40)

            async def fake_send(action, params, timeout=10):
                return {"status": "failed", "data": {}}

            client = MagicMock()
            client.send_action = fake_send
            with self.assertRaises(RuntimeError):
                await stream_upload_file(client, str(fp), chunk_size=16)

    async def test_raises_when_no_file_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            fp = Path(d) / "t.bin"
            fp.write_bytes(b"x" * 40)

            async def fake_send(action, params, timeout=10):
                if params.get("is_complete"):
                    return {"status": "ok", "data": {}}  # 缺 file_path
                return {"status": "ok", "data": {}}

            client = MagicMock()
            client.send_action = fake_send
            with self.assertRaises(RuntimeError):
                await stream_upload_file(client, str(fp), chunk_size=16)


class SendFileViaStreamTest(unittest.IsolatedAsyncioTestCase):
    """E1: 发送阶段（upload_private_file/group_file）必须用独立 send_timeout。

    分片上传每块小（512KB）30s 足够；NapCat 处理大文件发送（从 temp 转存
    到 QQ 服务器）可能远超 30s，若沿用分片超时会把已成功的发送误判为失败。
    """

    async def _run(self, *, is_group: bool = False) -> list[tuple[str, dict, float]]:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            fp = Path(d) / "t.bin"
            fp.write_bytes(b"x" * 32)
            calls: list[tuple[str, dict, float]] = []

            async def fake_send(action, params, timeout=10):
                calls.append((action, params, timeout))
                if params.get("is_complete"):
                    return {"status": "ok", "data": {"file_path": "/remote/t.bin"}}
                return {"status": "ok", "data": {}}

            client = MagicMock()
            client.send_action = fake_send
            adapter = MagicMock()
            adapter.info.platform = "QQ"
            adapter.get_client.return_value = client
            ctx = MagicMock()
            ctx.adapter_mgr.get_adapters.return_value = {"qq": adapter}

            if is_group:
                await send_file_via_stream(
                    ctx,
                    "qq:gm:999",
                    "123",
                    str(fp),
                    is_group=True,
                    group_id="999",
                    timeout=30,
                    send_timeout=420,
                    chunk_size=16,
                )
            else:
                await send_file_via_stream(
                    ctx,
                    "qq:dm:123",
                    "123",
                    str(fp),
                    timeout=30,
                    send_timeout=420,
                    chunk_size=16,
                )
            return calls

    async def test_private_send_uses_send_timeout(self):
        calls = await self._run()
        for action, _, t in calls:
            if action == "upload_private_file":
                self.assertEqual(t, 420)
            elif action == "upload_file_stream":
                self.assertEqual(t, 30)

    async def test_group_send_uses_send_timeout(self):
        calls = await self._run(is_group=True)
        for action, _, t in calls:
            if action == "upload_group_file":
                self.assertEqual(t, 420)


if __name__ == "__main__":
    unittest.main()
