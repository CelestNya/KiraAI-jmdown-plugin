"""JMdown main.py 核心逻辑测试。

core 框架由 ``tests/_core_stub.py`` 注入，使 main.py 可在无 KiraAI 运行时
环境下导入，从而隔离测试插件自身逻辑（输入校验、去重、并发上限、跨会话拦截）。

Run:
    pytest tests/test_main.py -v
"""

# jmdown 包与 core.* 由 tests/_core_stub.py 注入，pyright 静态无法解析
# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmdown.cache import CacheEntry
from jmdown.main import (
    JMDownError,
    JMdownPlugin,
    TaskState,
    _fmt,
    _generate_password,
    _parse_target,
)


def make_plugin() -> JMdownPlugin:
    """构造 plugin 实例，手动设置 send_jm_album 依赖的字段（绕过 initialize）。"""
    p = JMdownPlugin(MagicMock(), {})
    p._max_concurrent = 2
    p._upload_timeout = 300
    p._max_file_mb = 0  # 默认不限制，与 schema 一致
    p._allow_cross_session = False
    p._download_threads = 45
    p._notify_llm = True
    return p


def event_with_sid(sid: str = "qq:dm:1") -> MagicMock:
    ev = MagicMock()
    ev.session.sid = sid
    return ev


# ── 纯函数 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("qq:dm:123456", ("123456", False, None)),
        ("qq:gm:789", ("789", True, "789")),
    ],
    ids=["dm", "gm"],
)
def test_parse_target(target: str, expected: tuple[str, bool, str | None]) -> None:
    assert _parse_target(target) == expected


@pytest.mark.parametrize("target", ["qq:dm", "qq:xx:123"], ids=["missing_parts", "unknown_type"])
def test_parse_target_rejects_bad_input(target: str) -> None:
    with pytest.raises(JMDownError):
        _parse_target(target)


def test_password_custom_passthrough() -> None:
    assert _generate_password("mypass") == "mypass"


def test_password_random_length_16() -> None:
    assert len(_generate_password("")) == 16


def test_password_random_unique() -> None:
    assert _generate_password("") != _generate_password("")


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
    ids=["zero", "bytes", "kb", "mb"],
)
def test_fmt(size: int, expected: str) -> None:
    assert _fmt(size) == expected


# ── send_jm_album 输入校验（不触发真实下载/预查） ─────────────────────


async def test_invalid_album_id() -> None:
    assert "正整数" in await make_plugin().send_jm_album(MagicMock(), 0, "qq:dm:1")


async def test_orphan_blocked() -> None:
    p = make_plugin()
    p._orphan_aids.add(42)
    assert "清理" in await p.send_jm_album(MagicMock(), 42, "qq:dm:1")


async def test_concurrent_limit() -> None:
    p = make_plugin()
    p._running_tasks = {1: MagicMock(done=lambda: False), 2: MagicMock(done=lambda: False)}
    assert "过多" in await p.send_jm_album(MagicMock(), 3, "qq:dm:1")


async def test_bad_target() -> None:
    assert "错误" in await make_plugin().send_jm_album(MagicMock(), 7, "qq-xx-1")


async def test_cross_session_blocked() -> None:
    p = make_plugin()
    p._allow_cross_session = False
    assert "不允许转发" in await p.send_jm_album(event_with_sid("qq:dm:999"), 7, "qq:dm:111")


async def test_cross_session_allowed() -> None:
    p = make_plugin()
    p._allow_cross_session = True
    with (
        patch("jmdown.main._fetch_album_meta", return_value={}),
        patch.object(JMdownPlugin, "_task_runner", new=AsyncMock()),
    ):
        ret = await p.send_jm_album(event_with_sid("qq:dm:999"), 7, "qq:dm:111")
    assert "队列" in ret
    assert len(p._running_tasks) == 1


async def test_dedup_running() -> None:
    p = make_plugin()
    p._task_registry["JOB-x"] = TaskState(job_id="JOB-x", album_id=8, target="qq:dm:1")
    p._running_tasks[8] = MagicMock(done=lambda: False)
    ret = await p.send_jm_album(MagicMock(), 8, "qq:dm:1")
    assert "队列中" in ret
    assert "JOB-x" in ret


