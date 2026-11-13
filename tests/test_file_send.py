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
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmdown.file_send import FileSendError, send_file_standard
from jmdown.impl_support import UPLOAD_METHODS
from jmdown.main import JMdownPlugin, _resolve_upload_method

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    """只记录调用、按需抛出异常，不做任何协议判断。"""

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


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    p = tmp_path / "album.pdf"
    p.write_bytes(b"pdf-bytes")
    return p


@pytest.fixture
def plugin(tmp_path: Path) -> JMdownPlugin:
    f = tmp_path / "album.pdf"
    f.write_bytes(b"x")
    p = JMdownPlugin(MagicMock(), {})
    p._upload_timeout = 300
    p._test_file = str(f)  # type: ignore[attr-defined]
    return p


# ── 标准上传：payload 形态与动作选择 ──────────────────────────────────


async def test_path_mode_sends_absolute_path(pdf: Path) -> None:
    client = FakeClient()
    result = await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="path")

    action, params = client.calls[0]
    assert action == "upload_private_file"
    assert params["user_id"] == "123456"
    assert params["name"] == "album.pdf"
    assert Path(params["file"]).is_absolute()
    assert "base64://" not in params["file"]
    assert "载荷=path" in result


async def test_group_path_mode_uses_group_action(pdf: Path) -> None:
    client = FakeClient()
    await send_file_standard(make_ctx(client), "789", str(pdf), is_group=True, group_id="789", payload_mode="path")
    action, params = client.calls[0]
    assert action == "upload_group_file"
    assert params["group_id"] == "789"


async def test_base64_mode_inlines_content(pdf: Path) -> None:
    client = FakeClient()
    await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="base64")

    _, params = client.calls[0]
    assert params["file"].startswith("base64://")
    assert base64.b64decode(params["file"][len("base64://") :]) == b"pdf-bytes"


async def test_auto_is_rejected_after_removal(pdf: Path) -> None:
    client = FakeClient()
    with pytest.raises(FileSendError) as cm:
        await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="auto")
    assert "只支持 path / base64" in str(cm.value)
    assert client.calls == []


def test_no_size_gate_in_signature() -> None:
    params = inspect.signature(send_file_standard).parameters
    assert "max_bytes" not in params
    assert "max_mb" not in params


async def test_oversize_base64_is_still_sent(pdf: Path) -> None:
    """旧闸门 10MB：超过它也不该在插件侧被拒绝，交给实现端裁决。"""
    with open(pdf, "wb") as f:
        f.truncate(11 * 1024 * 1024)
    client = FakeClient()
    result = await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="base64")

    assert len(client.calls) == 1, "插件不得拦截大文件"
    assert client.calls[0][1]["file"].startswith("base64://")
    assert "载荷=base64" in result


# ── 失败语义 ──────────────────────────────────────────────────────────


async def test_non_ok_status_quotes_implementation_response(pdf: Path) -> None:
    client = FakeClient(status="failed")
    with pytest.raises(FileSendError) as cm:
        await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="path")
    msg = str(cm.value)
    assert "失败响应" in msg
    assert "status='failed'" in msg
    assert "upload_private_file" in msg
    assert "不设本地体积上限" in msg
    assert "超过内联上限" not in msg


async def test_timeout_with_empty_message_still_yields_useful_error(pdf: Path) -> None:
    """实测：帧超限只表现为 asyncio.TimeoutError()，其 str 是空字符串。"""
    client = FakeClient(exc=TimeoutError())
    with pytest.raises(FileSendError) as cm:
        await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="base64")
    msg = str(cm.value)
    assert msg.strip()
    assert "未收到实现端响应" in msg
    assert "upload_private_file" in msg
    assert "300s" in msg
    assert "Max payload size exceeded" in msg
    assert "文件太大" not in msg


async def test_connection_error_is_reported_not_swallowed(pdf: Path) -> None:
    client = FakeClient(exc=ConnectionResetError("peer closed"))
    with pytest.raises(FileSendError) as cm:
        await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="path")
    msg = str(cm.value)
    assert "ConnectionResetError" in msg
    assert "peer closed" in msg
    assert "可核对" in msg


async def test_websocket_connection_closed_is_reported(pdf: Path) -> None:
    """ConnectionClosed 的 MRO 不含 OSError，旧 except OSError 抓不到它。"""

    class ConnectionClosed(Exception):
        pass

    client = FakeClient(exc=ConnectionClosed("no close frame received or sent"))
    with pytest.raises(FileSendError) as cm:
        await send_file_standard(make_ctx(client), "123456", str(pdf), payload_mode="path")
    msg = str(cm.value)
    assert "连接中断" in msg
    assert "ConnectionClosed" in msg
    assert "未收到实现端响应" in msg


async def test_missing_file_raises(pdf: Path) -> None:
    with pytest.raises(FileSendError):
        await send_file_standard(make_ctx(FakeClient()), "123456", str(pdf) + ".nope", payload_mode="path")


async def test_missing_adapter_raises(pdf: Path) -> None:
    with pytest.raises(FileSendError):
        await send_file_standard(no_adapter_ctx(), "123456", str(pdf), payload_mode="path")


