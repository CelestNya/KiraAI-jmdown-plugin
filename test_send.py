"""
测试 upload_file_stream 完整流程：分片 → is_complete → upload_private_file
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "KiraAI-src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG = Path(__file__).parent.parent / "KiraAI-src" / "data" / "config" / "system_config.json"


async def recv_matching(ws, echo_prefix: str, timeout: int = 300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=min(10, deadline - time.time()))
        msg = json.loads(raw)
        e = msg.get("echo", "")
        if e.startswith(echo_prefix):
            return msg


async def test():
    user_id = sys.argv[1] if len(sys.argv) > 1 else "2263130787"

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    qq_cfg = None
    for entry in cfg.get("adapters", {}).values():
        if entry.get("platform", "").upper() == "QQ":
            qq_cfg = entry["config"]
            break

    import websockets

    headers = {"Authorization": f"Bearer {qq_cfg['ws_token']}"} if qq_cfg.get("ws_token") else {}
    async with websockets.connect(qq_cfg["ws_uri"], additional_headers=headers, max_size=2**24) as ws:
        print("[OK] Connected")

        pdf = Path("test_output/send_big.pdf")
        if not pdf.exists() or pdf.stat().st_size < 50_000_000:
            print("Generating ~50MB test PDF ...")
            from PIL import Image
            import img2pdf
            td = pdf.parent / "big_pages"
            td.mkdir(parents=True, exist_ok=True)
            pages = []
            for i in range(1500):
                p = td / f"p{i}.jpg"
                Image.new("RGB", (1200, 1800), color=(i % 256, (i*7) % 256, (i*13) % 256)).save(p, "JPEG", quality=85)
                pages.append(str(p))
            data = img2pdf.convert(*pages, producer="t", creator="t")
            pdf.write_bytes(data)
            for p in pages:
                os.remove(p)
            td.rmdir()
            print(f"  done: {pdf.stat().st_size / 1024 / 1024:.1f} MB")
        fsize = pdf.stat().st_size
        csize = 512 * 1024
        nchunks = (fsize + csize - 1) // csize
        sid = f"fl_{int(time.time())}"

        print(f"Upload {nchunks} chunks x {csize//1024}KB, total {fsize} bytes")
        # expected_sha256 必须为完整文件的 hash, 所有分片传同一个值
        full_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

        # 1. 发所有分块
        with open(str(pdf), "rb") as f:
            for i in range(nchunks):
                chunk = f.read(csize)
                b64 = base64.b64encode(chunk).decode()
                echo = f"c{sid}_{i}"
                await ws.send(json.dumps({
                    "action": "upload_file_stream",
                    "params": {
                        "stream_id": sid, "chunk_data": b64,
                        "chunk_index": i, "total_chunks": nchunks,
                        "file_size": fsize, "expected_sha256": full_sha256,
                        "filename": "send_test.pdf",
                    },
                    "echo": echo,
                }))
                resp = await recv_matching(ws, echo, 300)
                if resp.get("status") != "ok":
                    print(f"[FAIL] Chunk {i+1}: {resp}")
                    return
        print("[OK] All chunks sent")

        # 2. is_complete 信号 → 拿到 file_path
        echo = f"done_{sid}"
        await ws.send(json.dumps({
            "action": "upload_file_stream",
            "params": {"stream_id": sid, "is_complete": True},
            "echo": echo,
        }))
        complete_resp = await recv_matching(ws, echo, 300)
        print(f"\n--- Complete response ---")
        print(json.dumps(complete_resp, indent=2, ensure_ascii=False))

        file_path = complete_resp.get("data", {}).get("file_path", "")
        print(f"\nfile_path = {file_path!r}")

        if not file_path:
            print("[FAIL] No file_path after is_complete")
            return

        # 3. 用 file_path 发文件
        print(f"\nSend to {user_id} with file={file_path!r} ...")
        await ws.send(json.dumps({
            "action": "upload_private_file",
            "params": {"user_id": user_id, "file": file_path, "name": "send_test.pdf"},
            "echo": "send_final",
        }))
        send_resp = await recv_matching(ws, "send_final", 300)
        print(f"Send response: {json.dumps(send_resp, indent=2, ensure_ascii=False)}")
        if send_resp.get("status") == "ok":
            print("[OK] File sent! Check your QQ.")
        else:
            print("[FAIL] Send failed")


if __name__ == "__main__":
    asyncio.run(test())