async def test_dead_task_superseded() -> None:
    """最近 upload_timeout+120s 内毫无进展的运行中任务视为死任务，cancel 后允许重新提交。"""
    p = make_plugin()
    state = TaskState(job_id="JOB-old", album_id=9, target="qq:dm:1")
    state.started_at = time.time() - (p._upload_timeout + 200)
    state.progress_at = state.started_at  # 全程没有进展
    p._task_registry["JOB-old"] = state
    old_task = MagicMock(done=lambda: False)
    p._running_tasks[9] = old_task
    with (
        patch("jmdown.main._fetch_album_meta", return_value={}),
        patch.object(JMdownPlugin, "_task_runner", new=AsyncMock()),
    ):
        # 同会话，避免跨会话拦截
        ret = await p.send_jm_album(event_with_sid("qq:dm:1"), 9, "qq:dm:1")
    assert "队列" in ret
    old_task.cancel.assert_called_once()


async def test_long_running_but_progressing_task_kept() -> None:
    """总时长超阈值但仍在推进（如大文件 stream 上传）的任务不能被当死任务杀掉。"""
    p = make_plugin()
    state = TaskState(job_id="JOB-live", album_id=10, target="qq:dm:1")
    state.started_at = time.time() - (p._upload_timeout + 600)
    state.progress_at = time.time()  # 刚刚还有进度
    p._task_registry["JOB-live"] = state
    live_task = MagicMock(done=lambda: False)
    p._running_tasks[10] = live_task
    ret = await p.send_jm_album(event_with_sid("qq:dm:1"), 10, "qq:dm:1")
    assert "队列中" in ret
    live_task.cancel.assert_not_called()


# ── B2: 临时 ZIP 清理 ─────────────────────────────────────────────────


def test_cleanup_zip_on_success(tmp_path: Path) -> None:
    p = make_plugin()
    p._cache_dir = tmp_path
    zip_path = tmp_path / "9.zip"
    zip_path.write_bytes(b"fake")
    assert zip_path.exists()
    p._cleanup_zip(9)
    assert not zip_path.exists()


def test_cleanup_zip_missing_is_noop(tmp_path: Path) -> None:
    p = make_plugin()
    p._cache_dir = tmp_path
    p._cleanup_zip(123)  # 不存在不应抛异常


def test_cleanup_zip_none_cache_dir_is_noop() -> None:
    """_cache_dir 未初始化（None）时不应抛异常。"""
    p = make_plugin()
    p._cache_dir = None
    p._cleanup_zip(1)  # 不应抛 TypeError


# ── B1: query / search 走 to_thread 调用同步 IO ───────────────────────


async def test_query_jm_album_calls_meta() -> None:
    p = make_plugin()
    p._content_query = True
    meta = {"album_id": 7, "title": "MyTitle", "description": ""}
    with patch("jmdown.main._fetch_album_meta", return_value=meta) as m:
        ret = await p.query_jm_album(MagicMock(), 7)
    m.assert_called_once_with(7)
    assert "MyTitle" in ret


async def test_query_jm_album_blocked_when_off() -> None:
    p = make_plugin()
    p._content_query = False
    with patch("jmdown.main._fetch_album_meta") as m:
        ret = await p.query_jm_album(MagicMock(), 7)
    m.assert_not_called()
    assert "关闭" in ret


async def test_search_jm_album_calls_search() -> None:
    p = make_plugin()
    p._content_query = True
    results = [(7, "TitleSeven", ["tag1"])]
    with patch("jmdown.main._search_albums", return_value=(1, 1, results)) as m:
        ret = await p.search_jm_album(MagicMock(), keyword="k")
    assert m.called
    assert "TitleSeven" in ret


async def test_search_requires_at_least_one_field() -> None:
    p = make_plugin()
    p._content_query = True
    assert "错误" in await p.search_jm_album(MagicMock())


# ── B3: 后台 task 引用与 terminate ────────────────────────────────────


async def test_spawn_holds_and_releases() -> None:
    p = make_plugin()

    async def short() -> None:
        await asyncio.sleep(0.01)

    t = p._spawn_bg_task(short())
    assert t in p._bg_tasks
    await t
    # done_callback 应已将其从集合移除
    assert t not in p._bg_tasks


