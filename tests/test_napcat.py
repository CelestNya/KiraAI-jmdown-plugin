"""napcat_stream.py 测试 —— 纯函数 + 分片上传链路 mock。

napcat_stream 依赖 ``core.plugin.logger``，桩由 ``tests/_core_stub.py`` 注入。

Run:
    pytest tests/test_napcat.py -v
"""

# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from jmdown.napcat_stream import (
    StallGuard,
    _call_action,
    _find_qq_adapter,
    _fmt,
    _make_stream_id,
    _sha256_file,
    send_file_via_stream,
    stream_upload_file,
)


class ConnectionClosed(Exception):
    """模拟 websockets.exceptions.ConnectionClosed（注意：它不是 OSError）。"""


def make_file(tmp_path: Path, size: int, name: str = "t.bin") -> Path:
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def make_client(send_action: Any) -> MagicMock:
    client = MagicMock()
    client.send_action = send_action
    return client


def make_ctx(client: MagicMock) -> MagicMock:
    adapter = MagicMock()
    adapter.info.platform = "QQ"
    adapter.get_client.return_value = client
    ctx = MagicMock()
    ctx.adapter_mgr.get_adapters.return_value = {"qq": adapter}
    return ctx


# ── StallGuard ────────────────────────────────────────────────────────


def test_expires_after_window() -> None:
    g = StallGuard(window=0.05)
    assert not g.expired()
    time.sleep(0.08)
    assert g.expired()


def test_touch_resets() -> None:
    g = StallGuard(window=0.05)
    time.sleep(0.03)
    g.touch()
    assert not g.expired()


def test_expect_sets_stage_and_window() -> None:
    g = StallGuard(window=60)
    g.expect("实现端发送", window=480)
    assert g.stage == "实现端发送"
    assert g.window == 480


def test_window_has_floor() -> None:
    assert StallGuard(window=0).window > 0


# ── _call_action 的错误包装 ───────────────────────────────────────────


async def test_connection_closed_wrapped() -> None:
    async def boom(action: str, params: dict, timeout: float = 10) -> dict:
        raise ConnectionClosed("no close frame received or sent")

    with pytest.raises(RuntimeError) as cm:
        await _call_action(make_client(boom), "upload_private_file", {}, 5, stage="实现端发送")
    msg = str(cm.value)
    assert "连接中断" in msg
    assert "ConnectionClosed" in msg
    assert "可核对" in msg


async def test_non_connection_error_passes_through() -> None:
    async def boom(action: str, params: dict, timeout: float = 10) -> dict:
        raise ValueError("bug in client")

    with pytest.raises(ValueError):
        await _call_action(make_client(boom), "upload_private_file", {}, 5, stage="实现端发送")


@pytest.mark.parametrize(
    ("action", "stage", "expect_note"),
    [
        ("upload_file_stream", "分片 1/3", True),
        ("upload_private_file", "实现端发送", False),
    ],
    ids=["mid_chunks", "send_stage"],
)
async def test_incomplete_chunk_note_only_mid_chunks(action: str, stage: str, expect_note: bool) -> None:
    """只有逐片阶段断连才可能留下残缺分片，其他阶段不能说这句话。"""

    async def boom(_action: str, params: dict, timeout: float = 10) -> dict:
        raise ConnectionClosed("closed")

    with pytest.raises(RuntimeError) as cm:
        await _call_action(make_client(boom), action, {}, 5, stage=stage)
    assert ("残缺" in str(cm.value)) is expect_note


# ── 分片链路与 guard 的配合 ───────────────────────────────────────────


async def test_stream_upload_advances_guard_per_chunk(tmp_path: Path) -> None:
    """分片链路要持续 touch guard，否则外层无进展看门狗会误杀活跃传输。"""
    fp = make_file(tmp_path, 40)
    g = StallGuard(window=0.05)
    idles: list[float] = []

    async def fake_send(action: str, params: dict, timeout: float = 10) -> dict:
        idles.append(g.idle())  # 每片发出前，距上次实现端确认的间隔
        time.sleep(0.08)  # 单次往返比窗口还慢，只有 ack 后 touch 才能救
        if params.get("is_complete"):
            return {"status": "ok", "data": {"file_path": "/remote/t.bin"}}
        return {"status": "ok", "data": {}}

    remote = await stream_upload_file(make_client(fake_send), str(fp), timeout=30, chunk_size=16, guard=g)

    assert remote == "/remote/t.bin"
    assert g.stage == "分片完成确认"
    # 去掉逐片 touch 后这里的 idle 会累积到 0.08 并超过窗口
    assert all(i < 0.05 for i in idles), f"分片 ack 未重置停滞计时: {idles}"


async def test_activity_hook_fires_per_ack(tmp_path: Path) -> None:
    fp = make_file(tmp_path, 40)
    beats: list[float] = []

    async def fake_send(action: str, params: dict, timeout: float = 10) -> dict:
        if params.get("is_complete"):
            return {"status": "ok", "data": {"file_path": "/remote/t.bin"}}
        return {"status": "ok", "data": {}}

    await stream_upload_file(
        make_client(fake_send), str(fp), timeout=30, chunk_size=16, on_activity=lambda: beats.append(time.time())
    )
    # 3 片 + 1 次完成确认 = 4 次 ack
    assert len(beats) == 4


async def test_send_phase_widens_window(tmp_path: Path) -> None:
    fp = make_file(tmp_path, 32)
    g = StallGuard(window=1)

    async def fake_send(action: str, params: dict, timeout: float = 10) -> dict:
        if params.get("is_complete"):
            return {"status": "ok", "data": {"file_path": "/remote/t.bin"}}
        return {"status": "ok", "data": {}}

    await send_file_via_stream(
        make_ctx(make_client(fake_send)),
        "qq:dm:1",
        "1",
        str(fp),
        timeout=30,
        send_timeout=420,
        chunk_size=16,
        guard=g,
    )
    assert g.stage == "实现端发送"
    assert g.window >= 480


