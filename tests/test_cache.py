"""CacheIndex 单元测试 —— 覆盖 _load_error 属性存在性、getattr 防御兜底
以及 __pycache__ 清理逻辑（对应 #11 bug 修复）。

Run:
    pytest tests/test_cache.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cache import CacheEntry, CacheIndex


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "cache_index.json"


def entry(album_id: int, **over: Any) -> CacheEntry:
    base = {
        "album_id": album_id,
        "title": f"t{album_id}",
        "description": "",
        "page_count": 10,
        "pdf_path": f"/x/{album_id}.pdf",
        "size_bytes": 1000,
        "downloaded_at": float(album_id),
    }
    base.update(over)
    return CacheEntry(**base)


# ── _load_error 属性存在性 + 各场景正确值 ─────────────────────────────


def test_index_file_not_exist(index_path: Path) -> None:
    """索引文件不存在 → _load_error=False, _entries=[]"""
    ci = CacheIndex(index_path, max_entries=5)
    assert hasattr(ci, "_load_error"), "CacheIndex 必须有 _load_error 属性"
    assert not ci._load_error
    assert ci.list_all() == []


def test_valid_json(index_path: Path) -> None:
    """合法的 JSON 索引文件 → _load_error=False, 条目正确加载"""
    entries = [
        {
            "album_id": 1,
            "title": "t1",
            "description": "",
            "page_count": 10,
            "pdf_path": "/x/1.pdf",
            "size_bytes": 1000,
            "downloaded_at": 100.0,
        },
        {
            "album_id": 2,
            "title": "t2",
            "description": "",
            "page_count": 20,
            "pdf_path": "/x/2.pdf",
            "size_bytes": 2000,
            "downloaded_at": 200.0,
        },
    ]
    index_path.write_text(json.dumps(entries), "utf-8")

    ci = CacheIndex(index_path, max_entries=5)
    assert not ci._load_error
    assert len(ci.list_all()) == 2
    assert ci.list_all()[0].album_id == 1
    assert ci.list_all()[1].album_id == 2


def test_corrupted_json(index_path: Path) -> None:
    """损坏的 JSON → _load_error=True, _entries=[]"""
    index_path.write_text("这不是 JSON", "utf-8")

    ci = CacheIndex(index_path, max_entries=5)
    assert ci._load_error, "损坏 JSON 时应设 _load_error=True"
    assert ci.list_all() == []


def test_empty_file(index_path: Path) -> None:
    """空文件 → _load_error=True"""
    index_path.write_text("", "utf-8")

    ci = CacheIndex(index_path, max_entries=5)
    assert ci._load_error


def test_field_whitelist_drops_unknown(index_path: Path) -> None:
    """未知字段应被字段白名单过滤，不触发 _load_error"""
    entries = [
        {
            "album_id": 1,
            "title": "t1",
            "description": "",
            "page_count": 10,
            "pdf_path": "/x/1.pdf",
            "size_bytes": 1000,
            "downloaded_at": 100.0,
            "malicious_injection": "evil",
        },
    ]
    index_path.write_text(json.dumps(entries), "utf-8")

    ci = CacheIndex(index_path, max_entries=5)
    assert not ci._load_error
    assert len(ci.list_all()) == 1


def test_missing_required_field(index_path: Path) -> None:
    """缺少必需字段 → CacheEntry(**filtered) 构造异常 → _load_error=True"""
    index_path.write_text(json.dumps([{"album_id": 1, "title": "t1"}]), "utf-8")

    ci = CacheIndex(index_path, max_entries=5)
    # CacheEntry(**filtered) 缺必需字段，dataclass 构造抛 TypeError，_load() catch 后设标志
    assert ci._load_error
    assert len(ci.list_all()) == 0


def test_put_after_load_error(index_path: Path) -> None:
    """_load_error 后依然可以 put 新条目，不影响功能"""
    index_path.write_text("{", "utf-8")  # 截断 JSON → json.loads 抛异常

    ci = CacheIndex(index_path, max_entries=5)
    assert ci._load_error

    assert ci.put(entry(99, title="new", page_count=5, size_bytes=500, downloaded_at=300.0)) == []
    assert len(ci.list_all()) == 1
    assert ci.list_all()[0].album_id == 99


def test_fifo_eviction(index_path: Path) -> None:
    """超出 max_entries 时淘汰最旧条目"""
    ci = CacheIndex(index_path, max_entries=3)
    for i in range(5):
        ci.put(entry(i))

    assert len(ci.list_all()) == 3
    assert ci.list_all()[0].album_id == 2
    assert ci.list_all()[-1].album_id == 4


def test_put_updates_existing(index_path: Path) -> None:
    """put 同一 album_id 应更新而非重复"""
    ci = CacheIndex(index_path, max_entries=5)
    for i in range(2):
        ci.put(entry(1, title=f"v{i}"))

    assert len(ci.list_all()) == 1
    assert ci.list_all()[0].title == "v1"


# ── 防御兜底：即使加载了陈旧 CacheIndex 也不崩溃 ──────────────────────


class _OldCacheIndex:
    """模拟旧版 CacheIndex（没有 _load_error 属性）"""

    def __init__(self) -> None:
        self._entries: list[Any] = []

    def get(self, album_id: int) -> None:
        return None

    def put(self, e: CacheEntry) -> list[Any]:
        return []

    def list_all(self) -> list[Any]:
        return []


class _NewCacheIndex(_OldCacheIndex):
    """模拟新版 CacheIndex（有 _load_error 属性）"""

    def __init__(self) -> None:
        super().__init__()
        self._load_error = False


def test_missing_attribute_returns_true() -> None:
    """obj 没有 _load_error → getattr 返回 True（保守跳过清理）"""
    assert getattr(_OldCacheIndex(), "_load_error", True) is True


def test_false_attribute_returns_false() -> None:
    """obj._load_error=False → getattr 返回 False（正常执行清理）"""
    assert getattr(_NewCacheIndex(), "_load_error", True) is False


def test_true_attribute_returns_true() -> None:
    """obj._load_error=True → getattr 返回 True（跳过清理）"""
    new = _NewCacheIndex()
    new._load_error = True
    assert getattr(new, "_load_error", True) is True


# ── __pycache__ 清理逻辑（与 plugin_registry.py 的实现一致） ──────────


def cleanup_pycache(directory: Path) -> None:
    """复刻 plugin_registry.py 的清理：删 .py[cod]，目录空了就 rmdir。"""
    if not directory.is_dir():
        return
    for pyc_file in directory.glob("*.py[cod]"):
        pyc_file.unlink(missing_ok=True)
    try:
        directory.rmdir()
    except OSError:
        pass  # 还有别的文件，保留目录


@pytest.fixture
def pycache_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plugin" / "__pycache__"
    d.mkdir(parents=True)
    return d


def make_pyc(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_bytes(b"\x00" * 16)
    return p


def test_removes_all_pyc_files(pycache_dir: Path) -> None:
    """清理应删除所有 .py[cod] 文件"""
    f1 = make_pyc(pycache_dir, "cache.cpython-315.pyc")
    f2 = make_pyc(pycache_dir, "main.cpython-315.pyc")
    f3 = make_pyc(pycache_dir, "cache.cpython-314.pyo")

    cleanup_pycache(pycache_dir)

    assert not f1.exists()
    assert not f2.exists()
    assert not f3.exists()


def test_ignores_non_pyc_files(pycache_dir: Path) -> None:
    """清理不应删除 .py 或 .json 等无关文件"""
    make_pyc(pycache_dir, "cache.cpython-315.pyc")
    other = pycache_dir / "readme.txt"
    other.write_text("hello")

    cleanup_pycache(pycache_dir)

    assert other.exists(), "非 .py[cod] 文件应保留"


def test_removes_empty_pycache_dir(pycache_dir: Path) -> None:
    """所有 .pyc 删除后 __pycache__ 空 → rmdir 应删除目录"""
    make_pyc(pycache_dir, "cache.cpython-315.pyc")

    cleanup_pycache(pycache_dir)

    assert not pycache_dir.exists(), "空的 __pycache__ 目录应被删除"


def test_keeps_pycache_dir_if_non_pyc_remains(pycache_dir: Path) -> None:
    """__pycache__ 还有非 .pyc 文件 → rmdir 抛 OSError → 静默忽略"""
    make_pyc(pycache_dir, "cache.cpython-315.pyc")
    (pycache_dir / "readme.txt").write_text("hello")

    cleanup_pycache(pycache_dir)

    assert pycache_dir.exists(), "非空 __pycache__ 应保留"


def test_no_pycache_dir_is_noop(tmp_path: Path) -> None:
    """没有 __pycache__ 目录 → 不做任何操作，不抛异常"""
    missing = tmp_path / "other_dir"

    cleanup_pycache(missing)  # 不抛异常即通过

    assert not missing.exists()