async def test_terminate_cancels_bg_tasks() -> None:
    p = make_plugin()

    async def long() -> None:
        await asyncio.sleep(100)

    t = p._spawn_bg_task(long())
    assert t in p._bg_tasks
    await p.terminate()
    assert t.done()
    assert len(p._bg_tasks) == 0


# ── M5: _cleanup_task 自我保护 ────────────────────────────────────────


async def test_cleanup_task_pops_self() -> None:
    p = make_plugin()
    state = TaskState(job_id="JOB-x", album_id=5, target="qq:dm:1")
    p._running_tasks[5] = asyncio.current_task()
    p._cleanup_task(state)
    assert 5 not in p._running_tasks


async def test_cleanup_task_skips_when_not_current() -> None:
    p = make_plugin()
    state = TaskState(job_id="JOB-x", album_id=5, target="qq:dm:1")
    p._running_tasks[5] = MagicMock()  # 非当前 task
    p._cleanup_task(state)
    # current_task != _running_tasks[5] → 不弹
    assert 5 in p._running_tasks


async def test_cleanup_task_registry_trimmed_to_30() -> None:
    p = make_plugin()
    for i in range(35):
        p._task_registry[f"JOB-{i}"] = TaskState(job_id=f"JOB-{i}", album_id=i, target="qq:dm:1")
    state = TaskState(job_id="JOB-trim", album_id=99, target="qq:dm:1")
    p._running_tasks[99] = asyncio.current_task()  # 让自我保护放行
    p._cleanup_task(state)
    assert len(p._task_registry) <= 30


# ── C1: _load_config 分组读取 + 旧版平铺兼容 ──────────────────────────


def test_config_section_nested() -> None:
    p = JMdownPlugin(
        MagicMock(),
        {
            "download": {"download_threads": 30, "max_concurrent": 3},
            "content": {
                "content_query": True,
                "block_content_tools": False,
                "allow_cross_session": True,
            },
            "encryption": {"zip_encrypt": False, "custom_password": "pw"},
            "quality": {"pdf_quality": 70},
            "upload": {"upload_timeout": 600, "chunk_size": 1024 * 1024},
            "cache": {"max_cache": 5},
            "notification": {"notify_llm": False},
        },
    )
    p._load_config()
    assert p._download_threads == 30
    assert p._max_concurrent == 3
    assert p._content_query
    assert not p._block_content_tools
    assert p._allow_cross_session
    assert p._custom_password == "pw"
    assert p._pdf_quality == 70
    assert p._upload_timeout == 600
    assert p._chunk_size == 1024 * 1024
    assert p._max_cache == 5
    assert not p._notify_llm


def test_config_flat_legacy_fallback() -> None:
    """旧版平铺配置（schema 分组前生成的 jmdown.json）仍可读取。"""
    p = JMdownPlugin(
        MagicMock(),
        {
            "download_threads": 30,
            "max_concurrent": 3,
            "content_query": True,
            "allow_cross_session": True,
        },
    )
    p._load_config()
    assert p._download_threads == 30
    assert p._max_concurrent == 3
    assert p._content_query
    assert p._allow_cross_session
    assert p._max_cache == 10  # 未提供 → 默认


def test_config_section_precedence_over_flat() -> None:
    p = JMdownPlugin(MagicMock(), {"download": {"download_threads": 60}, "download_threads": 30})
    p._load_config()
    assert p._download_threads == 60


def test_config_section_not_dict_falls_back() -> None:
    p = JMdownPlugin(MagicMock(), {"download": "oops", "download_threads": 30})
    p._load_config()
    assert p._download_threads == 30


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("_download_threads", 45),
        ("_max_concurrent", 2),
        ("_content_query", False),
        ("_block_content_tools", True),
        ("_allow_cross_session", False),
        ("_zip_encrypt", False),
        ("_custom_password", ""),
        ("_pdf_quality", 85),
        ("_upload_timeout", 300),
        ("_chunk_size", 512 * 1024),
        ("_max_cache", 10),
        ("_notify_llm", True),
    ],
)
def test_config_defaults_when_empty(attr: str, expected: Any) -> None:
    p = JMdownPlugin(MagicMock(), {})
    p._load_config()
    assert getattr(p, attr) == expected


