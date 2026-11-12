"""e2e 基础设施：假的 OneBot v11 正向 WS 服务端 + 最小 WS 客户端。

为什么需要它：jmdown 的上传链路分「插件逻辑」与「协议帧」两层，单元测试只
覆盖前者。这里起一个真实 WebSocket 服务端，让插件代码真的把 JSON 帧发到
网络上，由服务端按 NapCat / SnowLuma 的实际响应格式回包、按 base64 解码、
按分片重组并校验 SHA256 —— 不连 QQ、不需要 NapCat 在线，CI 可直接跑。

``MiniOneBotClient`` 复刻 KiraAI ``NapCatWebSocketClient`` 的 echo/Future 模型，
但不带登录闸门与重连状态机；真实客户端的联动验证见
``scripts/e2e_real_core.py``（在 KiraAI 根目录运行）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

# SnowLuma stream-file.ts 的 stream_id 白名单；服务端据此拒绝不合规 ID
SAFE_STREAM_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_HEADERS_KWARG = (
    "additional_headers"
    if "additional_headers" in inspect.signature(websockets.connect).parameters
    else "extra_headers"
)


@dataclass
class DeliveredFile:
    action: str
    name: str
    mode: str
    payload: str
    content: bytes
    params: dict

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class FakeOneBotServer:
    """记录所有动作请求，并模拟文件发送/分片上传的服务端行为。"""

    token: str = "e2e-token"
    self_id: int = 10001
    fail_actions: frozenset[str] = field(default_factory=frozenset)
    omit_status: bool = False  # 模拟只回 retcode 的实现
    requests: list[dict] = field(default_factory=list)
    delivered: list[DeliveredFile] = field(default_factory=list)
    chunk_requests: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    streams: dict[str, dict] = field(default_factory=dict)
    connections: int = 0
    bad_auth: list[str] = field(default_factory=list)
    url: str = ""
    _server: Any = field(default=None, repr=False)
    _temp_files: list[Path] = field(default_factory=list, repr=False)

    async def start(self) -> str:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0, max_size=2**24, open_timeout=5)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for p in self._temp_files:
            p.unlink(missing_ok=True)
        self._temp_files.clear()

    def actions(self) -> list[str]:
        return [r["action"] for r in self.requests]

    async def _handle(self, ws, *args) -> None:
        request = getattr(ws, "request", None)
        headers = getattr(request, "headers", {}) if request is not None else {}
        auth = headers.get("Authorization", "") if headers else ""
        if self.token and auth != f"Bearer {self.token}":
            self.bad_auth.append(auth)
            await ws.close(1008, "invalid token")
            return

        self.connections += 1
        # NapCat / SnowLuma 连上后都推 lifecycle 元事件
        await ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": self.self_id,
                    "time": int(time.time()),
                }
            )
        )

        async for raw in ws:
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(req, dict):
                continue
            action = str(req.get("action", ""))
            params = req.get("params") or {}
            echo = req.get("echo")
            self.requests.append({"action": action, "params": params, "echo": echo})

            if action in self.fail_actions:
                resp = self._failed(f"injected failure for {action}")
            else:
                resp = self._respond(action, params)
            if self.omit_status:
                resp.pop("status", None)
            resp["echo"] = echo
            await ws.send(json.dumps(resp, ensure_ascii=False))

    @staticmethod
    def _ok(data: dict | None = None) -> dict:
        return {"status": "ok", "retcode": 0, "data": data or {}, "message": "", "wording": ""}

    @staticmethod
    def _failed(message: str) -> dict:
        return {"status": "failed", "retcode": 1404, "data": {}, "message": message, "wording": message}

    def _respond(self, action: str, params: dict) -> dict:
        if action == "get_login_info":
            return self._ok({"user_id": self.self_id, "nickname": "FakeBot"})
        if action in ("upload_private_file", "upload_group_file"):
            return self._deliver(action, params)
        if action == "upload_file_stream":
            return self._stream(params)
        return self._failed(f"unknown action {action}")

    def _deliver(self, action: str, params: dict) -> dict:
        value = str(params.get("file", ""))
        name = str(params.get("name", ""))
        if value.startswith("base64://"):
            try:
                content = base64.b64decode(value[len("base64://") :], validate=True)
            except (ValueError, binascii.Error):
                return self._failed("invalid base64 payload")
            mode, payload = "base64", value[:24]
        elif value.startswith("file://"):
            content = Path(value[len("file://") :]).read_bytes()
            mode, payload = "file_uri", value
        else:
            path = Path(value)
            if not path.is_file():
                return self._failed(f"file not found: {value}")
            content = path.read_bytes()
            mode, payload = "path", value

        self.delivered.append(DeliveredFile(action, name, mode, payload, content, dict(params)))
        return self._ok({"file_id": f"fake-{len(self.delivered)}"})

    def _stream(self, params: dict) -> dict:
        self.chunk_requests.append(dict(params))
        sid = str(params.get("stream_id", ""))
        if not SAFE_STREAM_ID.match(sid):
            return self._failed(f"invalid stream_id: {sid!r}")

        state = self.streams.setdefault(sid, {"chunks": {}, "total": None, "size": None, "sha": None})
        if params.get("is_complete"):
            chunks: dict[int, bytes] = state["chunks"]
            total = state["total"]
            if total is None or len(chunks) != total or any(i not in chunks for i in range(total)):
                return self._failed(f"incomplete stream: {len(chunks)}/{total}")
            content = b"".join(chunks[i] for i in range(total))
            if state["size"] is not None and len(content) != state["size"]:
                return self._failed("file_size mismatch")
            digest = hashlib.sha256(content).hexdigest()
            if state["sha"] and digest != state["sha"]:
                return self._failed("sha256 mismatch")
            tmp = Path(tempfile.gettempdir()) / f"jmdown_e2e_{sid}.bin"
            tmp.write_bytes(content)
            self._temp_files.append(tmp)
            return self._ok(
                {
                    "status": "file_complete",
                    "stream_id": sid,
                    "received_chunks": total,
                    "total_chunks": total,
                    "file_path": str(tmp),
                    "file_size": len(content),
                    "sha256": digest,
                }
            )

        idx = int(params.get("chunk_index", -1))
        data = params.get("chunk_data", "")
        try:
            state["chunks"][idx] = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            return self._failed("invalid chunk_data")
        state["total"] = int(params.get("total_chunks", state["total"] or 0))
        state["size"] = params.get("file_size", state["size"])
        state["sha"] = params.get("expected_sha256", state["sha"])
        return self._ok(
            {
                "status": "chunk_received",
                "stream_id": sid,
                "received_chunks": len(state["chunks"]),
                "total_chunks": state["total"],
            }
        )


class MiniOneBotClient:
    """最小 OneBot WS 客户端：与 KiraAI client.py 相同的 echo/Future 模型。"""

    def __init__(self, url: str, token: str = "e2e-token") -> None:
        self.ws_url = url
        self.access_token = token
        self.ws = None
        self.events: list[dict] = []
        self._futures: dict[str, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None

    async def connect(self) -> MiniOneBotClient:
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        self.ws = await websockets.connect(self.ws_url, **{_HEADERS_KWARG: headers}, max_size=2**24, open_timeout=5)
        self._recv_task = asyncio.create_task(self._recv_loop())
        return self

    async def _recv_loop(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                echo = msg.get("echo")
                if echo is not None:
                    fut = self._futures.pop(str(echo), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                else:
                    self.events.append(msg)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_action(self, action: str, params: dict, timeout: float = 10.0) -> dict:
        if self.ws is None:
            raise ConnectionError("MiniOneBotClient 未连接")
        echo = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._futures[echo] = future
        frame = {"action": action, "params": params, "echo": echo}
        await self.ws.send(json.dumps(frame, ensure_ascii=False))
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._futures.pop(echo, None)

    async def get_login_info(self) -> dict:
        return await self.send_action("get_login_info", {})

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        if self._recv_task is not None:
            try:
                await asyncio.wait_for(self._recv_task, timeout=2)
            except (TimeoutError, asyncio.CancelledError):
                self._recv_task.cancel()
            self._recv_task = None
        self.ws = None
