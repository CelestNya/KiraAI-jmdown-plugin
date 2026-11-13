"""标准上传、三方案分派、旧配置迁移与失败语义测试。

重点不在「能不能发出去」（那需要真实 NapCat），而在：
- ``upload_method`` 三选一的分派，默认必须是 path
- 三条路径都没有本地体积闸门：文件原样交给实现端
- 失败信息如实、非空、带诊断指引（明确拒绝 / 无响应超时 / 连接异常）
- 旧版「开关 + 载荷」配置升级后能无损迁移
- 标准链路不得伪造百分比（只能报进度未知）
"""

from __future__ import annotations

import asyncio
import base64
import inspect
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
    send_file_standard,
)
from jmdown.impl_support import UPLOAD_METHODS  # noqa: E402
from jmdown.main import JMdownPlugin, _resolve_upload_method  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    def __init__(self, status: str = "ok", exc: BaseException | None = None) -> None:
        self.status = status
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    async def send_action(self, action: str, params: dict, timeout: float = 10.0) -> dict:
        self.calls.append((action, params))
        if self.exc is not None:
            raise self.exc
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


class SendFileStandardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "album.pdf"
        self.file.write_bytes(b"pdf-bytes")

    async def test_path_mode_sends_absolute_path(self) -> None:
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

    async def test_auto_is_rejected_after_removal(self) -> None:
        client = FakeClient()
        with self.assertRaises(FileSendError) as cm:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="auto")
        self.assertIn("只支持 path / base64", str(cm.exception))
        self.assertEqual(client.calls, [])

    async def test_no_size_gate_signature(self) -> None:
        params = inspect.signature(send_file_standard).parameters
        self.assertNotIn("max_bytes", params)
        self.assertNotIn("max_mb", params)

    async def test_oversize_base64_is_still_sent(self) -> None:
        """旧闸门 10MB：超过它也不该在插件侧被拒绝，交给实现端裁决。"""
        with open(self.file, "wb") as f:
            f.truncate(11 * 1024 * 1024)
        client = FakeClient()
        result = await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="base64")

        self.assertEqual(len(client.calls), 1, "插件不得拦截大文件")
        self.assertTrue(client.calls[0][1]["file"].startswith("base64://"))
        self.assertIn("载荷=base64", result)

    async def test_non_ok_status_quotes_implementation_response(self) -> None:
        client = FakeClient(status="failed")
        with self.assertRaises(FileSendError) as ctx:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="path")
        msg = str(ctx.exception)
        self.assertIn("失败响应", msg)
        self.assertIn("status='failed'", msg)
        self.assertIn("upload_private_file", msg)
        self.assertIn("不设本地体积上限", msg)
        self.assertNotIn("超过内联上限", msg)

    async def test_timeout_with_empty_message_still_yields_useful_error(self) -> None:
        """实测：帧超限只表现为 asyncio.TimeoutError()，其 str 是空字符串。"""
        client = FakeClient(exc=TimeoutError())
        with self.assertRaises(FileSendError) as ctx:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="base64")
        msg = str(ctx.exception)
        self.assertTrue(msg.strip())
        self.assertIn("未收到实现端响应", msg)
        self.assertIn("upload_private_file", msg)
        self.assertIn("300s", msg)
        self.assertIn("Max payload size exceeded", msg)
        self.assertNotIn("文件太大", msg)

    async def test_connection_error_is_reported_not_swallowed(self) -> None:
        client = FakeClient(exc=ConnectionResetError("peer closed"))
        with self.assertRaises(FileSendError) as ctx:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="path")
        msg = str(ctx.exception)
        self.assertIn("ConnectionResetError", msg)
        self.assertIn("peer closed", msg)
        self.assertIn("可核对", msg)

    async def test_websocket_connection_closed_is_reported(self) -> None:
        """ConnectionClosed 的 MRO 不含 OSError，旧 except OSError 抓不到它。"""

        class ConnectionClosed(Exception):
            pass

        client = FakeClient(exc=ConnectionClosed("no close frame received or sent"))
        with self.assertRaises(FileSendError) as ctx:
            await send_file_standard(make_ctx(client), "123456", str(self.file), payload_mode="path")
        msg = str(ctx.exception)
        self.assertIn("连接中断", msg)
        self.assertIn("ConnectionClosed", msg)
        self.assertIn("未收到实现端响应", msg)

    async def test_missing_file_raises(self) -> None:
        with self.assertRaises(FileSendError):
            await send_file_standard(make_ctx(FakeClient()), "123456", str(self.file) + ".nope", payload_mode="path")

    async def test_missing_adapter_raises(self) -> None:
        with self.assertRaises(FileSendError):
            await send_file_standard(no_adapter_ctx(), "123456", str(self.file), payload_mode="path")

    async def test_client_not_initialized_raises(self) -> None:
        with self.assertRaises(FileSendError):
            await send_file_standard(make_ctx(None), "123456", str(self.file), payload_mode="path")

    async def test_standard_upload_never_reports_fake_percent(self) -> None:
        seen: list[tuple[int | None, str]] = []

        async def cb(pct: int | None, note: str) -> None:
            seen.append((pct, note))

        await send_file_standard(make_ctx(FakeClient()), "123456", str(self.file), payload_mode="path", progress_cb=cb)
        self.assertTrue(seen, "至少要明确告知一次进度状态")
        self.assertTrue(all(pct is None for pct, _ in seen), f"伪造了百分比: {seen}")


class DeliverDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "album.pdf"
        self.file.write_bytes(b"x")
        self.plugin = JMdownPlugin(MagicMock(), {})
        self.plugin._upload_timeout = 300

    async def test_default_uses_path_standard_upload(self) -> None:
        mock = AsyncMock(return_value="std")
        with patch("jmdown.file_send.send_file_standard", new=mock):
            result = await self.plugin._deliver("qq:dm:1", "1", False, None, str(self.file), None)

        self.assertEqual(result, "std")
        kwargs = mock.await_args.kwargs
        self.assertEqual(kwargs["payload_mode"], "path")
        self.assertNotIn("max_bytes", kwargs, "本地体积闸门已移除")

    async def test_base64_method_is_passed_through(self) -> None:
        self.plugin._upload_method = "base64"
        mock = AsyncMock(return_value="std")
        with patch("jmdown.file_send.send_file_standard", new=mock):
            await self.plugin._deliver("qq:dm:1", "1", False, None, str(self.file), None)
        self.assertEqual(mock.await_args.kwargs["payload_mode"], "base64")

    async def test_stream_method_uses_chunked_upload(self) -> None:
        self.plugin._upload_method = "stream"
        mock = AsyncMock(return_value="stream")
        with patch("jmdown.napcat_stream.send_file_via_stream", new=mock):
            result = await self.plugin._deliver("qq:gm:2", "2", True, "2", str(self.file), None)

        self.assertEqual(result, "stream")
        self.assertEqual(mock.await_args.kwargs["chunk_size"], self.plugin._chunk_size)

    async def test_watchdog_timeout_is_not_an_empty_string(self) -> None:
        async def never_finishes():
            await asyncio.sleep(3600)

        with self.assertRaises(TimeoutError) as ctx:
            await self.plugin._upload_with_watchdog(never_finishes(), timeout=1)
        msg = str(ctx.exception)
        self.assertTrue(msg.strip())
        self.assertIn("未收到实现端响应", msg)
        self.assertIn("1s", msg)


class UploadMethodConfigTest(unittest.TestCase):
    def test_schema_is_exactly_three_methods_defaulting_to_path(self) -> None:
        schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))["upload"]["fields"]
        field = schema["upload_method"]
        self.assertEqual(field["options"], list(UPLOAD_METHODS))
        self.assertEqual(field["default"], "path")
        for key in ("use_napcat_stream", "file_payload", "standard_max_mb"):
            self.assertNotIn(key, schema)
        for hint in (field["hint"], field["locales"]["zh"]["hint"]):
            for method in UPLOAD_METHODS:
                self.assertIn(method, hint)

    def test_clean_config_uses_default(self) -> None:
        self.assertEqual(_resolve_upload_method(None, {}, {}), "path")

    def test_new_value_wins(self) -> None:
        for method in UPLOAD_METHODS:
            with self.subTest(method=method):
                self.assertEqual(_resolve_upload_method(method, {}, {}), method)

    def test_invalid_value_falls_back_to_default(self) -> None:
        self.assertEqual(_resolve_upload_method("auto", {}, {}), "path")
        self.assertEqual(_resolve_upload_method("nonsense", {}, {}), "path")

    def test_legacy_stream_switch_migrates(self) -> None:
        self.assertEqual(_resolve_upload_method(None, {"use_napcat_stream": True}, {}), "stream")

    def test_legacy_payload_migrates(self) -> None:
        self.assertEqual(_resolve_upload_method(None, {"file_payload": "base64"}, {}), "base64")
        self.assertEqual(_resolve_upload_method(None, {"file_payload": "path"}, {}), "path")

    def test_legacy_auto_migrates_to_path(self) -> None:
        # auto 已移除：跨机用户必须在 WebUI 显式改方案，日志会告警
        self.assertEqual(_resolve_upload_method(None, {"file_payload": "auto"}, {}), "path")

    def test_flat_legacy_keys_migrate(self) -> None:
        self.assertEqual(_resolve_upload_method(None, {}, {"use_napcat_stream": True}), "stream")
        self.assertEqual(_resolve_upload_method(None, {}, {"file_payload": "base64"}), "base64")

    def test_legacy_switch_takes_precedence_over_payload(self) -> None:
        cfg = {"use_napcat_stream": True, "file_payload": "path"}
        self.assertEqual(_resolve_upload_method(None, cfg, {}), "stream")


if __name__ == "__main__":
    unittest.main()
