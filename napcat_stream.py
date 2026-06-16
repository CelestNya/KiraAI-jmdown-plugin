"""
NapCat Stream API 封装

大文件分片上传，绕过 WebSocket 16MB 帧限制。
复用主 WS 连接（通过 adapter.get_client()），不开新连接。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from typing import Optional

from core.plugin import logger

CHUNK_SIZE = 64 * 1024  # 64KB — NapCat 帧安全大小


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


async def stream_upload_file(client, file_path: str, timeout: int = 300) -> str:
    """upload_file_stream: 分片上传 → 返回 NapCat 远端 temp 路径.

    client: NapCatWebSocketClient 实例（已有 WS 连接）
    file_path: 本地 PDF 绝对路径
    timeout: 单次 send_action 超时
    """
    file_size = os.path.getsize(file_path)
    total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    stream_id = f"jmdown_{int(time.time())}_{os.path.basename(file_path)}"

    sha256 = hashlib.sha256()
    remote_path: Optional[str] = None

    with open(file_path, "rb") as f:
        for i in range(total_chunks):
            chunk = f.read(CHUNK_SIZE)
            sha256.update(chunk)
            chunk_b64 = base64.b64encode(chunk).decode()

            params = {
                "stream_id": stream_id,
                "chunk_data": chunk_b64,
                "chunk_index": i,
                "total_chunks": total_chunks,
                "file_size": file_size,
                "expected_sha256": sha256.hexdigest(),
            }

            resp = await client.send_action("upload_file_stream", params, timeout=timeout)
            status = resp.get("status", "")
            if status != "ok":
                raise RuntimeError(
                    f"Stream chunk {i + 1}/{total_chunks} failed: "
                    f"status={status} data={resp.get('data', {})}"
                )

            # 最后一片才返回 file_path
            if i == total_chunks - 1:
                remote_path = resp.get("data", {}).get("file_path", "")

    if not remote_path:
        raise RuntimeError("Stream upload completed but no file_path in response")

    return remote_path


async def send_file_via_stream(
    ctx,
    sid: str,
    user_id: str,
    file_path: str,
    is_group: bool = False,
    group_id: Optional[str] = None,
    timeout: int = 300,
) -> str:
    """完整链路: stream upload → upload_private_file / upload_group_file.

    返回人类可读的结果文本。
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
    remote_path = await stream_upload_file(client, file_path, timeout)
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
    return f"📤 已通过流传输发送 ({_fmt(file_size)})"


def _fmt(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024**2:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024 ** 2:.1f} MB"
