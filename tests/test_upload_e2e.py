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
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import _core_stub

_core_stub.bootstrap()

from _onebot_server import (  # noqa: E402
    SAFE_STREAM_ID,
    FakeOneBotServer,
    MiniOneBotClient,
)
from jmdown.cache import CacheEntry, CacheIndex  # noqa: E402
from jmdown.file_send import FileSendError  # noqa: E402
from jmdown.impl_support import STREAM_IMPLS  # noqa: E402
from jmdown.main import JMdownPlugin, TaskState  # noqa: E402


class FakeAdapter:
    def __init__(self, client) -> None:
        self.info = SimpleNamespace(platform="QQ")
        self._client = client

    def get_client(self):
        return self._client


class UploadE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.file = Path(self._tmp.name) / "测试 本子.pdf"  # 中文 + 空格 + 点号，专测清洗
        self.content = bytes(range(256)) * 40 + b"JMDOWN-E2E-PAYLOAD"
        self.file.write_bytes(self.content)
        self.sha256 = hashlib.sha256(self.content).hexdigest()

        self.server = FakeOneBotServer()
        url = await self.server.start()
        self.client = await MiniOneBotClient(url).connect()
        self.progress: list[tuple[int, str]] = []

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.stop()
        self._tmp.cleanup()

    async def _cb(self, pct: int | None, note: str) -> None:
        self.progress.append((pct, note))

    def _plugin(self, **overrides) -> JMdownPlugin:
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
        p._cache_dir = Path(self._tmp.name)
        for key, value in overrides.items():
            setattr(p, key, value)
        return p

    async def _deliver(self, *, is_group: bool = False, group_id: str | None = None, **overrides) -> str:
        p = self._plugin(**overrides)
        return await p._deliver("qq:dm:123456", "123456", is_group, group_id, str(self.file), self._cb)

    async def _wait_for_lifecycle(self) -> None:
        async def poll() -> None:
            while not any(e.get("meta_event_type") == "lifecycle" for e in self.client.events):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), timeout=2)

    async def test_handshake_token_and_lifecycle(self) -> None:
        """真 WS 连接：Bearer 鉴权、get_login_info echo 匹配、lifecycle 元事件。"""
        resp = await self.client.get_login_info()
        self.assertEqual(resp["data"]["user_id"], self.server.self_id)
        await self._wait_for_lifecycle()
        self.assertEqual(self.server.connections, 1)
        self.assertEqual(self.server.bad_auth, [])
        self.assertEqual(self.server.actions(), ["get_login_info"])

    async def test_standard_default_uses_path_and_preserves_bytes(self) -> None:
        result = await self._deliver()
        self.assertEqual(self.server.actions(), ["upload_private_file"])
        delivered = self.server.delivered[0]
        self.assertEqual(delivered.mode, "path")
        self.assertEqual(delivered.content, self.content)
        self.assertEqual(delivered.sha256, self.sha256)
        self.assertEqual(delivered.name, self.file.name)
        self.assertEqual(delivered.params["user_id"], "123456")
        self.assertIn("载荷=path", result)
        self.assertTrue(self.progress)
        self.assertTrue(all(pct is None for pct, _ in self.progress), f"标准链路伪造了百分比: {self.progress}")

    async def test_standard_base64_group_roundtrip(self) -> None:
        result = await self._deliver(is_group=True, group_id="789", _upload_method="base64")
        self.assertEqual(self.server.actions(), ["upload_group_file"])
        delivered = self.server.delivered[0]
        self.assertEqual(delivered.mode, "base64")
        self.assertTrue(delivered.payload.startswith("base64://"))
        self.assertEqual(delivered.content, self.content)
        self.assertEqual(delivered.params["group_id"], "789")
        self.assertIn("载荷=base64", result)
        self.assertTrue(self.progress)
        self.assertTrue(all(pct is None for pct, _ in self.progress), f"标准链路伪造了百分比: {self.progress}")

    async def test_base64_is_explicit_not_guessed_from_ws_url(self) -> None:
        # auto 已移除：即使地址看起来是远端，也只有显式选 base64 才内联
        self.client.ws_url = "ws://192.168.1.9:3001"
        await self._deliver()
        self.assertEqual(self.server.delivered[0].mode, "path")

        self.server.delivered.clear()
        result = await self._deliver(_upload_method="base64")
        self.assertEqual(self.server.delivered[0].mode, "base64")
        self.assertIn("载荷=base64", result)

    async def test_failed_action_reports_implementation_response(self) -> None:
        """实现端明确拒绝：如实转述响应，不腉断原因、不编造体积上限。"""
        self.server.fail_actions = frozenset({"upload_private_file"})
        with self.assertRaises(FileSendError) as cm:
            await self._deliver()
        msg = str(cm.exception)
        self.assertTrue(msg.strip())
        self.assertIn("upload_private_file", msg)
        self.assertIn("status='failed'", msg)
        self.assertIn("retcode=1404", msg)
        self.assertIn("injected failure", msg)
        self.assertIn("不设本地体积上限", msg)
        self.assertNotIn("超过内联上限", msg)
        self.assertNotIn("文件太大", msg)
        for name in STREAM_IMPLS.split("、"):
            self.assertIn(name, msg)

    async def test_oversize_base64_is_not_blocked_locally(self) -> None:
        """旧 10MB 闸门已移除：超限文件照发，由实现端裁决。"""
        big = self.file.parent / "big.bin"
        big.write_bytes(b"x" * (11 * 1024 * 1024))
        self.file = big
        result = await self._deliver(_upload_method="base64")
        self.assertEqual(self.server.actions(), ["upload_private_file"])
        self.assertEqual(self.server.delivered[0].mode, "base64")
        self.assertEqual(len(self.server.delivered[0].content), 11 * 1024 * 1024)
        self.assertIn("载荷=base64", result)

    async def test_stream_chunk_failure_reports_implementation_response(self) -> None:
        """stream 分片阶段：如实转述实现端响应，不臆断原因。"""
        self.server.fail_actions = frozenset({"upload_file_stream"})
        with self.assertRaises(FileSendError) as cm:
            await self._deliver(_upload_method="stream")
        msg = str(cm.exception)
        self.assertTrue(msg.strip())
        self.assertIn("分片 1/", msg)
        self.assertIn("status='failed'", msg)
        self.assertIn("retcode=1404", msg)
        self.assertIn("不设本地体积上限", msg)

    async def test_stream_send_stage_failure_reports_implementation_response(self) -> None:
        """stream 实现端发送阶段：单独标注阶段，不混同为分片失败。"""
        self.server.fail_actions = frozenset({"upload_private_file"})
        with self.assertRaises(FileSendError) as cm:
            await self._deliver(_upload_method="stream")
        msg = str(cm.exception)
        self.assertIn("实现端发送", msg)
        self.assertIn("upload_private_file", msg)
        self.assertIn("status='failed'", msg)

    async def test_stream_timeout_is_not_an_empty_string(self) -> None:
        """分片链路的空消息 TimeoutError 也必须转成有内容的失败信息。"""
        with patch.object(self.client, "send_action", new=AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(FileSendError) as cm:
                await self._deliver(_upload_method="stream")
        msg = str(cm.exception)
        self.assertTrue(msg.strip())
        self.assertIn("未收到实现端响应", msg)
        self.assertIn("upload_file_stream", msg)

    async def test_stream_chain_reassembles_exact_file(self) -> None:
        result = await self._deliver(_upload_method="stream")
        actions = self.server.actions()
        chunks = [r for r in self.server.chunk_requests if not r.get("is_complete")]
        completes = [r for r in self.server.chunk_requests if r.get("is_complete")]

        total = (len(self.content) + 1023) // 1024
        self.assertEqual(len(chunks), total)
        self.assertEqual(len(completes), 1)
        self.assertEqual(actions[-1], "upload_private_file")
        self.assertEqual(actions.count("upload_file_stream"), total + 1)

        stream_ids = {r["stream_id"] for r in self.server.chunk_requests}
        self.assertEqual(len(stream_ids), 1)
        self.assertRegex(stream_ids.pop(), SAFE_STREAM_ID)
        self.assertEqual([r["chunk_index"] for r in chunks], list(range(total)))
        for r in chunks:
            self.assertEqual(r["total_chunks"], total)
            self.assertEqual(r["file_size"], len(self.content))
            self.assertEqual(r["expected_sha256"], self.sha256)

        # 服务端按分片重组后再被插件当作远端 temp 路径发出去
        delivered = self.server.delivered[0]
        self.assertEqual(delivered.mode, "path")
        self.assertEqual(delivered.content, self.content)
        self.assertEqual(delivered.sha256, self.sha256)
        self.assertIn("流传输", result)
        self.assertTrue(any(isinstance(pct, int) for pct, _ in self.progress), "分片期间应有真实百分比")
        self.assertIsNone(self.progress[-1][0], "实现端发送阶段没有进度，不能挂在 100%")

    async def test_stream_group_uses_upload_group_file(self) -> None:
        await self._deliver(_upload_method="stream", is_group=True, group_id="888")
        self.assertEqual(self.server.actions()[-1], "upload_group_file")
        self.assertEqual(self.server.delivered[0].params["group_id"], "888")

    async def test_failed_action_hint_lists_supported_impls(self) -> None:
        self.server.fail_actions = frozenset({"upload_private_file"})
        with self.assertRaises(FileSendError) as cm:
            await self._deliver()
        msg = str(cm.exception)
        self.assertIn("标准上传失败", msg)
        self.assertIn("SnowLuma", msg)
        self.assertIn("同机部署", msg)

    async def test_retcode_only_response_is_accepted(self) -> None:
        """部分实现只回 retcode=0 不带 status，不能被误判为失败。"""
        self.server.omit_status = True
        probe = await self.client.get_login_info()
        self.assertNotIn("status", probe)
        self.assertEqual(probe["retcode"], 0)
        result = await self._deliver()
        self.assertIn("标准上传发送", result)
        self.assertEqual(len(self.server.delivered), 1)

    async def test_task_runner_cache_hit_end_to_end(self) -> None:
        """任务状态机 → 缓存命中 → 上传 → 完成通知，全链路走真实 WS。

        下载/PDF 合成不触网，用真实 CacheIndex 装一条指向本地文件的记录，
        从而把「插件真的能完成一次发送任务」纳入 e2e 而非只测 _deliver。
        """
        p = self._plugin()
        cache = CacheIndex(Path(self._tmp.name) / "cache_index.json", 10)
        cache.put(
            CacheEntry(
                album_id=123,
                title="e2e",
                description="",
                page_count=1,
                pdf_path=str(self.file),
                size_bytes=len(self.content),
                downloaded_at=time.time(),
            )
        )
        p._cache = cache
        state = TaskState(job_id="JOB-E2E", album_id=123, target="qq:dm:123456")

        await p._task_runner(state)

        self.assertIsNone(state.error)
        self.assertEqual(state.status, "done")
        self.assertEqual(state.phases["下载"], "缓存")
        self.assertEqual(state.phases["上传"], "已完成")
        self.assertTrue(state.result["from_cache"])
        self.assertIn("标准上传发送", state.result["send_result"])
        self.assertEqual(self.server.actions(), ["upload_private_file"])
        self.assertEqual(self.server.delivered[0].content, self.content)
        self.assertEqual(p.ctx.publish_notice.await_count, 1)
        notice_kwargs = p.ctx.publish_notice.await_args.kwargs
        self.assertTrue(notice_kwargs["is_mentioned"])

    async def test_task_runner_reports_failure_notice(self) -> None:
        """上传失败时任务转 failed 并推失败通知，不能静默吞掉。"""
        self.server.fail_actions = frozenset({"upload_private_file"})
        p = self._plugin()
        cache = CacheIndex(Path(self._tmp.name) / "cache_index.json", 10)
        cache.put(CacheEntry(123, "e2e", "", 1, str(self.file), len(self.content), time.time()))
        p._cache = cache
        state = TaskState(job_id="JOB-FAIL", album_id=123, target="qq:dm:123456")

        await p._task_runner(state)

        self.assertEqual(state.status, "failed")
        self.assertIn("标准上传失败", state.error or "")
        self.assertEqual(p.ctx.publish_notice.await_count, 1)


if __name__ == "__main__":
    unittest.main()
