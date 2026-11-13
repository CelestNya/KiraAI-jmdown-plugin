"""上传链路 e2e：插件真实代码 → 真实 WebSocket 帧 → 假 OneBot 服务端。

这一层验证的是「线上真正会发生什么」：`_deliver()` 发出的动作名、参数、
载荷编码、分片序列、完成帧与最终发送，全部由服务端按 NapCat / SnowLuma
的响应格式解析并校验。下载与 PDF 合成不在本层（它们不触网，由单元测试覆盖）。

运行：``pytest tests/test_upload_e2e.py -v``，不需要 NapCat / QQ / KiraAI 在线。
真实 KiraAI 客户端的联动验证见 ``scripts/e2e_real_core.py``。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from _onebot_server import SAFE_STREAM_ID, FakeOneBotServer, MiniOneBotClient
from jmdown.cache import CacheEntry, CacheIndex
from jmdown.file_send import FileSendError
from jmdown.impl_support import STREAM_IMPLS
from jmdown.main import JMdownPlugin, TaskState


class FakeAdapter:
    def __init__(self, client: MiniOneBotClient) -> None:
        self.info = SimpleNamespace(platform="QQ")
        self._client = client

    def get_client(self) -> MiniOneBotClient:
        return self._client


@dataclass
class E2E:
    """一次 e2e 用例的运行时上下文：真实文件 + 真实 WS 两端。"""

    file: Path
    content: bytes
    sha256: str
    server: FakeOneBotServer
    client: MiniOneBotClient
    tmp_path: Path
    progress: list[tuple[int | None, str]] = field(default_factory=list)

    def plugin(self, **overrides: object) -> JMdownPlugin:
        ctx = SimpleNamespace(
            adapter_mgr=SimpleNamespace(get_adapters=lambda: {"qq": FakeAdapter(self.client)}),
            publish_notice=AsyncMock(),
        )
        p = JMdownPlugin(ctx, {})
        p._upload_timeout = 300
        p._chunk_size = 1024
        p._upload_method = "path"
        # _task_runner / _completion_notice 用到、平时由 _load_config 设置的字段
        p._notify_llm = True
        p._content_query = False
        p._zip_encrypt = False
        p._custom_password = ""
        p._download_threads = 4
        p._cache_dir = self.tmp_path
        for key, value in overrides.items():
            setattr(p, key, value)
        return p

    async def deliver(
        self,
        *,
        is_group: bool = False,
        group_id: str | None = None,
        file: Path | None = None,
        **overrides: object,
    ) -> str:
        p = self.plugin(**overrides)
        return await p._deliver("qq:dm:123456", "123456", is_group, group_id, str(file or self.file), self.cb)

    async def cb(self, pct: int | None, note: str) -> None:
        self.progress.append((pct, note))

    async def wait_for_lifecycle(self) -> None:
        async def poll() -> None:
            while not any(e.get("meta_event_type") == "lifecycle" for e in self.client.events):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout=2)


@pytest.fixture
async def e2e(tmp_path: Path):
    # 中文 + 空格 + 点号，专测文件名清洗
    f = tmp_path / "测试 本子.pdf"
    content = bytes(range(256)) * 40 + b"JMDOWN-E2E-PAYLOAD"
    f.write_bytes(content)

    server = FakeOneBotServer()
    url = await server.start()
    client = await MiniOneBotClient(url).connect()
    try:
        yield E2E(
            file=f,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            server=server,
            client=client,
            tmp_path=tmp_path,
        )
    finally:
        await client.close()
        await server.stop()


# ── 握手与鉴权 ────────────────────────────────────────────────────────


async def test_handshake_token_and_lifecycle(e2e: E2E) -> None:
    """真 WS 连接：Bearer 鉴权、get_login_info echo 匹配、lifecycle 元事件。"""
    resp = await e2e.client.get_login_info()
    assert resp["data"]["user_id"] == e2e.server.self_id
    await e2e.wait_for_lifecycle()
    assert e2e.server.connections == 1
    assert e2e.server.bad_auth == []
    assert e2e.server.actions() == ["get_login_info"]


# ── 标准方案：path / base64 ───────────────────────────────────────────


async def test_standard_default_uses_path_and_preserves_bytes(e2e: E2E) -> None:
    result = await e2e.deliver()
    assert e2e.server.actions() == ["upload_private_file"]
    delivered = e2e.server.delivered[0]
    assert delivered.mode == "path"
    assert delivered.content == e2e.content
    assert delivered.sha256 == e2e.sha256
    assert delivered.name == e2e.file.name
    assert delivered.params["user_id"] == "123456"
    assert "载荷=path" in result
    assert e2e.progress
    assert all(pct is None for pct, _ in e2e.progress), f"标准链路伪造了百分比: {e2e.progress}"


async def test_standard_base64_group_roundtrip(e2e: E2E) -> None:
    result = await e2e.deliver(is_group=True, group_id="789", _upload_method="base64")
    assert e2e.server.actions() == ["upload_group_file"]
    delivered = e2e.server.delivered[0]
    assert delivered.mode == "base64"
    assert delivered.payload.startswith("base64://")
    assert delivered.content == e2e.content
    assert delivered.params["group_id"] == "789"
    assert "载荷=base64" in result
    assert e2e.progress
    assert all(pct is None for pct, _ in e2e.progress), f"标准链路伪造了百分比: {e2e.progress}"


async def test_base64_is_explicit_not_guessed_from_ws_url(e2e: E2E) -> None:
    # auto 已移除：即使地址看起来是远端，也只有显式选 base64 才内联
    e2e.client.ws_url = "ws://192.168.1.9:3001"
    await e2e.deliver()
    assert e2e.server.delivered[0].mode == "path"

    e2e.server.delivered.clear()
    result = await e2e.deliver(_upload_method="base64")
    assert e2e.server.delivered[0].mode == "base64"
    assert "载荷=base64" in result


async def test_oversize_base64_is_not_blocked_locally(e2e: E2E) -> None:
    """旧 10MB 闸门已移除：超限文件照发，由实现端裁决。"""
    big = e2e.tmp_path / "big.bin"
    big.write_bytes(b"x" * (11 * 1024 * 1024))
    result = await e2e.deliver(file=big, _upload_method="base64")
    assert e2e.server.actions() == ["upload_private_file"]
    assert e2e.server.delivered[0].mode == "base64"
    assert len(e2e.server.delivered[0].content) == 11 * 1024 * 1024
    assert "载荷=base64" in result


async def test_retcode_only_response_is_accepted(e2e: E2E) -> None:
    """部分实现只回 retcode=0 不带 status，不能被误判为失败。"""
    e2e.server.omit_status = True
    probe = await e2e.client.get_login_info()
    assert "status" not in probe
    assert probe["retcode"] == 0
    result = await e2e.deliver()
    assert "标准上传发送" in result
    assert len(e2e.server.delivered) == 1


# ── 失败语义（标准方案） ──────────────────────────────────────────────


async def test_failed_action_reports_implementation_response(e2e: E2E) -> None:
    """实现端明确拒绝：如实转述响应，不臆断原因、不编造体积上限。"""
    e2e.server.fail_actions = frozenset({"upload_private_file"})
    with pytest.raises(FileSendError) as cm:
        await e2e.deliver()
    msg = str(cm.value)
    assert msg.strip()
    assert "upload_private_file" in msg
    assert "status='failed'" in msg
    assert "retcode=1404" in msg
    assert "injected failure" in msg
    assert "不设本地体积上限" in msg
    assert "超过内联上限" not in msg
    assert "文件太大" not in msg
    for name in STREAM_IMPLS.split("、"):
        assert name in msg


async def test_failed_action_hint_lists_supported_impls(e2e: E2E) -> None:
    e2e.server.fail_actions = frozenset({"upload_private_file"})
    with pytest.raises(FileSendError) as cm:
        await e2e.deliver()
    msg = str(cm.value)
    assert "标准上传失败" in msg
    assert "SnowLuma" in msg
    assert "同机部署" in msg


# ── 分片方案 ──────────────────────────────────────────────────────────


async def test_stream_chunk_failure_reports_implementation_response(e2e: E2E) -> None:
    """stream 分片阶段：如实转述实现端响应，不臆断原因。"""
    e2e.server.fail_actions = frozenset({"upload_file_stream"})
    with pytest.raises(FileSendError) as cm:
        await e2e.deliver(_upload_method="stream")
    msg = str(cm.value)
    assert msg.strip()
    assert "分片 1/" in msg
    assert "status='failed'" in msg
    assert "retcode=1404" in msg
    assert "不设本地体积上限" in msg


async def test_stream_send_stage_failure_reports_implementation_response(e2e: E2E) -> None:
    """stream 实现端发送阶段：单独标注阶段，不混同为分片失败。"""
    e2e.server.fail_actions = frozenset({"upload_private_file"})
    with pytest.raises(FileSendError) as cm:
        await e2e.deliver(_upload_method="stream")
    msg = str(cm.value)
    assert "实现端发送" in msg
    assert "upload_private_file" in msg
    assert "status='failed'" in msg


async def test_stream_timeout_is_not_an_empty_string(e2e: E2E) -> None:
    """分片链路的空消息 TimeoutError 也必须转成有内容的失败信息。"""
    with patch.object(e2e.client, "send_action", new=AsyncMock(side_effect=TimeoutError())):
        with pytest.raises(FileSendError) as cm:
            await e2e.deliver(_upload_method="stream")
    msg = str(cm.value)
    assert msg.strip()
    assert "未收到实现端响应" in msg
    assert "upload_file_stream" in msg


async def test_stream_chain_reassembles_exact_file(e2e: E2E) -> None:
    result = await e2e.deliver(_upload_method="stream")
    actions = e2e.server.actions()
    chunks = [r for r in e2e.server.chunk_requests if not r.get("is_complete")]
    completes = [r for r in e2e.server.chunk_requests if r.get("is_complete")]

    total = (len(e2e.content) + 1023) // 1024
    assert len(chunks) == total
    assert len(completes) == 1
    assert actions[-1] == "upload_private_file"
    assert actions.count("upload_file_stream") == total + 1

    stream_ids = {r["stream_id"] for r in e2e.server.chunk_requests}
    assert len(stream_ids) == 1
    assert re.fullmatch(SAFE_STREAM_ID, stream_ids.pop())
    assert [r["chunk_index"] for r in chunks] == list(range(total))
    for r in chunks:
        assert r["total_chunks"] == total
        assert r["file_size"] == len(e2e.content)
        assert r["expected_sha256"] == e2e.sha256

    # 服务端按分片重组后再被插件当作远端 temp 路径发出去
    delivered = e2e.server.delivered[0]
    assert delivered.mode == "path"
    assert delivered.content == e2e.content
    assert delivered.sha256 == e2e.sha256
    assert "流传输" in result
    assert any(isinstance(pct, int) for pct, _ in e2e.progress), "分片期间应有真实百分比"
    assert e2e.progress[-1][0] is None, "实现端发送阶段没有进度，不能挂在 100%"


async def test_stream_group_uses_upload_group_file(e2e: E2E) -> None:
    await e2e.deliver(_upload_method="stream", is_group=True, group_id="888")
    assert e2e.server.actions()[-1] == "upload_group_file"
    assert e2e.server.delivered[0].params["group_id"] == "888"


# ── 任务状态机全链路 ──────────────────────────────────────────────────


async def test_task_runner_cache_hit_end_to_end(e2e: E2E) -> None:
    """任务状态机 → 缓存命中 → 上传 → 完成通知，全链路走真实 WS。

    下载/PDF 合成不触网，用真实 CacheIndex 装一条指向本地文件的记录，
    从而把「插件真的能完成一次发送任务」纳入 e2e 而非只测 _deliver。
    """
    p = e2e.plugin()
    cache = CacheIndex(e2e.tmp_path / "cache_index.json", 10)
    cache.put(
        CacheEntry(
            album_id=123,
            title="e2e",
            description="",
            page_count=1,
            pdf_path=str(e2e.file),
            size_bytes=len(e2e.content),
            downloaded_at=time.time(),
        )
    )
    p._cache = cache
    state = TaskState(job_id="JOB-E2E", album_id=123, target="qq:dm:123456")

    await p._task_runner(state)

    assert state.error is None
    assert state.status == "done"
    assert state.phases["下载"] == "缓存"
    assert state.phases["上传"] == "已完成"
    assert state.result["from_cache"]
    assert "标准上传发送" in state.result["send_result"]
    assert e2e.server.actions() == ["upload_private_file"]
    assert e2e.server.delivered[0].content == e2e.content
    assert p.ctx.publish_notice.await_count == 1
    assert p.ctx.publish_notice.await_args.kwargs["is_mentioned"]


async def test_task_runner_reports_failure_notice(e2e: E2E) -> None:
    """上传失败时任务转 failed 并推失败通知，不能静默吞掉。"""
    e2e.server.fail_actions = frozenset({"upload_private_file"})
    p = e2e.plugin()
    cache = CacheIndex(e2e.tmp_path / "cache_index.json", 10)
    cache.put(CacheEntry(123, "e2e", "", 1, str(e2e.file), len(e2e.content), time.time()))
    p._cache = cache
    state = TaskState(job_id="JOB-FAIL", album_id=123, target="qq:dm:123456")

    await p._task_runner(state)

    assert state.status == "failed"
    assert "标准上传失败" in (state.error or "")
    assert p.ctx.publish_notice.await_count == 1
