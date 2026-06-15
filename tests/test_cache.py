"""CacheIndex 单元测试 — 不依赖 jmcomic / KiraAI 核心."""

import json
import time
import tempfile
from pathlib import Path

import pytest

from jmdown.cache import CacheEntry, CacheIndex


# ── 夹具 ──

@pytest.fixture
def index_path():
    with tempfile.TemporaryDirectory(prefix="_test_cache_") as tmp:
        yield Path(tmp) / "cache_index.json"


def _entry(album_id: int, **kw) -> CacheEntry:
    d = dict(album_id=album_id, title="t", description="d", page_count=10,
             pdf_path="/x/y.pdf", size_bytes=1000, downloaded_at=time.time())
    d.update(kw)
    return CacheEntry(**d)


# ── CacheEntry ──

class TestCacheEntry:
    def test_fields(self):
        e = CacheEntry(album_id=1, title="x", description="d", page_count=5,
                       pdf_path="/a/b.pdf", size_bytes=500, downloaded_at=100.0)
        assert e.album_id == 1
        assert e.title == "x"
        assert e.description == "d"
        assert e.page_count == 5
        assert e.pdf_path == "/a/b.pdf"
        assert e.size_bytes == 500
        assert e.downloaded_at == 100.0


# ── CacheIndex ──

class TestCacheIndexInit:
    def test_empty_when_no_file(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        assert ci.list_all() == []

    def test_load_existing(self, index_path):
        data = [
            dict(album_id=1, title="a", description="d", page_count=5,
                 pdf_path="/1.pdf", size_bytes=100, downloaded_at=10.0),
            dict(album_id=2, title="b", description="e", page_count=3,
                 pdf_path="/2.pdf", size_bytes=200, downloaded_at=20.0),
        ]
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(data), "utf-8")
        ci = CacheIndex(index_path, max_entries=10)
        assert len(ci.list_all()) == 2

    def test_corrupt_file_fallback(self, index_path):
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("not-json", "utf-8")
        ci = CacheIndex(index_path, max_entries=10)
        assert ci.list_all() == []

    def test_max_entries_bound(self, index_path):
        ci = CacheIndex(index_path, max_entries=3)
        for i in range(4):
            ci.put(_entry(i))
        assert len(ci.list_all()) == 3


class TestCacheIndexGet:
    def test_hit(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        e = _entry(42)
        ci.put(e)
        assert ci.get(42) is not None
        assert ci.get(42).album_id == 42

    def test_miss(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        ci.put(_entry(1))
        assert ci.get(999) is None

    def test_after_eviction(self, index_path):
        ci = CacheIndex(index_path, max_entries=2)
        ci.put(_entry(1))
        ci.put(_entry(2))
        ci.put(_entry(3))  # 1 evicted
        assert ci.get(1) is None
        assert ci.get(2) is not None
        assert ci.get(3) is not None


class TestCacheIndexPut:
    def test_append(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        e = _entry(1)
        evicted = ci.put(e)
        assert evicted == []
        assert len(ci.list_all()) == 1

    def test_dedup(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        ci.put(_entry(1, title="old"))
        ci.put(_entry(1, title="new"))
        assert len(ci.list_all()) == 1
        assert ci.get(1).title == "new"

    def test_eviction_order_fifo(self, index_path):
        ci = CacheIndex(index_path, max_entries=3)
        for i in range(5):
            ci.put(_entry(i))
        ids = [e.album_id for e in ci.list_all()]
        assert ids == [2, 3, 4], f"expected [2,3,4] got {ids}"

    def test_eviction_return_oldest(self, index_path):
        ci = CacheIndex(index_path, max_entries=2)
        ci.put(_entry(1))
        ci.put(_entry(2))
        evicted = ci.put(_entry(3))
        assert len(evicted) == 1
        assert evicted[0].album_id == 1

    def test_no_eviction_when_under_limit(self, index_path):
        ci = CacheIndex(index_path, max_entries=5)
        for i in range(3):
            ci.put(_entry(i))
        assert len(ci.list_all()) == 3


class TestCacheIndexPersistence:
    def test_save_and_reload(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        ci.put(_entry(1, title="persist"))
        ci2 = CacheIndex(index_path, max_entries=10)
        assert ci2.get(1) is not None
        assert ci2.get(1).title == "persist"

    def test_json_format(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        ci.put(_entry(1, title="fmt"))
        raw = json.loads(index_path.read_text("utf-8"))
        assert isinstance(raw, list)
        assert raw[0]["album_id"] == 1
        assert raw[0]["title"] == "fmt"

    def test_empty_index_creates_no_file(self, index_path):
        ci = CacheIndex(index_path, max_entries=10)
        assert not index_path.exists()