async def test_client_not_initialized_raises(pdf: Path) -> None:
    with pytest.raises(FileSendError):
        await send_file_standard(make_ctx(None), "123456", str(pdf), payload_mode="path")


async def test_standard_upload_never_reports_fake_percent(pdf: Path) -> None:
    seen: list[tuple[int | None, str]] = []

    async def cb(pct: int | None, note: str) -> None:
        seen.append((pct, note))

    await send_file_standard(make_ctx(FakeClient()), "123456", str(pdf), payload_mode="path", progress_cb=cb)
    assert seen, "至少要明确告知一次进度状态"
    assert all(pct is None for pct, _ in seen), f"伪造了百分比: {seen}"


# ── _deliver 三方案分派 ───────────────────────────────────────────────


async def test_default_uses_path_standard_upload(plugin: JMdownPlugin) -> None:
    mock = AsyncMock(return_value="std")
    with patch("jmdown.file_send.send_file_standard", new=mock):
        result = await plugin._deliver("qq:dm:1", "1", False, None, plugin._test_file, None)  # type: ignore[attr-defined]

    assert result == "std"
    kwargs = mock.await_args.kwargs
    assert kwargs["payload_mode"] == "path"
    assert "max_bytes" not in kwargs, "本地体积闸门已移除"


async def test_base64_method_is_passed_through(plugin: JMdownPlugin) -> None:
    plugin._upload_method = "base64"
    mock = AsyncMock(return_value="std")
    with patch("jmdown.file_send.send_file_standard", new=mock):
        await plugin._deliver("qq:dm:1", "1", False, None, plugin._test_file, None)  # type: ignore[attr-defined]
    assert mock.await_args.kwargs["payload_mode"] == "base64"


async def test_stream_method_uses_chunked_upload(plugin: JMdownPlugin) -> None:
    plugin._upload_method = "stream"
    mock = AsyncMock(return_value="stream")
    with patch("jmdown.napcat_stream.send_file_via_stream", new=mock):
        result = await plugin._deliver("qq:gm:2", "2", True, "2", plugin._test_file, None)  # type: ignore[attr-defined]

    assert result == "stream"
    assert mock.await_args.kwargs["chunk_size"] == plugin._chunk_size


async def test_watchdog_timeout_is_not_an_empty_string(plugin: JMdownPlugin) -> None:
    async def never_finishes() -> str:
        await asyncio.sleep(3600)
        return "unreachable"

    with pytest.raises(TimeoutError) as cm:
        await plugin._upload_with_watchdog(never_finishes(), timeout=1)
    msg = str(cm.value)
    assert msg.strip()
    assert "未收到实现端响应" in msg
    assert "1s" in msg


# ── 配置解析与旧配置迁移 ──────────────────────────────────────────────


def test_schema_is_exactly_three_methods_defaulting_to_path() -> None:
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))["upload"]["fields"]
    field = schema["upload_method"]
    assert field["options"] == list(UPLOAD_METHODS)
    assert field["default"] == "path"
    for key in ("use_napcat_stream", "file_payload", "standard_max_mb"):
        assert key not in schema
    for hint in (field["hint"], field["locales"]["zh"]["hint"]):
        for method in UPLOAD_METHODS:
            assert method in hint


def test_clean_config_uses_default() -> None:
    assert _resolve_upload_method(None, {}, {}) == "path"


@pytest.mark.parametrize("method", list(UPLOAD_METHODS))
def test_new_value_wins(method: str) -> None:
    assert _resolve_upload_method(method, {}, {}) == method


@pytest.mark.parametrize("bad", ["auto", "nonsense"])
def test_invalid_value_falls_back_to_default(bad: str) -> None:
    assert _resolve_upload_method(bad, {}, {}) == "path"


def test_legacy_stream_switch_migrates() -> None:
    assert _resolve_upload_method(None, {"use_napcat_stream": True}, {}) == "stream"


@pytest.mark.parametrize(("legacy", "expected"), [("base64", "base64"), ("path", "path")])
def test_legacy_payload_migrates(legacy: str, expected: str) -> None:
    assert _resolve_upload_method(None, {"file_payload": legacy}, {}) == expected


def test_legacy_auto_migrates_to_path() -> None:
    # auto 已移除：跨机用户必须在 WebUI 显式改方案，日志会告警
    assert _resolve_upload_method(None, {"file_payload": "auto"}, {}) == "path"


@pytest.mark.parametrize(
    ("flat", "expected"),
    [({"use_napcat_stream": True}, "stream"), ({"file_payload": "base64"}, "base64")],
    ids=["stream_switch", "payload"],
)
def test_flat_legacy_keys_migrate(flat: dict[str, Any], expected: str) -> None:
    assert _resolve_upload_method(None, {}, flat) == expected


def test_legacy_switch_takes_precedence_over_payload() -> None:
    cfg = {"use_napcat_stream": True, "file_payload": "path"}
    assert _resolve_upload_method(None, cfg, {}) == "stream"
