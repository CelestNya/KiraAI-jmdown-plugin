"""标准文件上传（OneBot 11 通用动作）。

与 ``napcat_stream`` 的分工：

- 本模块只用 ``upload_private_file`` / ``upload_group_file`` —— OneBot 11
  生态里 NapCat / LLOneBot / Lagrange 共同实现的动作，不含 ``upload_file_stream``
  这类 NapCat 专有分片接口。
- ``napcat_stream`` 保留专有分片链路，仅在配置显式开启时使用。

``file`` 参数的三种形态决定了可用性边界：

- ``path``   ：NapCat 所在机器上的路径，零膨胀，**仅同机可用**
- ``base64`` ：内联内容，跨机可用，体积 +33%，受实现方请求体上限约束
- ``auto``   ：按 WS 地址判定，回环地址视为同机

注意 OneBot 11 标准本身没有文件上传动作，``upload_*_file`` 是各家实现的共同
扩展；遇到不支持的实现时本模块会抛出带指引的 ``FileSendError``，而不是静默失败。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from urllib.parse import urlparse

from .napcat_stream import _find_qq_adapter, _fmt

# 回环地址：客户端连的是本机 NapCat，路径直传有效
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "[::1]"})

# base64 内联的默认上限。NapCat WS 接收帧上限 16MB，base64 膨胀 4/3，
# 加上 JSON 信封开销后按 10MB 原始体积卡住，留足余量。
DEFAULT_BASE64_LIMIT = 10 * 1024 * 1024

_PAYLOAD_MODES = ("auto", "path", "base64")


class FileSendError(RuntimeError):
    """标准上传失败（含配置不可用、体积超限、实现不支持）"""


def resolve_payload_mode(mode: str, ws_url: str | None) -> str:
    """把配置值解析成实际载荷形态 ``path`` / ``base64``。

    ``auto`` 按 NapCat WS 地址判定：回环地址说明同机，可直接给路径；
    地址未知或为远端时一律退回 base64 —— 跨机给路径只会得到
    「文件不存在」这类难以定位的远端报错。
    """
    if mode in ("path", "base64"):
        return mode
    host = (urlparse(ws_url or "").hostname or "").lower()
    return "path" if host in _LOOPBACK_HOSTS else "base64"


def _read_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


async def send_file_standard(
    ctx,
    user_id: str,
    file_path: str,
    *,
    is_group: bool = False,
    group_id: str | None = None,
    timeout: float = 300,
    progress_cb=None,
    payload_mode: str = "auto",
    max_bytes: int | None = None,
) -> str:
    """用 OneBot 通用动作发送文件，返回人类可读结果。

    :param payload_mode: ``auto`` / ``path`` / ``base64``，见模块注释
    :param max_bytes: base64 内联体积上限，``None`` 取 ``DEFAULT_BASE64_LIMIT``
    :param timeout: 单次 ``send_action`` 超时；大文件转存可能远超分片耗时
    :param progress_cb: ``async callable(pct: int, note: str)``
    """
    adapter = _find_qq_adapter(ctx.adapter_mgr)
    if adapter is None:
        raise FileSendError("QQ 适配器不可用，无法发送文件")

    client = adapter.get_client()
    if client is None:
        raise FileSendError("客户端未初始化，无法发送文件")

    path = Path(file_path)
    if not path.is_file():
        raise FileSendError(f"待发送文件不存在: {file_path}")
    size = path.stat().st_size
    file_name = path.name

    mode = resolve_payload_mode(payload_mode, getattr(client, "ws_url", ""))
    if mode == "base64":
        limit = DEFAULT_BASE64_LIMIT if max_bytes is None else max_bytes
        if size > limit:
            raise FileSendError(
                f"文件 {_fmt(size)} 超过内联上限 {_fmt(limit)}：base64 载荷会撞上实现方请求体上限。"
                "可选：把 NapCat 与本机部署在同一台机器（改用路径直传）、开启「使用 NapCat 分片上传」、"
                "或降低 PDF 质量 / 分卷后重试"
            )
        if progress_cb:
            await progress_cb(0, "编码")
        file_value = "base64://" + await asyncio.to_thread(_read_base64, str(path))
    else:
        file_value = str(path.resolve())

    if is_group and group_id:
        action, params = "upload_group_file", {"group_id": str(group_id)}
    else:
        action, params = "upload_private_file", {"user_id": str(user_id)}
    params.update({"file": file_value, "name": file_name})

    resp = await client.send_action(action, params, timeout=timeout)
    status = str(resp.get("status", ""))
    if status != "ok":
        raise FileSendError(
            f"标准上传失败: action={action} status={status} data={resp.get('data', {})}。"
            "若实现方不支持该动作，请开启「使用 NapCat 分片上传」"
        )

    if progress_cb:
        await progress_cb(100, _fmt(size))
    return f"已通过标准上传发送 ({_fmt(size)}, 载荷={mode})"
