"""实测 OneBot 实现方的文件上传大小上限（需在线环境，默认不发送任何文件）。

插件三条上传方案均不设本地体积上限，由实现端决定成败；本脚本只用于实测 /
排障 / 容量规划，结果不会写回配置。2026-11-12 实测 NapCat.Core 4.18.14：
``base64`` 私聊文件 37 MB 成功、38 MB 触发 ``Max payload size exceeded``，
与 ``maxPayload=50MiB`` 及 4/3 膨胀一致。
本脚本按阶梯（可选二分）逐个发送真实文件，找出 ``path`` / ``base64`` 两种载荷
各自的最大成功体积。

安全：默认 dry-run（起本地假服务端，不连 QQ、不发文件）。要对真实实现探测，
必须同时给 ``--execute`` 和 ``--user`` / ``--group``。**每次成功都会真的往该
会话发一个文件**，请先用小号/测试群。

    # 自检脚本本身（离线）
    python scripts/probe_upload_limit.py --dry-run

    # 真实探测（会发文件！）
    python scripts/probe_upload_limit.py --execute --user 123456789 \
        --url ws://127.0.0.1:3001 --token <token>

    # 群聊 + 精确二分
    python scripts/probe_upload_limit.py --execute --group 888 --binary
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
for _entry in (PLUGIN_ROOT, PLUGIN_ROOT / "tests"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from _onebot_server import FakeOneBotServer, MiniOneBotClient  # noqa: E402

from impl_support import is_ok_response  # noqa: E402

MB = 1024 * 1024
DEFAULT_STEPS = (1, 4, 8, 10, 16, 24, 32, 64)
DRY_RUN_STEPS = (1, 2, 4)


def make_sparse_file(path: Path, size: int) -> None:
    """造指定大小的文件；truncate 不实际写盘，避免探测本身变成 IO 压力。"""
    with open(path, "wb") as f:
        f.truncate(size)


async def probe_one(
    client,
    *,
    payload: str,
    size_mb: int,
    user: str | None,
    group: str | None,
    timeout: float,
    tmp_dir: Path,
) -> tuple[bool, dict]:
    path = tmp_dir / f"probe_{payload}_{size_mb}mb.bin"
    make_sparse_file(path, size_mb * MB)

    if payload == "base64":
        with open(path, "rb") as f:
            file_value = "base64://" + base64.b64encode(f.read()).decode()
    else:
        file_value = str(path.resolve())

    if group:
        action, params = "upload_group_file", {"group_id": group}
    else:
        action, params = "upload_private_file", {"user_id": user or ""}
    params.update({"file": file_value, "name": f"jmdown-probe-{size_mb}mb.bin"})

    try:
        resp = await client.send_action(action, params, timeout=timeout)
        return is_ok_response(resp), resp
    except Exception as e:  # 超时/断连也算该档位失败
        detail = str(e).strip() or "实现端未返回响应（查实现端日志是否有 Max payload size exceeded）"
        return False, {"error": f"{type(e).__name__}: {detail}"}
    finally:
        path.unlink(missing_ok=True)


def failure_reason(resp: dict) -> str:
    if "error" in resp:
        return resp["error"]
    return (
        f"status={resp.get('status', '')} retcode={resp.get('retcode', '')} "
        f"message={resp.get('message') or resp.get('wording') or ''}"
    )[:180]


async def scan(client, payload: str, steps: tuple[int, ...], args) -> dict:
    last_ok = 0
    failure = None
    rows = []

    for size in steps:
        ok, resp = await probe_one(
            client,
            payload=payload,
            size_mb=size,
            user=args.user,
            group=args.group,
            timeout=args.timeout,
            tmp_dir=Path(args.tmp_dir),
        )
        rows.append({"size_mb": size, "ok": ok, "reason": "" if ok else failure_reason(resp)})
        print(f"  [{payload:6}] {size:>4} MB -> {'OK' if ok else 'FAIL'} {'' if ok else rows[-1]['reason']}")
        if ok:
            last_ok = size
        else:
            failure = size
            break

    if args.binary and failure is not None and last_ok and failure - last_ok > 1:
        lo, hi = last_ok, failure
        while hi - lo > 1:
            mid = (lo + hi) // 2
            ok, resp = await probe_one(
                client,
                payload=payload,
                size_mb=mid,
                user=args.user,
                group=args.group,
                timeout=args.timeout,
                tmp_dir=Path(args.tmp_dir),
            )
            print(f"  [{payload:6}] {mid:>4} MB -> {'OK' if ok else 'FAIL'} (bisect)")
            if ok:
                lo = mid
            else:
                hi = mid
        last_ok, failure = lo, hi

    return {
        "payload": payload,
        "max_ok_mb": last_ok,
        "first_fail_mb": failure,
        "rows": rows,
    }


async def main_async(args) -> int:
    server = None
    client = None
    try:
        if args.dry_run:
            server = FakeOneBotServer(token="")
            url = await server.start()
            client = await MiniOneBotClient(url, token="").connect()
            print("模式：dry-run（本地假服务端，不发送真实文件）")
            steps = DRY_RUN_STEPS
        else:
            if not (args.user or args.group):
                raise SystemExit("真实探测需要 --user 或 --group 指定接收方")
            if not args.execute:
                raise SystemExit("真实探测需要显式加 --execute（会真的发文件）")
            client = await MiniOneBotClient(args.url, token=args.token).connect()
            print(
                f"模式：真实探测 -> {args.url}（接收方 {'群 ' + args.group if args.group else '私聊 ' + (args.user or '')}）"
            )
            steps = tuple(args.steps) if args.steps else DEFAULT_STEPS
            if args.max_mb:
                steps = tuple(s for s in steps if s <= args.max_mb)

        results = []
        for payload in args.payload:
            print(f"\n载荷形态：{payload}")
            results.append(await scan(client, payload, steps, args))

        base64_ok = next((r["max_ok_mb"] for r in results if r["payload"] == "base64"), None)
        print("\n" + "=" * 64)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if base64_ok is not None and not args.dry_run:
            print(f"\n实测结论：base64 最大成功 {base64_ok} MB；插件不据此设置本地闸门，该值仅作排障与容量规划参考。")
        if args.dry_run:
            print("\n自检完成：脚本链路可用；假服务端没有体积上限，不代表真实上限。")
        return 0
    finally:
        if client is not None:
            await client.close()
        if server is not None:
            await server.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="实测 OneBot 文件上传大小上限")
    parser.add_argument("--dry-run", action="store_true", help="离线自检（默认行为，不连 QQ）")
    parser.add_argument("--execute", action="store_true", help="允许向真实会话发送探测文件")
    parser.add_argument("--url", default=os.environ.get("ONEBOT_WS_URL", "ws://127.0.0.1:3001"))
    parser.add_argument("--token", default=os.environ.get("ONEBOT_TOKEN", ""))
    parser.add_argument("--user", help="私聊接收 QQ 号")
    parser.add_argument("--group", help="群号（与 --user 二选一）")
    parser.add_argument("--payload", nargs="+", choices=("path", "base64"), default=["path", "base64"])
    parser.add_argument("--steps", nargs="+", type=int, help="自定义阶梯 MB 序列")
    parser.add_argument("--max-mb", type=int, help="阶梯上限（过滤掉更大的档位）")
    parser.add_argument("--binary", action="store_true", help="首个失败点后二分精确到 1MB")
    parser.add_argument("--timeout", type=float, default=300.0, help="单次动作超时秒数")
    parser.add_argument("--tmp-dir", default=tempfile.gettempdir(), help="临时文件目录")
    return parser.parse_args()


if __name__ == "__main__":
    cfg = parse_args()
    if not cfg.dry_run and not cfg.execute:
        cfg.dry_run = True  # 没给 --execute 一律按离线自检处理
    raise SystemExit(asyncio.run(main_async(cfg)))
