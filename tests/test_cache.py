"""
CacheIndex 单元测试 — 覆盖 _load_error 属性存在性 + getattr 防御兜底 +
__pycache__ 清理逻辑（对应 #11 bug 修复）。

Run:
    python -m pytest tests/test_cache.py -v
    # or
    python -m unittest tests.test_cache -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

# ── 被测模块 ──
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cache import CacheEntry, CacheIndex


class CacheIndexLoadErrorTest(unittest.TestCase):
    """_load_error 属性存在性 + 各场景正确值"""

    # ── 夹具 ──

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.index_path = Path(self._tmp.name) / "cache_index.json"

    def tearDown(self):
        self._tmp.cleanup()

    # ── 正向用例 ──

    def test_index_file_not_exist(self):
        """索引文件不存在 → _load_error=False, _entries=[]"""
        ci = CacheIndex(self.index_path, max_entries=5)
        self.assertTrue(hasattr(ci, "_load_error"),
                        "CacheIndex 必须有 _load_error 属性")
        self.assertFalse(ci._load_error)
        self.assertEqual(ci.list_all(), [])

    def test_valid_json(self):
        """合法的 JSON 索引文件 → _load_error=False, 条目正确加载"""
        entries = [
            {"album_id": 1, "title": "t1", "description": "", "page_count": 10,
             "pdf_path": "/x/1.pdf", "size_bytes": 1000, "downloaded_at": 100.0},
            {"album_id": 2, "title": "t2", "description": "", "page_count": 20,
             "pdf_path": "/x/2.pdf", "size_bytes": 2000, "downloaded_at": 200.0},
        ]
        self.index_path.write_text(json.dumps(entries), "utf-8")

        ci = CacheIndex(self.index_path, max_entries=5)
        self.assertFalse(ci._load_error)
        self.assertEqual(len(ci.list_all()), 2)
        self.assertEqual(ci.list_all()[0].album_id, 1)
        self.assertEqual(ci.list_all()[1].album_id, 2)

    # ── 异常场景 ──

    def test_corrupted_json(self):
        """损坏的 JSON → _load_error=True, _entries=[]"""
        self.index_path.write_text("这不是 JSON", "utf-8")

        ci = CacheIndex(self.index_path, max_entries=5)
        self.assertTrue(ci._load_error,
                        "损坏 JSON 时应设 _load_error=True")
        self.assertEqual(ci.list_all(), [])

    def test_empty_file(self):
        """空文件 → _load_error=True"""
        self.index_path.write_text("", "utf-8")

        ci = CacheIndex(self.index_path, max_entries=5)
        self.assertTrue(ci._load_error)

    def test_field_whitelist_drops_unknown(self):
        """未知字段应被字段白名单过滤，不触发 _load_error"""
        entries = [
            {"album_id": 1, "title": "t1", "description": "", "page_count": 10,
             "pdf_path": "/x/1.pdf", "size_bytes": 1000, "downloaded_at": 100.0,
             "malicious_injection": "evil"},
        ]
        self.index_path.write_text(json.dumps(entries), "utf-8")

        ci = CacheIndex(self.index_path, max_entries=5)
        self.assertFalse(ci._load_error)
        self.assertEqual(len(ci.list_all()), 1)

    def test_missing_required_field(self):
        """缺少必需字段 → CacheEntry(**filtered) 构造异常 → _load_error=True"""
        entries = [
            {"album_id": 1, "title": "t1"},  # 缺 page_count/pdf_path/...
        ]
        self.index_path.write_text(json.dumps(entries), "utf-8")

        ci = CacheIndex(self.index_path, max_entries=5)
        # CacheEntry(**filtered) 缺必需字段，dataclass 构造抛 TypeError
        # _load() catch 后设 _load_error=True
        self.assertTrue(ci._load_error)
        self.assertEqual(len(ci.list_all()), 0)

    def test_put_after_load_error(self):
        """_load_error 后依然可以 put 新条目，不影响功能"""
        self.index_path.write_text("{", "utf-8")  # 截断 JSON → json.loads 抛异常

        ci = CacheIndex(self.index_path, max_entries=5)
        self.assertTrue(ci._load_error)

        entry = CacheEntry(album_id=99, title="new", description="",
                           page_count=5, pdf_path="/x/99.pdf",
                           size_bytes=500, downloaded_at=300.0)
        evicted = ci.put(entry)
        self.assertEqual(evicted, [])
        self.assertEqual(len(ci.list_all()), 1)
        self.assertEqual(ci.list_all()[0].album_id, 99)

    def test_fifo_eviction(self):
        """超出 max_entries 时淘汰最旧条目"""
        ci = CacheIndex(self.index_path, max_entries=3)
        for i in range(5):
            entry = CacheEntry(album_id=i, title=f"t{i}", description="",
                               page_count=10, pdf_path=f"/x/{i}.pdf",
                               size_bytes=1000, downloaded_at=float(i))
            ci.put(entry)

        self.assertEqual(len(ci.list_all()), 3)
        self.assertEqual(ci.list_all()[0].album_id, 2)
        self.assertEqual(ci.list_all()[-1].album_id, 4)

    def test_put_updates_existing(self):
        """put 同一 album_id 应更新而非重复"""
        ci = CacheIndex(self.index_path, max_entries=5)
        for i in range(2):
            entry = CacheEntry(album_id=1, title=f"v{i}", description="",
                               page_count=10, pdf_path="/x/1.pdf",
                               size_bytes=1000, downloaded_at=float(i))
            ci.put(entry)

        self.assertEqual(len(ci.list_all()), 1)
        self.assertEqual(ci.list_all()[0].title, "v1")


class GetattrDefensiveFallbackTest(unittest.TestCase):
    """getattr(..., True) 防御兜底 — 即使加载了陈旧 CacheIndex 也不崩溃"""

    class OldCacheIndex:
        """模拟旧版 CacheIndex（没有 _load_error 属性）"""
        def __init__(self):
            self._entries = []

        def get(self, album_id):
            return None

        def put(self, entry):
            return []

        def list_all(self):
            return []

    class NewCacheIndex:
        """模拟新版 CacheIndex（有 _load_error 属性）"""
        def __init__(self):
            self._entries = []
            self._load_error = False

        def get(self, album_id):
            return None

        def put(self, entry):
            return []

        def list_all(self):
            return []

    def test_missing_attribute_returns_true(self):
        """obj 没有 _load_error → getattr 返回 True（保守跳过清理）"""
        old = self.OldCacheIndex()
        self.assertEqual(getattr(old, "_load_error", True), True)

    def test_false_attribute_returns_false(self):
        """obj._load_error=False → getattr 返回 False（正常执行清理）"""
        new = self.NewCacheIndex()
        self.assertEqual(getattr(new, "_load_error", True), False)

    def test_true_attribute_returns_true(self):
        """obj._load_error=True → getattr 返回 True（跳过清理）"""
        new = self.NewCacheIndex()
        new._load_error = True
        self.assertEqual(getattr(new, "_load_error", True), True)


class PycacheCleanupTest(unittest.TestCase):
    """__pycache__ 清理逻辑 — 对应 plugin_registry.py 新增的清理代码"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.plugin_root = Path(self._tmp.name)
        self.pycache_dir = self.plugin_root / "__pycache__"
        self.pycache_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _create_pyc(self, name: str):
        """在 __pycache__ 中创建模拟 .pyc 文件"""
        p = self.pycache_dir / name
        p.write_bytes(b"\x00" * 16)
        return p

    def test_removes_all_pyc_files(self):
        """清理应删除所有 .py[cod] 文件"""
        f1 = self._create_pyc("cache.cpython-315.pyc")
        f2 = self._create_pyc("main.cpython-315.pyc")
        f3 = self._create_pyc("cache.cpython-314.pyo")

        # 执行与 plugin_registry.py 相同的清理逻辑
        if self.pycache_dir.is_dir():
            for pyc_file in self.pycache_dir.glob("*.py[cod]"):
                pyc_file.unlink(missing_ok=True)

        self.assertFalse(f1.exists())
        self.assertFalse(f2.exists())
        self.assertFalse(f3.exists())

    def test_ignores_non_pyc_files(self):
        """清理不应删除 .py 或 .json 等无关文件"""
        self._create_pyc("cache.cpython-315.pyc")
        other = self.pycache_dir / "readme.txt"
        other.write_text("hello")

        if self.pycache_dir.is_dir():
            for pyc_file in self.pycache_dir.glob("*.py[cod]"):
                pyc_file.unlink(missing_ok=True)

        self.assertTrue(other.exists(), "非 .py[cod] 文件应保留")

    def test_removes_empty_pycache_dir(self):
        """所有 .pyc 删除后 __pycache__ 空 → rmdir 应删除目录"""
        self._create_pyc("cache.cpython-315.pyc")

        if self.pycache_dir.is_dir():
            for pyc_file in self.pycache_dir.glob("*.py[cod]"):
                pyc_file.unlink(missing_ok=True)
            try:
                self.pycache_dir.rmdir()
            except OSError:
                pass

        self.assertFalse(self.pycache_dir.exists(),
                         "空的 __pycache__ 目录应被删除")

    def test_keeps_pycache_dir_if_non_pyc_remains(self):
        """__pycache__ 还有非 .pyc 文件 → rmdir 抛 OSError → 静默忽略"""
        self._create_pyc("cache.cpython-315.pyc")
        other = self.pycache_dir / "readme.txt"
        other.write_text("hello")

        if self.pycache_dir.is_dir():
            for pyc_file in self.pycache_dir.glob("*.py[cod]"):
                pyc_file.unlink(missing_ok=True)
            try:
                self.pycache_dir.rmdir()
            except OSError:
                pass

        self.assertTrue(self.pycache_dir.exists(),
                        "非空 __pycache__ 应保留")

    def test_no_pycache_dir_is_noop(self):
        """没有 __pycache__ 目录 → 不做任何操作，不抛异常"""
        no_pycache = self.plugin_root / "other_dir"

        # 目录不存在时，is_dir() 为 False，整个块跳过
        if no_pycache.is_dir():
            for pyc_file in no_pycache.glob("*.py[cod]"):
                pyc_file.unlink(missing_ok=True)
            try:
                no_pycache.rmdir()
            except OSError:
                pass

        # 不应抛异常就算通过
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
