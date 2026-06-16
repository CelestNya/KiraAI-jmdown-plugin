"""
NapCat Stream API 封装

大文件分片上传，绕过 WebSocket 16MB 帧限制。
复用主 WS 连接（通过 adapter.get_client()），不开新连接。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from typing import Optional

from core.plugin import logger

CHUNK_SIZE = 512 * 1024  # 512KB — 减少 round-trip 次数, NapCat 帧上限 16MB


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
) -> str:
    """upload_file_stream: 分片上传 → 返回 NapCat 远端 temp 路径.

    client: NapCatWebSocketClient 实例（已有 WS 连接）
    file_path: 本地 PDF 绝对路径
    timeout: 单次 send_action 超时
    progress_cb: async callable(pct: int, speed_str: str) 每 25% 回调
    chunk_size: 分片大小, 默认 512KB, NapCat 帧上限 16MB
    """
    file_size = os.path.getsize(file_path)
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    stream_id = f"jmdown_{int(time.time())}_{os.path.basename(file_path)}"

    logger.info(f"Stream upload start: {file_path} ({total_chunks} chunks, {_fmt(file_size)})")

    # expected_sha256 必须为完整文件 hash, 所有分片传同一个值
    # 分块读 + 丢线程, 避免一次性读入大文件并阻塞事件循环
    full_sha256 = await asyncio.to_thread(_sha256_file, file_path)
    remote_path: Optional[str] = None
    t0 = time.time()
    report_every = max(1, total_chunks // 20)  # 约 20 次通知

    with open(file_path, "rb") as f:
        for i in range(total_chunks):
            chunk_start = time.time()
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

            resp = await client.send_action("upload_file_stream", params, timeout=timeout)
            status = resp.get("status", "")
            if status != "ok":
                raise RuntimeError(
                    f"Stream chunk {i + 1}/{total_chunks} failed: "
                    f"status={status} data={resp.get('data', {})}"
                )

            # 报告进度
            if progress_cb and (
                i == 0 or i == total_chunks - 1 or i % report_every == 0
            ):
                chunk_elapsed = time.time() - chunk_start
                inst_speed = len(chunk) / chunk_elapsed if chunk_elapsed > 0 else 0
                pct = min(int((i + 1) / total_chunks * 100), 100)
                avg_speed = (chunk_size * (i + 1)) / (time.time() - t0) if (time.time() - t0) > 0 else 0
                speed_str = _fmt(avg_speed) + f"/s"
                await progress_cb(pct, speed_str)

    # ── 所有块发送完成，通知 NapCat 组装文件 ──
    logger.info("All chunks sent, signaling completion ...")
    complete_resp = await client.send_action("upload_file_stream", {
        "stream_id": stream_id,
        "is_complete": True,
    }, timeout=timeout)
    status = complete_resp.get("status", "")
    if status != "ok":
        raise RuntimeError(
            f"Stream completion signal failed: "
            f"status={status} data={complete_resp.get('data', {})}"
        )
    remote_path = complete_resp.get("data", {}).get("file_path", "")
    if not remote_path:
        raise RuntimeError(
            f"Stream completed but no file_path in response: "
            f"{json.dumps(complete_resp, ensure_ascii=False)}"
        )

    return remote_path


async def send_file_via_stream(
    ctx,
    sid: str,
    user_id: str,
    file_path: str,
    is_group: bool = False,
    group_id: Optional[str] = None,
    timeout: int = 300,
    progress_cb=None,
    chunk_size: int = 512 * 1024,
) -> str:
    """完整链路: stream upload → upload_private_file / upload_group_file.

    返回人类可读的结果文本。
    progress_cb: async callable(pct: int) for upload progress.
    chunk_size: 分片大小, 默认 512KB.
    """
    # 动态查找 platform=QQ 的 adapter，不硬编码注册名
    adapter = _find_qq_adapter(ctx.adapter_mgr)
    if adapter is None:
        raise RuntimeError("QQ 适配器不可用，无法流传输")
    client = adapter.get_client()
    if client is None:
        raise RuntimeError("NapCat 客户端未初始化")
    file_name = os.path.basename(file_path)

    # 1. 分片上传到 NapCat temp
    remote_path = await stream_upload_file(client, file_path, timeout, progress_cb, chunk_size)
    file_size = os.path.getsize(file_path)
    logger.info(f"Stream upload OK: {file_name} ({_fmt(file_size)}) → {remote_path}")

    # 2. 从 NapCat temp 发给目标用户
    if is_group and group_id:
        resp = await client.send_action(
            "upload_group_file",
            {"group_id": group_id, "file": remote_path, "name": file_name},
            timeout=timeout,
        )
    else:
        resp = await client.send_action(
            "upload_private_file",
            {"user_id": user_id, "file": remote_path, "name": file_name},
            timeout=timeout,
        )

    status = resp.get("status", "")
    if status != "ok":
        raise RuntimeError(
            f"Send file failed: status={status} data={resp.get('data', {})}"
        )

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
    return f"{b / 1024 ** 2:.1f} MB"
