"""标准文件上传（OneBot 生态通用扩展动作）。

``upload_private_file`` / ``upload_group_file`` 覆盖面最广，已确认
NapCat / SnowLuma / LLOneBot / Lagrange.OneBot / OpenShamrock / go-cqhttp
均实现，能力矩阵见 ``impl_support.py``。``file`` 参数有两种形态：

- ``path``   ：实现端所在机器上的路径，零膨胀，**仅同机可用**，所有实现都支持
- ``base64`` ：内联内容，跨机可用，体积 +33%，仅部分实现支持（见 impl_support）

第三种方案 ``stream`` 走 ``napcat_stream`` 的分片链路，只有 NapCat / SnowLuma
提供。三种方案由 ``upload_method`` 配置显式选择，不再按 WS 地址自动猜测。

注意 OneBot 11 标准本身没有文件上传动作，``upload_*_file`` 是 go-cqhttp 引入、
各家实现共同提供的扩展。

**插件不预设体积上限**：三条方案都把文件原样交给实现端，由实现端的真实上限
决定成败。失败时抛 ``FileSendError``，文本如实转述「实现端返回的失败响应」或
「等待响应超时」，并附诊断指引；不臆断原因、不编造上限、不留空字符串。

标准动作是一次 ``send_action``，实现端内部转存不回报中间状态，OneBot 也没有
进度事件，因此本模块不产生百分比：只用 ``progress_cb(None, ...)`` 告知
「进度未知」，避免把本地编码进度伪装成上传进度。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from .impl_support import (
    FileSendError,
    diagnosis_hint,
    format_action_failure,
    format_no_response,
    is_ok_response,
)
from .napcat_stream import _find_qq_adapter, _fmt

STANDARD_PAYLOAD_MODES = frozenset({"path", "base64"})


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
    payload_mode: str = "path",
) -> str:
    """用 OneBot 通用动作发送文件，返回人类可读结果。

    :param payload_mode: ``path`` / ``base64``，见模块注释
    :param timeout: 单次 ``send_action`` 超时；大文件转存可能远超分片耗时
    :param progress_cb: ``async callable(pct: int | None, note: str)``；
        标准链路只能传 ``None``（进度未知）
    """
    if payload_mode not in STANDARD_PAYLOAD_MODES:
        raise FileSendError(f"未知的标准上传载荷 {payload_mode!r}，只支持 path / base64")

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

    if payload_mode == "base64":
        file_value = "base64://" + await asyncio.to_thread(_read_base64, str(path))
    else:
        file_value = str(path.resolve())

    if is_group and group_id:
        action, params = "upload_group_file", {"group_id": str(group_id)}
    else:
        action, params = "upload_private_file", {"user_id": str(user_id)}
    params.update({"file": file_value, "name": file_name})

    # 单次动作无中间状态，明确报告「进度未知」而不是伪造 0% / 100%
    if progress_cb:
        await progress_cb(None, "等待实现端响应")

    try:
        resp = await client.send_action(action, params, timeout=timeout)
    except TimeoutError as e:
        raise FileSendError(format_no_response(action, timeout, stage="标准上传", size_bytes=size)) from e
    except OSError as e:
        raise FileSendError(
            f"标准上传连接异常（{type(e).__name__}: {e}），{action} 未收到实现端响应。{diagnosis_hint()}"
        ) from e

    if not is_ok_response(resp):
        raise FileSendError(format_action_failure(action, resp, stage="标准上传"))

    return f"已通过标准上传发送 ({_fmt(size)}, 载荷={payload_mode})"
