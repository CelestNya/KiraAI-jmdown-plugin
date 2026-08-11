"""jmdown 插件本地集成冒烟测试（真实 KiraAI 环境 + 真实 jmcomic 网络）。

在 KiraAI-src 的 uv 环境下运行：
    cd ../KiraAI-src && uv run python ../KiraAI-jmdown-plugin/scripts/local_smoke_test.py

设计约束：
- 数据目录用临时目录，不碰生产 data/plugin_data/jmdown
- 配置内容取自真实 data/config/plugins/jmdown.json（只读）
- 不读取任何色情内容：
  * content_query=false → search/query 工具走拦截路径
  * 网络查询只用不存在的 album_id（返回"本子不存在"）
  * 校验类用例（非法参数/跨会话/并发）不触网
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
KIRA_SRC = ROOT.parent / "KiraAI-src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(KIRA_SRC))

# 插件目录名与包名不一致（KiraAI-jmdown-plugin → jmdown），手动注册包路径
_pkg = types.ModuleType("jmdown")
_pkg.__path__ = [str(ROOT)]
sys.modules["jmdown"] = _pkg

# 运行于 KiraAI-src 环境：真实 core + 真实 jmcomic
from jmdown.main import JMdownPlugin  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def make_ctx(data_dir: Path):
    """轻量 ctx：真实数据目录（临时）+ mock 的框架接口。"""
    ctx = MagicMock()
    ctx.get_plugin_data_dir.return_value = data_dir
    ctx.plugin_mgr.get_plugin_id_for_module.return_value = None  # 未注册 → 跳过工具隐藏
    ctx.publish_notice = AsyncMock()
    ctx.adapter_mgr.get_adapters.return_value = {}
    return ctx


def load_real_cfg() -> dict:
    """读取生产插件配置（只读）。"""
    cfg_path = KIRA_SRC / "data" / "config" / "plugins" / "jmdown.json"
    with cfg_path.open(encoding="utf-8") as f:
        return json.load(f)


async def main() -> int:
    global PASS, FAIL
    print("=== jmdown 本地集成冒烟测试 ===")
    cfg = load_real_cfg()
    check("读取到真实 jmdown.json", bool(cfg), str(list(cfg)[:5]))

    with tempfile.TemporaryDirectory(prefix="jmdown_smoke_") as td:
        data_dir = Path(td)
        ctx = make_ctx(data_dir)
        plugin: JMdownPlugin = JMdownPlugin(ctx, cfg)
        await plugin.initialize()
        check("initialize 成功（真实配置 + 临时目录）", True)
        check(
            "section 嵌套配置读取正确",
            plugin._max_cache == 10 and plugin._download_threads == 45,
            f"max_cache={plugin._max_cache}, threads={plugin._download_threads}",
        )
        check(
            "section 嵌套优先（max_concurrent=2 嵌套值，非平铺残留 3）",
            plugin._max_concurrent == 2,
            f"max_concurrent={plugin._max_concurrent}",
        )
        check("content_query=false（拦截路径）", plugin._content_query is False)
        check("数据目录在临时路径", str(data_dir) in str(plugin._data_dir))

        ev = MagicMock()
        ev.session.sid = "qq:dm:1"

        # ── 1. 校验类（不触网）──
        r = await plugin.send_jm_album(ev, 0, "qq:dm:1")
        check("album_id=0 拒绝", "正整数" in r, r)
        r = await plugin.send_jm_album(ev, 7, "bad-target")
        check("target 格式错误拒绝", "格式错误" in r, r)
        r = await plugin.send_jm_album(ev, 7, "qq:xx:1")
        check("未知会话类型拒绝", "未知会话类型" in r, r)
        ev2 = MagicMock()
        ev2.session.sid = "qq:dm:999"
        r = await plugin.send_jm_album(ev2, 7, "qq:dm:1")
        check("跨会话转发拦截", "不允许转发" in r, r)
        r = await plugin.query_jm_task(ev, "JOB-nonexistent")
        check("query_jm_task 未知 job", "未找到任务" in r, r)

        # ── 2. content_query=false → search/query 拦截（不读取任何本子内容）──
        r = await plugin.search_jm_album(ev, keyword="测试")
        check("search 被拦截", "已关闭" in r, r)
        r = await plugin.query_jm_album(ev, 424022)
        check("query 被拦截（不读取元信息）", "已关闭" in r, r)

        # ── 3. 真实网络：不存在的 album_id（不返回任何本子内容）──
        try:
            r = await asyncio.wait_for(
                plugin.send_jm_album(ev, 999999999999, "qq:dm:1"), timeout=30
            )
            check("不存在的 album_id 预查拒绝", "不存在" in r, r)
        except TimeoutError:
            check("不存在的 album_id 预查拒绝", False, "网络超时(30s)")

        # ── 4. 清理 ──
        await plugin.terminate()
        check("terminate 干净退出", True)

    print(f"\n结果: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