def test_config_clamping() -> None:
    p = JMdownPlugin(
        MagicMock(),
        {
            "download": {"download_threads": 0, "max_concurrent": 0},
            "upload": {"upload_timeout": 0, "chunk_size": 1},
        },
    )
    p._load_config()
    assert p._download_threads == 1
    assert p._max_concurrent == 1
    assert p._upload_timeout == 1
    assert p._chunk_size == 4096


def test_zip_encrypt_requires_pyzipper() -> None:
    p = JMdownPlugin(MagicMock(), {"encryption": {"zip_encrypt": True}})
    with patch("builtins.__import__", side_effect=ImportError):
        with pytest.raises(RuntimeError):
            p._load_config()


# ── D1: orphan（jmcomic 同步线程无法 cancel） ─────────────────────────


async def test_cancel_registers_orphan() -> None:
    """reload/terminate 取消任务时，jmcomic 同步线程无法被 cancel（orphan）。

    取消路径必须登记 orphan 延迟释放；清理必须容错，否则 initialize 会因
    WinError 145（目录非空，线程仍在写入）崩溃导致插件加载失败。
    """
    p = make_plugin()
    p._cache = MagicMock()
    p._cache.get.return_value = None
    p._cache_dir = None
    p._download_dir = None
    p.ctx.publish_notice = AsyncMock()
    state = TaskState(job_id="JOB-x", album_id=42, target="qq:dm:1")
    started = threading.Event()
    release = threading.Event()

    def hanging_dl(*args: Any, **kwargs: Any) -> None:
        started.set()
        release.wait(10)

    with patch("jmdown.main._download_images", side_effect=hanging_dl):
        t = asyncio.create_task(p._task_runner(state))
        # 轮询等待下载线程启动——不能同步阻塞事件循环（会饿死 _task_runner）
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        release.set()
    assert 42 in p._orphan_aids
    # 清理测试内遗留的延迟释放 bg task，避免泄漏
    bg = list(p._bg_tasks)
    for b in bg:
        b.cancel()
    await asyncio.gather(*bg, return_exceptions=True)


async def test_release_orphan_cleans_download_dir(tmp_path: Path) -> None:
    """延迟释放到期后，残留的下载目录应被清理。"""
    p = make_plugin()
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "42").mkdir()
    (dl / "42" / "a.jpg").write_bytes(b"x")
    p._download_dir = dl
    p._cache_dir = tmp_path
    p._orphan_aids.add(42)
    await p._release_orphan_after(42, 0)
    assert 42 not in p._orphan_aids
    assert not (dl / "42").exists()


def test_clean_orphans_tolerates_failure(tmp_path: Path) -> None:
    """目录删除失败（WinError 145，线程仍占用）不应使 initialize 崩溃。"""
    p = make_plugin()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "424022").mkdir()
    p._cache_dir = cache_dir
    p._download_dir = dl
    p._cache = MagicMock()
    p._cache.list_all.return_value = []
    with patch("jmdown.main._rmtree", side_effect=OSError(145, "目录不是空的")):
        p._clean_orphans()  # 不应抛


def test_clean_orphans_skips_orphan_aid_dirs(tmp_path: Path) -> None:
    """_orphan_aids 中的目录应跳过，等待延迟释放后清理。"""
    p = make_plugin()
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "42").mkdir()
    (dl / "43").mkdir()
    p._download_dir = dl
    p._cache_dir = tmp_path
    p._cache = MagicMock()
    p._cache.list_all.return_value = []
    p._orphan_aids.add(42)
    p._clean_orphans()
    assert (dl / "42").exists()  # orphan 跳过
    assert not (dl / "43").exists()  # 非 orphan 仍清理


# ── E2: 上传 watchdog 与发送阶段超时 ─────────────────────────────────


