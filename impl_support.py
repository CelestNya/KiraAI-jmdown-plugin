"""OneBot 实现方的文件上传能力矩阵（唯一数据源）。

``schema.json`` 的开关提示、运行时错误提示、配置文档与一致性测试都从这里
取值，避免「代码改了、文案没改」。本模块不参与运行时判定，插件不会依据
实现名做分支——列表只用于给人看的提示。

核查时间 2026-11-11，依据是各项目源码/官方文档，**不是运行时探测**：

===========================  ======  ========  ========  ========
实现                         标准动作  path     base64   分片
===========================  ======  ========  ========  ========
NapCat                       ✅      ✅        ✅        ✅
SnowLuma                     ✅      ✅        ✅        ✅
LLOneBot                     ✅      ✅        ✅        ❌
Lagrange.OneBot (v1)         ✅      ✅        ❌        ❌
OpenShamrock                 ✅      ✅        ✅        ❌
go-cqhttp                    ✅      ✅        ❌        ❌
===========================  ======  ========  ========  ========

证据（GitHub 默认分支 / 指定分支）：

- NapCat ``packages/napcat-onebot/action/router.ts`` 注册三个动作；
  ``go-cqhttp/UploadPrivateFile.ts`` 用 ``uriToLocalFile`` 解析 file；
  ``stream/UploadFileStream.ts`` 定义分片参数。
- SnowLuma ``onebot/src/actions/group-file.ts`` 与 ``stream-file.ts``，注释写明
  "NapCat-compatible chunked file transfer"；``protocol/src/highway/utils.ts``
  的 ``resolveLocalFilePath`` 接受 path / base64:// / data: / http(s)://。
- LLOneBot ``onebot11/action/go-cqhttp/UploadPrivateFile.ts``（及 Group 版）
  用 ``uri2local``；``common/utils/file.ts`` 的 ``checkUriType`` 明确支持
  ``base64://``。仓库内无 ``upload_file_stream`` 实现。
- Lagrange.Core ``v1`` 分支 ``Lagrange.OneBot/Core/Operation/File/UploadPrivateFileOperation.cs``：
  ``new FileEntity(file.File)`` → ``FileStream(path)``，只接受本地路径；
  无分片动作。（master 已迁 Milky 协议，OneBot 实现在 v1 分支。）
- OpenShamrock 官方文档「文件相关」：file 为「本地文件路径或文件 base64 或文件 url」，
  ``base64://`` 开头；无分片动作。
- go-cqhttp ``coolq/api.go``：``PathExists(file)`` / ``os.Open(file)``，
  文档明写「只能上传本地文件」；已停止维护，仅作历史兼容记录。

注意：``upload_private_file`` / ``upload_group_file`` **不是 OneBot 11 核心标准
条款**，而是 go-cqhttp 引入、各家实现共同提供的扩展动作。称其为「通用上传」
指的是覆盖面广，不是协议标准地位。
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ImplSupport:
    name: str
    standard: bool  # upload_private_file / upload_group_file
    path: bool  # file 接受本地绝对路径
    base64: bool  # file 接受 base64://
    stream: bool  # upload_file_stream
    evidence: str


SUPPORTED_IMPLS: tuple[ImplSupport, ...] = (
    ImplSupport(
        "NapCat",
        True,
        True,
        True,
        True,
        "napcat-onebot/action/router.ts + go-cqhttp/UploadPrivateFile.ts + stream/UploadFileStream.ts",
    ),
    ImplSupport(
        "SnowLuma",
        True,
        True,
        True,
        True,
        "onebot/src/actions/group-file.ts + stream-file.ts (NapCat-compatible)",
    ),
    ImplSupport(
        "LLOneBot",
        True,
        True,
        True,
        False,
        "onebot11/action/go-cqhttp/UploadPrivateFile.ts + common/utils/file.ts#checkUriType",
    ),
    ImplSupport(
        "Lagrange.OneBot",
        True,
        True,
        False,
        False,
        "v1: Lagrange.OneBot/Core/Operation/File/UploadPrivateFileOperation.cs -> FileEntity(path)",
    ),
    ImplSupport(
        "OpenShamrock",
        True,
        True,
        True,
        False,
        "官方文档 api/file.html：file 为本地路径 / base64:// / URL",
    ),
    ImplSupport(
        "go-cqhttp",
        True,
        True,
        False,
        False,
        "coolq/api.go: PathExists / os.Open（已停维护）",
    ),
)


# 三种上传方案的唯一取值集合。``path`` / ``base64`` 走同一组标准动作，
# 区别只在 file 参数形态；``stream`` 是分片动作。不再提供按 WS 地址猜测的 auto。
UPLOAD_METHODS: tuple[str, ...] = ("path", "base64", "stream")
DEFAULT_UPLOAD_METHOD: str = "path"


def _names(predicate) -> str:
    return "、".join(i.name for i in SUPPORTED_IMPLS if predicate(i))


STANDARD_IMPLS: str = _names(lambda i: i.standard)
STREAM_IMPLS: str = _names(lambda i: i.stream)
BASE64_IMPLS: str = _names(lambda i: i.base64)
PATH_ONLY_IMPLS: str = _names(lambda i: i.standard and not i.base64)


def is_ok_response(resp: dict) -> bool:
    """判定 OneBot 动作响应是否成功，兼容两种信封。

    多数实现给 ``status: "ok"``；少数只给 ``retcode: 0``。只认 status 会把
    后者误判为失败（历史 bug）。带 status 时以 status 为准，否则回落 retcode。
    """
    status = str(resp.get("status", "")).lower()
    if status:
        return status == "ok"
    try:
        return int(resp.get("retcode", -1)) == 0
    except (TypeError, ValueError):
        return False


class FileSendError(RuntimeError):
    """文件上传失败。消息会直接进 ``TaskState.error`` 并推给 LLM。

    因此文本必须自包含、如实、带可操作下一步：不能是空字符串，不能把
    「实现端返回失败」臆断成「文件太大」，也不能编造并未实测的体积上限。
    """


def response_summary(resp: dict) -> str:
    """如实摘录实现端响应，不做原因推断。"""
    parts = [f"{k}={resp[k]!r}" for k in ("status", "retcode", "message", "wording") if k in resp]
    data = resp.get("data")
    if data not in (None, {}, ""):
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        parts.append(f"data={text[:200]}")
    return ", ".join(parts) or "(响应体为空)"


def diagnosis_hint() -> str:
    """失败时的可操作指引；只列可核对的事实，不预设体积上限。"""
    return (
        "可核对：① 实现端日志（NapCat 请求体超限时打印 'Max payload size exceeded'，"
        "且不会返回失败响应）；② 动作是否被实现方支持 —— 标准动作已确认 "
        f"{STANDARD_IMPLS}，分片动作 {STREAM_IMPLS}；"
        "③ 目标会话是否有发送文件权限、path 方案是否同机部署；"
        "④ 换用另一种上传方案（path / base64 / stream）重试。"
    )


def format_action_failure(action: str, resp: dict, *, stage: str) -> str:
    """实现端明确返回失败响应：原样转述，不替实现端判断原因。"""
    return (
        f"{stage}失败：实现端对 {action} 返回了失败响应（{response_summary(resp)}）。"
        "插件默认不设本地体积上限（可配 upload.max_file_mb），上限由实现端决定。"
        f"{diagnosis_hint()}"
    )


def format_no_response(action: str, timeout: float, *, stage: str, size_bytes: int | None = None) -> str:
    """等待响应超时：如实说明「没收到响应」，列可能原因但不断言是哪一种。"""
    size = f"（文件 {size_bytes / 1024 / 1024:.1f} MB）" if size_bytes is not None else ""
    return (
        f"{stage}失败：{action} 在 {timeout:g}s 内未收到实现端响应{size}。"
        "这不是实现端明确拒绝；可能原因包括请求体被实现方丢弃/超限、连接中断、"
        "或实现端转存耗时超过超时设置。插件无法从客户端区分，需查实现端日志确认。"
        f"{diagnosis_hint()}"
    )


def is_disconnect_error(e: BaseException) -> bool:
    """判定是否为「连接层」异常。

    ``websockets.exceptions.ConnectionClosed`` 的 MRO 是
    ``ConnectionClosed → WebSocketException → Exception``，**不是 OSError**，
    只 except OSError 会漏掉真正的断连，最终给 LLM 的是
    ``no close frame received or sent`` 这种无诊断价值的原文。
    这里按类型名判定，避免为了一个 except 分支硬依赖 websockets 包。
    """
    if isinstance(e, OSError):
        return True
    return any(cls.__name__.startswith("ConnectionClosed") for cls in type(e).__mro__)


def format_disconnect(action: str, resp_stage: str, err: BaseException, *, mid_chunks: bool = False) -> str:
    """连接中断：说明中断位置与被中断的动作，不臆断成超限。

    ``mid_chunks`` 只在分片逐片发送阶段为真——只有那时断连才会留下残缺分片；
    标准单次动作和「分片完成确认之后的发送阶段」都不能这么说，否则又在给
    LLM 编造传输状态。
    """
    tail = (
        "传输链路断开，不是实现端明确拒绝；分片方案下已发出的分片可能是残缺的。"
        if mid_chunks
        else ("传输链路断开，不是实现端明确拒绝。")
    )
    return f"{resp_stage}连接中断（{type(err).__name__}: {err}），{action} 未收到实现端响应。{tail}{diagnosis_hint()}"


def format_stalled(stage: str, idle: float, window: float) -> str:
    """无进展看门狗触发：说明「多久没有收到确认」，不对原因下结论。

    不能断言「不是体积超限」：部分实现（如 NapCat）对超限请求体是**静默丢弃**
    且不回包，此时「没有 ack」恰恰可能是分片帧超限造成的。
    """
    return (
        f"{stage}停滞：连续 {idle:.0f}s 没有收到任何实现端确认（无进展阈值 {window:g}s）。"
        "具体原因无法从客户端判定：可能是实现端进程卡死或掉线、上行链路中断、"
        "实现端在转存阶段卡住，**也可能是实现端静默丢弃了超出其请求体 / 帧上限的分片**"
        "（部分实现超限时不返回失败响应）。"
        f"{diagnosis_hint()}"
    )


def format_size_limit(size_bytes: int, limit_mb: int, method: str) -> str:
    """配置闸门拦截：明确说这是插件侧设置，不是 QQ / 实现端的上限。

    建议值取「向上取整的 MB」，不能四舍五入：1.2MB 的文件被 1MB 上限拦下时，
    四舍五入会得到「调到 ≥ 1 MB」，照着改依然被拦。
    """
    need_mb = -(-size_bytes // (1024 * 1024))  # ceil(size / MB)
    return (
        f"发送前拦截：文件 {size_bytes / 1024 / 1024:.1f} MB 超过插件配置的"
        f"「文件大小上限 {limit_mb} MB」（upload.max_file_mb）。"
        "这是插件侧的保护性闸门，**不是 QQ 或实现端的上限**。"
        f"要发送就把它调到 ≥ {need_mb} MB，或设为 0 取消限制；"
        f"当前方案 {method}。"
    )
