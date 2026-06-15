"""
JMComic 下载器 — 缓存模块

独立于 KiraAI/jmcomic，可单元测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CacheEntry:
    album_id: int
    title: str
    description: str
    page_count: int
    pdf_path: str
    size_bytes: int
    downloaded_at: float


class CacheIndex:
    """FIFO 缓存队列, 持久化 JSON."""

    def __init__(self, index_path: Path, max_entries: int):
        self._path = index_path
        self._max = max_entries
        self._entries: list[CacheEntry] = []
        self._load()

    def get(self, album_id: int) -> Optional[CacheEntry]:
        for e in self._entries:
            if e.album_id == album_id:
                return e
        return None

    def put(self, entry: CacheEntry) -> list[CacheEntry]:
        self._entries = [e for e in self._entries if e.album_id != entry.album_id]
        self._entries.append(entry)
        evicted: list[CacheEntry] = []
        while len(self._entries) > self._max:
            evicted.append(self._entries.pop(0))
        self._save()
        return evicted

    def list_all(self) -> list[CacheEntry]:
        return list(self._entries)

    def _load(self):
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            self._entries = [CacheEntry(**e) for e in raw]
        except Exception as exc:
            # logger unavailable here in standalone context
            self._entries = []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "album_id": e.album_id, "title": e.title,
                "description": e.description, "page_count": e.page_count,
                "pdf_path": e.pdf_path, "size_bytes": e.size_bytes,
                "downloaded_at": e.downloaded_at,
            }
            for e in self._entries
        ]
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
