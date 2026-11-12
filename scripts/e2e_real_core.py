"""真实 KiraAI core 联动 e2e（本地手动运行，不进 CI）。

``tests/test_upload_e2e.py`` 用精简客户端验证「插件发出的 OneBot 帧」；本脚本
把精简客户端换成 KiraAI 真正的 ``NapCatWebSocketClient``，验证插件与宿主
客户端的接合：正向 WS 连接、Bearer 鉴权、get_login_info 校验、lifecycle 登录
闸门、echo/Future 响应匹配，以及 path / base64 / stream 三种上传方案。

仍然不需要 NapCat 或 QQ 在线 —— 对端是 ``tests/_onebot_server.py`` 的假实现。

运行（必须从 KiraAI 根目录，或让脚本自行上溯定位）：

    ./venv/Scripts/python.exe data/plugins/jmdown/scripts/e2e_real_core.py

也可显式指定：``KIRAAI_ROOT=/path/to/KiraAI python scripts/e2e_real_core.py``
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def find_kiraai_root() -> Path:
    env = os.environ.get("KIRAAI_ROOT")
    if env:
        candidate = Path(env).resolve()
        if (candidate / "core/plugin/manager.py").is_file():
            return candidate
    for candidate in (Path.cwd(), *Path.cwd().parents, PLUGIN_ROOT, *PLUGIN_ROOT.parents):
        if (candidate / "core/plugin/manager.py").is_file():
            return candidate
    raise SystemExit("找不到 KiraAI 根目录（缺少 core/plugin/manager.py），请设置 KIRAAI_ROOT")


def bootstrap(root: Path) -> None:
    """对齐 KiraAI 的插件加载环境。

    ``core.logging_manager`` 一被 import 就按 CWD 打开 ``data/log.log``，所以
    必须先 chdir 到 KiraAI 根，否则会在插件目录下凭空建一个 data/。
    """
    os.chdir(root)
    for entry in (root, root / "data" / "plugins", PLUGIN_ROOT / "tests"):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)

    pkg = types.ModuleType("plugins")
    pkg.__path__ = [str(root / "data" / "plugins")]
    sys.modules["plugins"] = pkg
    sub = types.ModuleType("plugins.jmdown")
    sub.__path__ = [str(PLUGIN_ROOT)]
    sys.modules["plugins.jmdown"] = sub


class Adapter:
    def __init__(self, client) -> None:
        self.info = SimpleNamespace(platform="QQ")
        self._client = client

    def get_client(self):
        return self._client


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"[PASS] {name}")
        else:
            self.failed += 1
            print(f"[FAIL] {name} {detail}")


async def run_e2e() -> int:
    root = find_kiraai_root()
    bootstrap(root)

    from _onebot_server import SAFE_STREAM_ID, FakeOneBotServer
    from core.adapter.src.qq.napcat_client.client import NapCatWebSocketClient
    from core.plugin import registry
    from jmdown.impl_support import STANDARD_IMPLS
    from jmdown.main import JMdownPlugin

    checks = Checks()
    print(f"KiraAI 根：{root}")
    print(f"插件根：{PLUGIN_ROOT}")
    print(f"支持矩阵：{STANDARD_IMPLS}\n")

    # 真实 core 的注册表能识别插件归属
    pid = registry.get_obj_plugin_id(JMdownPlugin)
    comp = registry._plugin_components.get(pid)
    tools = sorted(comp.tool_funcs) if comp else []
    checks.check("registry 解析 plugin_id=jmdown", pid == "jmdown", f"实际 {pid}")
    checks.check("4 个工具已注册", len(tools) == 4, f"实际 {tools}")

    server = FakeOneBotServer()
    url = await server.start()
    client = NapCatWebSocketClient(url, server.token)
    run_task = asyncio.create_task(client.run(str(server.self_id), url, server.token))

    try:
        await asyncio.wait_for(client.login_success_event.wait(), timeout=5)
        checks.check("lifecycle 登录闸门放行", client.login_success_event.is_set())
        login = await client.send_action("get_login_info", {}, timeout=3)
        checks.check(
            "get_login_info 经 echo 回包",
            login.get("data", {}).get("user_id") == server.self_id,
            str(login),
        )
        checks.check("Bearer 鉴权通过", server.bad_auth == [], str(server.bad_auth))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "真实 core 联动.pdf"
            content = bytes(range(256)) * 32 + b"REAL-CORE-E2E"
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()

            ctx = SimpleNamespace(
                adapter_mgr=SimpleNamespace(get_adapters=lambda: {"qq": Adapter(client)}),
                publish_notice=AsyncMock(),
            )

            def plugin(**overrides):
                p = JMdownPlugin(ctx, {})
                p._upload_timeout = 300
                p._chunk_size = 1024
                p._upload_method = "path"
                for key, value in overrides.items():
                    setattr(p, key, value)
                return p

            async def progress(_pct: int | None, _note: str) -> None:
                return None

            # 1) 标准路线：默认 path 直传
            server.requests.clear()
            result = await plugin()._deliver("qq:dm:123456", "123456", False, None, str(path), progress)
            checks.check(
                "标准 path：动作/载荷/字节正确",
                server.actions() == ["upload_private_file"]
                and server.delivered[0].mode == "path"
                and server.delivered[0].content == content,
                f"actions={server.actions()} delivered={len(server.delivered)}",
            )
            checks.check("标准 path：结果文本", "载荷=path" in result, result)

            # 2) 标准路线：base64 跨机形态
            server.requests.clear()
            server.delivered.clear()
            result = await plugin(_upload_method="base64")._deliver(
                "qq:dm:123456", "123456", False, None, str(path), progress
            )
            checks.check(
                "标准 base64：服务端解码后字节一致",
                server.delivered and server.delivered[0].content == content,
                f"delivered={len(server.delivered)}",
            )

            # 3) 分片路线：真实客户端逐片发帧、服务端重组
            server.requests.clear()
            server.delivered.clear()
            server.chunk_requests.clear()
            result = await plugin(_upload_method="stream")._deliver(
                "qq:dm:123456", "123456", False, None, str(path), progress
            )
            chunks = [r for r in server.chunk_requests if not r.get("is_complete")]
            total = (len(content) + 1023) // 1024
            stream_ids = {r["stream_id"] for r in server.chunk_requests}
            checks.check(
                "分片：片数/完成帧/最终发送",
                len(chunks) == total
                and server.actions().count("upload_file_stream") == total + 1
                and server.actions()[-1] == "upload_private_file",
                f"chunks={len(chunks)} expected={total} actions={server.actions()[-3:]}",
            )
            checks.check(
                "分片：SHA256 与重组内容一致",
                bool(stream_ids)
                and all(SAFE_STREAM_ID.match(s) for s in stream_ids)
                and chunks[0]["expected_sha256"] == digest
                and server.delivered[0].content == content,
                f"stream_ids={stream_ids}",
            )
            checks.check("分片：结果文本", "流传输" in result, result)
    finally:
        await client.close()
        try:
            await asyncio.wait_for(run_task, timeout=3)
        except (TimeoutError, asyncio.CancelledError):
            run_task.cancel()
        await server.stop()
        # 让 logging_manager 的后台线程/句柄安静退出
        await asyncio.sleep(0)

    print(f"\n结果：{checks.passed} 通过 / {checks.failed} 失败")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    # 时间只用于打印，不参与任何协议判定
    print(f"e2e 开始 {time.strftime('%H:%M:%S')}")
    sys.exit(asyncio.run(run_e2e()))