async def test_watchdog_keeps_original_timeout_message() -> None:
    """send_action 的超时信息（含 action 名）不应被"上传超时, 已取消"吞掉。"""
    p = make_plugin()

    async def slow() -> str:
        raise TimeoutError("请求 upload_private_file 超时")

    with pytest.raises(TimeoutError) as cm:
        await p._upload_with_watchdog(slow(), 60)
    assert "upload_private_file" in str(cm.value)


async def test_task_runner_cache_hit_passes_send_timeout(tmp_path: Path) -> None:
    """upload_method=stream 时缓存命中路径必须区分分片超时与发送超时。"""
    p = make_plugin()
    p._upload_method = "stream"
    p.ctx.publish_notice = AsyncMock()

    dl = tmp_path / "downloads"
    dl.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    pdf = cache_dir / "7.pdf"
    pdf.write_bytes(b"%PDF-fake")
    p._download_dir = dl
    p._cache_dir = cache_dir
    p._cache = MagicMock()
    p._cache.get.return_value = CacheEntry(
        album_id=7,
        title="T",
        description="",
        page_count=1,
        pdf_path=str(pdf),
        size_bytes=10,
        downloaded_at=time.time(),
    )
    p._content_query = False
    p._zip_encrypt = False
    p._chunk_size = 512 * 1024

    captured: dict[str, Any] = {}

    async def fake_send(
        ctx: Any,
        sid: str,
        user_id: str,
        file_path: str,
        is_group: bool,
        group_id: str | None,
        timeout: float,
        progress_cb: Callable[..., Any] | None,
        chunk_size: int,
        send_timeout: float,
        guard: Any = None,
        on_activity: Callable[[], None] | None = None,
    ) -> str:
        captured.update(
            {
                "timeout": timeout,
                "send_timeout": send_timeout,
                "file_path": file_path,
                "guard": guard,
                "on_activity": on_activity,
            }
        )
        return "ok"

    # main.py 在函数内 `from .napcat_stream import ...`，patch 模块属性
    with patch("jmdown.napcat_stream.send_file_via_stream", new=fake_send):
        state = TaskState(job_id="JOB-c", album_id=7, target="qq:dm:1")
        await p._task_runner(state)

    assert captured["timeout"] == max(10, p._upload_timeout // 10)
    assert captured["send_timeout"] == p._upload_timeout + 120
    # stream 必须带无进展看门狗，否则外层没有总时长闸门
    assert captured["guard"] is not None
    assert captured["guard"].window >= 60
    # 每片 ack 必须能刷新任务存活时间戳（进度回调只有 ~20 次，不足以证明存活）
    assert captured["on_activity"].__self__ is state
    assert captured["on_activity"].__func__ is TaskState.touch
    assert state.status == "done"


async def test_stall_watchdog_raises_on_no_progress() -> None:
    """长时间没有 ack 才判失败，且信息里写清「停滞」而非「未收到响应」。"""
    from jmdown.napcat_stream import StallGuard

    p = make_plugin()
    guard = StallGuard(window=0.3)

    async def hang() -> str:
        await asyncio.sleep(10)
        return "unreachable"

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(RuntimeError) as cm:
        await p._upload_with_stall_watchdog(hang(), guard)
    idle = loop.time() - t0
    assert idle < 5  # 及时取消，不等满 10s
    assert idle >= 1.9, "必须真的经过至少一次轮询才判定"
    msg = str(cm.value)
    assert "停滞" in msg
    assert "没有收到任何实现端确认" in msg
    # 超限帧会被部分实现静默丢弃，"不是体积超限" 是不能下的结论
    assert "这不是体积超限" not in msg
    assert "静默丢弃" in msg


async def test_stall_watchdog_does_not_kill_slow_but_progressing() -> None:
    """慢速但有 ack 的传输不能被误杀，且必须跨过看门狗的轮询周期。"""
    from jmdown.napcat_stream import StallGuard

    p = make_plugin()
    guard = StallGuard(window=1.0)  # 阈值 > 每次间隔，但总时长 > 轮询周期 2s

    async def slow_but_alive() -> str:
        for _ in range(8):
            await asyncio.sleep(0.4)
            guard.touch()  # 每次 ack 都算进展
        return "ok"

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await p._upload_with_stall_watchdog(slow_but_alive(), guard) == "ok"
    assert loop.time() - t0 > 2, "总时长必须超过 2s 轮询周期才算验证到"


async def test_stall_watchdog_propagates_result_and_error() -> None:
    from jmdown.napcat_stream import StallGuard

    p = make_plugin()

    async def boom() -> str:
        raise ValueError("inner")

    with pytest.raises(ValueError):
        await p._upload_with_stall_watchdog(boom(), StallGuard(window=5))


# ── 文件大小上限：插件侧配置闸门 ──────────────────────────────────────


async def deliver_with_size(tmp_path: Path, size_bytes: int, limit_mb: int) -> str:
    p = make_plugin()
    p._max_file_mb = limit_mb
    p._upload_method = "path"
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x" * size_bytes)

    async def fake_send(ctx: Any, user_id: str, file_path: str, **kw: Any) -> str:
        return "ok"

    with patch("jmdown.file_send.send_file_standard", new=fake_send):
        return await p._deliver("qq:dm:1", "1", False, None, str(f), None)


async def test_size_limit_zero_means_unlimited(tmp_path: Path) -> None:
    assert await deliver_with_size(tmp_path, 2 * 1024 * 1024, 0) == "ok"


async def test_size_limit_over_limit_rejected_with_explicit_reason(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as cm:
        await deliver_with_size(tmp_path, 2 * 1024 * 1024, 1)
    msg = str(cm.value)
    assert "文件大小上限 1 MB" in msg
    assert "不是 QQ 或实现端的上限" in msg
    assert "设为 0" in msg


async def test_size_limit_under_and_equal_passes(tmp_path: Path) -> None:
    assert await deliver_with_size(tmp_path, 1024 * 1024, 1) == "ok"


async def test_size_limit_advice_is_actionable_for_marginal_overflow(tmp_path: Path) -> None:
    """1.2MB 被 1MB 拦下时，建议值必须能真的放行（不能四舍五入回 1MB）。"""
    with pytest.raises(RuntimeError) as cm:
        await deliver_with_size(tmp_path, int(1.2 * 1024 * 1024), 1)
    assert "调到 ≥ 2 MB" in str(cm.value)


async def test_size_limit_missing_file_yields_actionable_error() -> None:
    """max_file_mb>0 时也不能把裸 FileNotFoundError 抛给 LLM。"""
    p = make_plugin()
    p._max_file_mb = 1
    p._upload_method = "path"
    with pytest.raises(RuntimeError) as cm:
        await p._deliver("qq:dm:1", "1", False, None, "/nonexistent/x.pdf", None)
    assert "待发送文件不可读" in str(cm.value)


# ── 并发提交：预查窗口内必须原子占位 ─────────────────────────────────


async def test_same_album_twice_during_precheck() -> None:
    """预查元信息是 await，「检查-注册」之间必须原子占位。"""
    p = make_plugin()
    p._max_concurrent = 5
    ev = event_with_sid("qq:dm:1")

    def slow_meta(_aid: int) -> dict:
        time.sleep(0.3)  # 放大预查窗口
        return {}

    with (
        patch("jmdown.main._fetch_album_meta", side_effect=slow_meta),
        patch.object(JMdownPlugin, "_task_runner", new=AsyncMock()),
    ):
        first, second = await asyncio.gather(
            p.send_jm_album(ev, 77, "qq:dm:1"),
            p.send_jm_album(ev, 77, "qq:dm:1"),
        )

    joined = [r for r in (first, second) if "加入队列" in r]
    blocked = [r for r in (first, second) if "正在校验" in r]
    assert len(joined) == 1, f"重复提交应只建一个任务: {first!r} / {second!r}"
    assert len(blocked) == 1
    assert len(p._running_tasks) == 1
    assert len(p._task_registry) == 1
    assert p._pending_aids == set(), "占位必须释放"


async def test_pending_is_released_after_meta_error() -> None:
    p = make_plugin()
    with patch("jmdown.main._fetch_album_meta", side_effect=JMDownError("不存在")):
        ret = await p.send_jm_album(event_with_sid("qq:dm:1"), 78, "qq:dm:1")

    assert "错误" in ret
    assert p._pending_aids == set()
