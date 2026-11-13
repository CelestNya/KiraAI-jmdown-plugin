"""分片上传（upload_file_stream），大文件时绕开请求体上限。

该动作由 NapCat 引入，SnowLuma 做了 NapCat-compatible 实现；其余实现
（LLOneBot / Lagrange.OneBot / OpenShamrock / go-cqhttp）不提供，能力矩阵见
``impl_support.py``。复用主 WS 连接（adapter.get_client()），不开新连接。
由配置 ``upload_method=stream`` 显式选择。

分片期间的百分比是真实的（已确认分片数 / 总分片数）；分片完成后实现端
把文件转存并发送给 QQ 这一段仍无进度回报，因此会额外上报一次 ``None``。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import time
from pathlib import Path

from core.plugin import logger

from .impl_support import (
    FileSendError,
    diagnosis_hint,
    format_action_failure,
    format_disconnect,
    format_no_response,
    is_disconnect_error,
    is_ok_response,
    response_summary,
)


class NapCatConfigError(FileSendError):
    """配置错误（适配器/客户端不可用），与其余上传失败同族。"""

    pass


CHUNK_SIZE = 512 * 1024  # 单片默认 512KB；实测 NapCat 4.18.14 maxPayload=50MiB，非文件大小闸门


class StallGuard:
    """记录「最近一次收到实现端确认」的时刻，供上层做无进展判定。

    分片方案的总时长不可预估——``分片数 × 每片往返 + 实现端转存``，100MB 在
    250KB/s 上就超过 7 分钟。用固定总时长当闸门，只会把进度一路正常的传输
    误判成超时（而文案还写着「未收到响应」，属于我们自己的假话）。真正要抓的
    是「长时间没有任何 ack」这一种卡死，所以这里只维护时间戳和阈值。
    """

    __slots__ = ("_last", "_window", "stage")

    def __init__(self, window: float):
        self._last = time.monotonic()
        self._window = max(0.01, float(window))
        self.stage = "启动"

    @property
    def window(self) -> float:
        return self._window

    def expect(self, stage: str, window: float | None = None) -> None:
        """进入新阶段：换个名字、换阈值，并重置计时（阶段切换本身算进展）。"""
        self.stage = stage
        if window is not None:
            self._window = max(0.01, float(window))
        self.touch()

    def touch(self) -> None:
        self._last = time.monotonic()

    def idle(self) -> float:
        return time.monotonic() - self._last

    def expired(self) -> bool:
        return self.idle() > self._window


async def _call_action(
    client, action: str, params: dict, timeout: float, *, stage: str, size_bytes: int | None = None
) -> dict:
    """统一包装 send_action：超时/断连都转成有内容的 FileSendError。"""
    try:
        return await client.send_action(action, params, timeout=timeout)
    except TimeoutError as e:
        raise FileSendError(format_no_response(action, timeout, stage=stage, size_bytes=size_bytes)) from e
    except Exception as e:
        # ConnectionClosed 不是 OSError，必须单独识别；其余异常原样上抛
        if not is_disconnect_error(e):
            raise
        raise FileSendError(format_disconnect(action, stage, e)) from e


# SnowLuma 用 /^[A-Za-z0-9_-]{1,128}$/ 校验 stream_id；NapCat 不限制。
# 原实现直接拼 basename（含 .pdf / 中文 / 空格），第一片就会被 SnowLuma 拒绝。
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _make_stream_id(file_path: str) -> str:
    """生成跨实现兼容的 stream_id，长度控制在 128 字符白名单内。"""
    stem = Path(file_path).stem
    safe = _UNSAFE_ID_CHARS.sub("_", stem)[:80] or "file"
    # 毫秒时间戳：同一秒内两个同名任务不会复用同一个 stream_id
    return f"jmdown_{int(time.time() * 1000)}_{safe}"


def _find_qq_adapter(adapter_mgr):
    """遍历已注册 adapter，返回 platform=QQ 的实例（不依赖硬编码注册名）"""
    adapters = adapter_mgr.get_adapters()
    for name, inst in adapters.items():
        if getattr(inst, "info", None) and inst.info.platform.upper() == "QQ":
            logger.info(f"Found QQ adapter: name={name!r}, platform={inst.info.platform!r}")
            return inst
    logger.warning(
        f"QQ adapter not found. Available: "
        f"{[(n, getattr(a, 'info', None) and a.info.platform) for n, a in adapters.items()]}"
    )
    return None


async def stream_upload_file(
    client,
    file_path: str,
    timeout: int = 300,
    progress_cb=None,
    chunk_size: int = 512 * 1024,
    guard: StallGuard | None = None,
) -> str:
    """upload_file_stream: 分片上传 → 返回实现端远端 temp 路径.

    client: OneBot WS 客户端实例（已有 WS 连接）
    file_path: 本地 PDF 绝对路径
    timeout: 单次 send_action 超时
    progress_cb: async callable(pct: int | None, note: str)，分片期间给真实百分比
    chunk_size: 分片大小, 默认 512KB（单片上限由 main._load_config 控制）
    guard: 无进展看门狗计时器；每次实现端确认后 touch（进度回调不可靠时也能看到进展）
    """
    file_size = os.path.getsize(file_path)
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    stream_id = _make_stream_id(file_path)
    if guard:
        guard.expect(f"分片 1/{total_chunks}", window=max(60, timeout * 2))

    logger.info(f"Stream upload start: {file_path} ({total_chunks} chunks, {_fmt(file_size)})")

    # expected_sha256 必须为完整文件 hash, 所有分片传同一个值
    # 分块读 + 丢线程, 避免一次性读入大文件并阻塞事件循环
    full_sha256 = await asyncio.to_thread(_sha256_file, file_path)
    remote_path: str | None = None
    t0 = time.time()
    report_every = max(1, total_chunks // 20)  # 约 20 次通知

    with open(file_path, "rb") as f:
        for i in range(total_chunks):
            chunk = f.read(chunk_size)
            chunk_b64 = base64.b64encode(chunk).decode()

            params = {
                "stream_id": stream_id,
                "chunk_data": chunk_b64,
                "chunk_index": i,
                "total_chunks": total_chunks,
                "file_size": file_size,
                "expected_sha256": full_sha256,
                "filename": os.path.basename(file_path),
            }

            stage = f"分片 {i + 1}/{total_chunks}"
            resp = await _call_action(client, "upload_file_stream", params, timeout, stage=stage, size_bytes=file_size)
            if not is_ok_response(resp):
                raise FileSendError(format_action_failure("upload_file_stream", resp, stage=stage))
            if guard:
                if i + 1 < total_chunks:
                    guard.expect(f"分片 {i + 2}/{total_chunks}", window=max(60, timeout * 2))
                else:
                    guard.touch()

            # 报告进度（用全程平均速度，单chunk瞬时速度未使用）
            if progress_cb and (i == 0 or i == total_chunks - 1 or i % report_every == 0):
                pct = min(int((i + 1) / total_chunks * 100), 100)
                elapsed = time.time() - t0
                avg_speed = (chunk_size * (i + 1)) / elapsed if elapsed > 0 else 0
                speed_str = _fmt(avg_speed) + "/s"
                await progress_cb(pct, speed_str)

    # ── 所有块发送完成，通知实现端组装文件 ──
    logger.info("All chunks sent, signaling completion ...")
    if guard:
        guard.expect("分片完成确认", window=max(60, timeout * 2))
    complete_resp = await _call_action(
        client,
        "upload_file_stream",
        {"stream_id": stream_id, "is_complete": True},
        timeout,
        stage="分片完成确认",
        size_bytes=file_size,
    )
    if not is_ok_response(complete_resp):
        raise FileSendError(format_action_failure("upload_file_stream", complete_resp, stage="分片完成确认"))
    remote_path = complete_resp.get("data", {}).get("file_path", "")
    if not remote_path:
        raise FileSendError(
            f"分片完成确认失败：实现端响应中缺少 file_path（{response_summary(complete_resp)}）。{diagnosis_hint()}"
        )

    return remote_path


async def send_file_via_stream(
    ctx,
    sid: str,
    user_id: str,
    file_path: str,
    is_group: bool = False,
    group_id: str | None = None,
    timeout: int = 300,
    send_timeout: int | None = None,
    progress_cb=None,
    chunk_size: int = 512 * 1024,
    guard: StallGuard | None = None,
) -> str:
    """完整链路: stream upload → upload_private_file / upload_group_file.

    返回人类可读的结果文本。
    progress_cb: async callable(pct: int | None, note: str). ``int`` 是真实分片
        百分比；``None`` 表示「实现端发送中，进度未知」。
    chunk_size: 分片大小, 默认 512KB.
    timeout: 单个分片 send_action 超时（每片 512KB，秒级即可完成）.
    send_timeout: 发送 API（upload_private_file/upload_group_file）超时。
        实现端处理大文件发送（temp 转存 → QQ 服务器）可能远超分片超时，
        若沿用 timeout 会把已成功的发送误判为超时。默认取 timeout 值。
    guard: 无进展看门狗计时器，发送阶段会换成更宽的阈值（该阶段没有中间 ack）。
    """
    # 动态查找 platform=QQ 的 adapter，不硬编码注册名
    adapter = _find_qq_adapter(ctx.adapter_mgr)
    if adapter is None:
        raise NapCatConfigError("QQ 适配器不可用，无法流传输")
    client = adapter.get_client()
    if client is None:
        raise NapCatConfigError("QQ 客户端未初始化")
    file_name = os.path.basename(file_path)

    # 1. 分片上传到实现端 temp
    remote_path = await stream_upload_file(client, file_path, timeout, progress_cb, chunk_size, guard)
    file_size = os.path.getsize(file_path)
    logger.info(f"Stream upload OK: {file_name} ({_fmt(file_size)}) → {remote_path}")

    # 2. 用实现端 temp 路径发给目标用户（发送 API 用独立的 send_timeout，
    #    避免大文件发送处理超时被误判为失败）
    #    这一阶段实现端内部转存，协议不回报进度，不能继续挂在 100%
    send_to = send_timeout if send_timeout is not None else timeout
    if guard:
        # 该阶段没有中间 ack，阈值必须覆盖实现端转存耗时，否则会误杀正在转存的大文件
        guard.expect("实现端发送", window=send_to + 60)
    if progress_cb:
        await progress_cb(None, "实现端发送中")
    if is_group and group_id:
        action = "upload_group_file"
        params = {"group_id": group_id, "file": remote_path, "name": file_name}
    else:
        action = "upload_private_file"
        params = {"user_id": user_id, "file": remote_path, "name": file_name}
    resp = await _call_action(client, action, params, send_to, stage="实现端发送", size_bytes=file_size)

    if not is_ok_response(resp):
        raise FileSendError(format_action_failure(action, resp, stage="实现端发送"))

    logger.info(f"File sent to {user_id}: {file_name}")
    return f"已通过流传输发送 ({_fmt(file_size)})"


def _sha256_file(file_path: str) -> str:
    """分块读取计算 SHA256, 避免一次性读入大文件."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _fmt(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024**2:.1f} MB"