# ── 纯函数 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (1023, "1023 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
    ids=["zero", "bytes", "kb", "mb"],
)
def test_fmt_units(size: int, expected: str) -> None:
    assert _fmt(size) == expected


def test_sha256_matches_stdlib(tmp_path: Path) -> None:
    data = b"hello world" * 1000
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    assert _sha256_file(str(p)) == hashlib.sha256(data).hexdigest()


def test_find_adapter_finds_qq() -> None:
    adapter = MagicMock()
    adapter.info.platform = "QQ"
    mgr = MagicMock()
    mgr.get_adapters.return_value = {"qq_bot": adapter}
    assert _find_qq_adapter(mgr) is adapter


def test_find_adapter_returns_none_when_no_qq() -> None:
    adapter = MagicMock()
    adapter.info.platform = "Telegram"
    mgr = MagicMock()
    mgr.get_adapters.return_value = {"tg": adapter}
    assert _find_qq_adapter(mgr) is None


# ── 分片上传核心链路 ──────────────────────────────────────────────────


async def test_chunks_and_complete(tmp_path: Path) -> None:
    """分片数 + is_complete 信号 + file_path 返回。"""
    fp = make_file(tmp_path, 40, "test.bin")  # 40 字节，chunk_size=16 → 3 片
    calls: list[tuple[str, dict]] = []
    progress: list[int | None] = []

    async def fake_send_action(action: str, params: dict, timeout: float = 10) -> dict:
        calls.append((action, params))
        if params.get("is_complete"):
            return {"status": "ok", "data": {"file_path": "/remote/test.bin"}}
        return {"status": "ok", "data": {}}

    async def cb(pct: int | None, spd: str) -> None:
        progress.append(pct)

    remote = await stream_upload_file(make_client(fake_send_action), str(fp), timeout=5, progress_cb=cb, chunk_size=16)
    assert remote == "/remote/test.bin"
    assert len(calls) == 4  # 3 分片 + 1 完成信号
    assert calls[-1][1].get("is_complete")
    assert progress  # 进度回调被触发


async def test_raises_on_chunk_fail(tmp_path: Path) -> None:
    fp = make_file(tmp_path, 40)

    async def fake_send(action: str, params: dict, timeout: float = 10) -> dict:
        return {"status": "failed", "data": {}}

    with pytest.raises(RuntimeError):
        await stream_upload_file(make_client(fake_send), str(fp), chunk_size=16)


async def test_raises_when_no_file_path(tmp_path: Path) -> None:
    fp = make_file(tmp_path, 40)

    async def fake_send(action: str, params: dict, timeout: float = 10) -> dict:
        return {"status": "ok", "data": {}}  # 完成帧缺 file_path

    with pytest.raises(RuntimeError):
        await stream_upload_file(make_client(fake_send), str(fp), chunk_size=16)


# ── 发送阶段独立超时（E1） ────────────────────────────────────────────


async def run_send(*, tmp_path: Path, is_group: bool = False) -> list[tuple[str, dict, float]]:
    """E1: 发送阶段（upload_private_file/group_file）必须用独立 send_timeout。

    分片上传每块小（512KB）30s 足够；NapCat 处理大文件发送（从 temp 转存
    到 QQ 服务器）可能远超 30s，若沿用分片超时会把已成功的发送误判为失败。
    """
    fp = make_file(tmp_path, 32)
    calls: list[tuple[str, dict, float]] = []

    async def fake_send(action: str, params: dict, timeout: float = 10) -> dict:
        calls.append((action, params, timeout))
        if params.get("is_complete"):
            return {"status": "ok", "data": {"file_path": "/remote/t.bin"}}
        return {"status": "ok", "data": {}}

    kwargs: dict[str, Any] = {"timeout": 30, "send_timeout": 420, "chunk_size": 16}
    sid = "qq:dm:123"
    if is_group:
        kwargs.update(is_group=True, group_id="999")
        sid = "qq:gm:999"
    await send_file_via_stream(make_ctx(make_client(fake_send)), sid, "123", str(fp), **kwargs)
    return calls


@pytest.mark.parametrize("is_group", [False, True], ids=["private", "group"])
async def test_send_uses_send_timeout(tmp_path: Path, is_group: bool) -> None:
    calls = await run_send(tmp_path=tmp_path, is_group=is_group)
    send_action_name = "upload_group_file" if is_group else "upload_private_file"
    seen_send = False
    for action, _, t in calls:
        if action == send_action_name:
            assert t == 420, f"{action} 应该用 send_timeout"
            seen_send = True
        elif action == "upload_file_stream":
            assert t == 30, "分片应该用单片超时"
    assert seen_send, f"没有观察到 {send_action_name}"


# ── stream_id 白名单 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["123456.pdf", "本子 名称.zip", "a+b(c).pdf", ".hidden", ""],
)
def test_stream_id_matches_snowluma_whitelist(name: str) -> None:
    """SnowLuma 用 /^[A-Za-z0-9_-]{1,128}$/ 校验 stream_id。

    原实现直接拼 basename（含 .pdf / 中文 / 空格），第一片就会被拒绝。
    """
    sid = _make_stream_id(f"/tmp/jmdown/{name}")
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid)
    assert len(sid) <= 128
    assert sid.startswith("jmdown_")


def test_stream_id_does_not_collide() -> None:
    assert _make_stream_id("/tmp/a.pdf") != _make_stream_id("/tmp/b.pdf")
