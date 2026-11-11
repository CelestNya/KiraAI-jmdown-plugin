"""标准上传（OneBot 通用动作）与发送链路分派测试。

重点不在「能不能发出去」（那需要真实 NapCat），而在：
- 载荷形态判定正确（同机路径 / 跨机 base64），这是跨机部署最容易踩的坑
- 超限前置拒绝，而不是发到一半撞实现方上限再以超时收场
- `_deliver` 的开关分派：默认必须走标准路线，分片只在显式开启时使用
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import _core_stub

_core_stub.bootstrap()

from jmdown.file_send import (  # noqa: E402
    FileSendError,
    resolve_payload_mode,
    send_file_standard,
)
from jmdown.main import JMdownPlugin  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    """记录 send_action 调用；ws_url 用于载荷形态判定。"""

    def __init__(self, ws_url: str = "ws://localhost:3001", status: str = "ok") -> None:
        self.ws_url = ws_url
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    async def send_action(self, action: str, params: dict, timeout: float = 10.0) -> dict:
        self.calls.append((action, params))
        return {"status": self.status, "data": {}}


class FakeAdapter:
    def __init__(self, client: FakeClient | None) -> None:
        self.info = SimpleNamespace(platform="QQ")
        self._client = client

    def get_client(self) -> FakeClient | None:
        return self._client


def make_ctx(client: FakeClient | None) -> SimpleNamespace:
    adapter = FakeAdapter(client)
    return SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapters=lambda: {"qq": adapter}))


def no_adapter_ctx() -> SimpleNamespace:
    return SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapters=lambda: {}))


class ResolvePayloadModeTest(unittest.TestCase):
    def test_auto_loopback_is_path(self) -> None:
        for url in ("ws://localhost:3001", "ws://127.0.0.1:3001", "ws://[::1]:3001"):
            with self.subTest(url=url):
                self.assertEqual(resolve_payload_mode("auto", url), "path")

    def test_auto_remote_is_base64(self) -> None:
        # 跨机给路径只会得到远端「文件不存在」，必须内联内容
        self.assertEqual(resolve_payload_mode("auto", "ws://10.0.0.5:3001"), "base64")

    def test_auto_without_url_falls_back_to_base64(self) -> None:
        self.assertEqual(resolve_payload_mode("auto", None), "base64")
        self.assertEqual(resolve_payload_mode("auto", ""), "base64")

    def test_explicit_mode_wins(self) -> None:
        self.assertEqual(resolve_payload_mode("path", "ws://10.0.0.5:3001"), "path")
        self.assertEqual(resolve_payload_mode("base64", "ws://localhost:3001"), "base64")


class SendFileStandardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "album.pdf"
        self.file.write_bytes(b"pdf-bytes")

    async def test_private_path_mode_sends_absolute_path(self) -> None:
        client = FakeClient()
        result = await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="path")

        action, params = client.calls[0]
        self.assertEqual(action, "upload_private_file")
        self.assertEqual(params["user_id"], "123456")
        self.assertEqual(params["name"], "album.pdf")
        self.assertTrue(Path(params["file"]).is_absolute())
        self.assertNotIn("base64://", params["file"])
        self.assertIn("载荷=path", result)

    async def test_group_path_mode_uses_group_action(self) -> None:
        client = FakeClient()
        await send_file_standard(
            make_ctx(client), "789", str(self.file), is_group=True, group_id="789", payload_mode="path"
        )
        action, params = client.calls[0]
        self.assertEqual(action, "upload_group_file")
        self.assertEqual(params["group_id"], "789")

    async def test_base64_mode_inlines_content(self) -> None:
        client = FakeClient()
        await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="base64")

        _, params = client.calls[0]
        self.assertTrue(params["file"].startswith("base64://"))
        decoded = base64.b64decode(params["file"][len("base64://") :])
        self.assertEqual(decoded, b"pdf-bytes")

    async def test_auto_mode_uses_base64_for_remote_napcat(self) -> None:
        client = FakeClient(ws_url="ws://192.168.1.9:3001")
        await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="auto")
        _, params = client.calls[0]
        self.assertTrue(params["file"].startswith("base64://"))

    async def test_oversize_inline_is_rejected_before_sending(self) -> None:
        client = FakeClient()
        with self.assertRaises(FileSendError) as ctx:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="base64", max_bytes=1)
        self.assertIn("内联上限", str(ctx.exception))
        self.assertEqual(client.calls, [], "超限必须在发送前拒绝")

    async def test_non_ok_status_surfaces_actionable_hint(self) -> None:
        client = FakeClient(status="failed")
        with self.assertRaises(FileSendError) as ctx:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="path")
        self.assertIn("分片", str(ctx.exception))

    async def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileSendError):
            await send_file_standard(make_ctx(FakeClient()), "123456", str(self.file) + ".nope", payload_mode="path")

    async def test_missing_adapter_raises(self) -> None:
        with self.assertRaises(FileSendError):
            await send_file_standard(no_adapter_ctx(), "123456", str(self.file), payload_mode="path")

    async def test_client_not_initialized_raises(self) -> None:
        with self.assertRaises(FileSendError):
            await send_file_standard(make_ctx(None), "123456", str(self.file), payload_mode="path")

    async def test_progress_reaches_100(self) -> None:
        seen: list[tuple[int, str]] = []

        async def cb(pct: int, note: str) -> None:
            seen.append((pct, note))

        await send_file_standard(make_ctx(FakeClient()), "123456", str(self.file), payload_mode="path", progress_cb=cb)
        self.assertEqual(seen[-1][0], 100)


class DeliverDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "album.pdf"
        self.file.write_bytes(b"x")
        self.plugin = JMdownPlugin(MagicMock(), {})
        self.plugin._upload_timeout = 300

    async def test_default_uses_standard_upload(self) -> None:
        mock = AsyncMock(return_value="std")
        with patch("jmdown.file_send.send_file_standard", new=mock):
            result = await self.plugin._deliver("qq:dm:1", "1", False, None, str(self.file), None)

        self.assertEqual(result, "std")
        kwargs = mock.await_args.kwargs
        self.assertEqual(kwargs["payload_mode"], "auto")
        self.assertEqual(kwargs["max_bytes"], 10 * 1024 * 1024)

    async def test_switch_on_uses_napcat_stream(self) -> None:
        self.plugin._use_napcat_stream = True
        mock = AsyncMock(return_value="stream")
        with patch("jmdown.napcat_stream.send_file_via_stream", new=mock):
            result = await self.plugin._deliver("qq:gm:2", "2", True, "2", str(self.file), None)

        self.assertEqual(result, "stream")
        self.assertEqual(mock.await_args.kwargs["chunk_size"], self.plugin._chunk_size)


class DefaultConfigTest(unittest.TestCase):
    def test_stream_switch_defaults_off(self) -> None:
        # 默认值即政策：分片是 NapCat 专有能力，不能让用户「默认继承」
        schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
        self.assertIs(schema["upload"]["fields"]["use_napcat_stream"]["default"], False)


if __name__ == "__main__":
    unittest.main()
