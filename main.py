"""
JMComic 下载器 — KiraAI 插件

流程: 下载本子 → 合 PDF → 删原图 → 缓存管理
最终返回标题、描述、页数、PDF 路径.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import jmcomic
from PIL import Image
import img2pdf

from core.plugin import BasePlugin, PluginContext, register_tool as tool, logger
from core.chat.message_elements import Text
from core.chat.message_utils import MessageChain

from .cache import CacheEntry, CacheIndex


class JMDownError(Exception):
    """插件业务异常"""


def _parse_target(target: str) -> tuple[str, bool, Optional[str]]:
    """解析目标会话标识 → (user_id, is_group, group_id)

    格式: adapter:type:id
    - qq:dm:123456 → 私聊 user 123456
    - qq:gm:789012 → 群聊 group 789012
    """
    parts = target.split(":", 2)
    if len(parts) != 3:
        raise JMDownError(f"目标会话格式错误: {target}, 应为 adapter:type:id")
    _adapter, stype, sid = parts
    if stype == "dm":
        return sid, False, None
    if stype == "gm":
        return sid, True, sid
    raise JMDownError(f"未知会话类型: {stype}, 应为 dm(私聊) 或 gm(群聊)")


# ── 下载 & PDF ──

def _download_images(album_id: int, download_dir: Path, threads: int = 45) -> tuple:
    """下载图片, 返回 (album_obj, image_dir, images[], title, desc)."""
    opt = jmcomic.JmOption.default()
    opt.dir_rule.base_dir = str(download_dir.resolve())
    opt.dir_rule.rule = "Aid"
    opt.download.image.suffix = ".jpg"
    opt.download.image.decode = True
    opt.download.threading.image = threads
    opt.client.retry_times = 3
    opt.plugins.after_album = []

    try:
        result_set = opt.download_album([album_id])
    except Exception as e:
        raise JMDownError(f"下载失败: {e}") from e
    if not result_set:
        raise JMDownError("下载返回空结果")

    album_obj = next(iter(result_set))[0]
    title = getattr(album_obj, "name", str(album_id))
    description = getattr(album_obj, "description", "")

    # 找图片目录: 扫 download_dir 下最新有图的子目录
    image_dir: Optional[Path] = None
    candidates = sorted(
        d for d in download_dir.iterdir()
        if d.is_dir()
    )
    # 优先 Aid 规则目录, 其次含 ID 的, 最后最新的
    for d in candidates:
        if d.name == str(album_id):
            image_dir = d
            break
    if image_dir is None:
        for d in candidates:
            if str(album_id) in d.name:
                image_dir = d
                break
    if image_dir is None:
        # 取最新有图片的子目录
        for d in reversed(candidates):
            has_img = any(
                p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
                for p in d.iterdir()
            )
            if has_img:
                image_dir = d
                break
    if image_dir is None:
        raise JMDownError("下载完成但找不到图片目录")

    valid = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    images = sorted(
        p for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in valid
    )
    if not images:
        images = sorted(
            p for p in image_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in valid
        )
    if not images:
        raise JMDownError("下载完成但目录中无图片文件")

    return album_obj, image_dir, images, title, description


def _images_to_pdf(images: list[Path], output_path: Path, quality: int = 85) -> int:
    """图片合 PDF, 返回字节数. 非 JPEG 先转 JPEG."""
    final: list[Path] = []
    temps: list[Path] = []
    try:
        for p in images:
            if p.suffix.lower() in (".jpg", ".jpeg"):
                final.append(p)
            else:
                tmp = p.with_name(p.stem + ".jm_tmp.jpg")
                Image.open(p).convert("RGB").save(tmp, "JPEG", quality=quality, optimize=True)
                final.append(tmp)
                temps.append(tmp)
        pdf_data = img2pdf.convert(*[str(p) for p in final],
                                   producer="kira-jmdown", creator="kira-jmdown")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(pdf_data)
        return output_path.stat().st_size
    finally:
        for t in temps:
            t.unlink(missing_ok=True)


def _rmtree(dir_path: Path):
    if not dir_path.exists():
        return
    for child in dir_path.iterdir():
        if child.is_dir():
            _rmtree(child)
        else:
            child.unlink(missing_ok=True)
    dir_path.rmdir()


# ── 插件 ──

class JMdownPlugin(BasePlugin):
    """JMComic 本子下载 → PDF 插件."""

    def __init__(self, ctx: PluginContext, cfg: dict):
        super().__init__(ctx, cfg)
        self._data_dir: Optional[Path] = None
        self._download_dir: Optional[Path] = None
        self._cache_dir: Optional[Path] = None
        self._cache: Optional[CacheIndex] = None
        self._max_cache: int = 10
        self._pdf_quality: int = 85
        self._desc_max_length: int = 80
        self._stream_threshold: int = 10 * 1024 * 1024   # 10MB
        self._upload_timeout: int = 300

    async def initialize(self):
        self._data_dir = self.ctx.get_plugin_data_dir() or Path("data/plugin_data/jmdown")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir = self._data_dir / "downloads"
        self._download_dir.mkdir(exist_ok=True)
        self._cache_dir = self._data_dir / "cache"
        self._cache_dir.mkdir(exist_ok=True)

        self._max_cache = int(self.plugin_cfg.get("max_cache", 10))
        self._desc_max_length = int(self.plugin_cfg.get("desc_max_length", 80))
        self._pdf_quality = int(self.plugin_cfg.get("pdf_quality", 85))
        mb = int(self.plugin_cfg.get("stream_threshold_mb", 10))
        self._stream_threshold = mb * 1024 * 1024
        self._upload_timeout = int(self.plugin_cfg.get("upload_timeout", 300))
        self._cache = CacheIndex(self._data_dir / "cache_index.json", self._max_cache)
        self._clean_orphans()

        logger.info(
            f"JMdown 就绪, 缓存上限 {self._max_cache} 本, "
            f"流传输阈值 {mb}MB"
        )

    async def terminate(self):
        logger.info("JMdown 已终止")

    async def _notice(self, sid: str, text: str):
        """通过会话发送进度通知，LLM 后续能看见。"""
        if not sid:
            return
        try:
            await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=False)
        except Exception:
            pass  # 通知失败不中断主流程

    @tool(
        "send_jm_album",
        "下载禁漫天堂(JMComic)本子并发送 PDF 到指定会话。返回标题、页数、发送结果。",
        {
            "type": "object",
            "properties": {
                "album_id": {
                    "type": "integer",
                    "description": "禁漫本子数字 ID"
                },
                "target": {
                    "type": "string",
                    "description": (
                        "目标会话标识，格式为 adapter_name:session_type:session_id。"
                        "示例：qq:dm:123456（私聊）、qq:gm:789012（群聊）"
                    )
                }
            },
            "required": ["album_id", "target"]
        }
    )
    async def send_jm_album(self, event, album_id: int, target: str) -> str:
        if album_id <= 0:
            return "错误: album_id 须为正整数"

        user_id, is_group, group_id = _parse_target(target)

        try:
            return await self._download(album_id, target, user_id, is_group, group_id)
        except JMDownError as e:
            logger.error(f"#{album_id} 失败: {e}")
            return f"❌ 失败: {e}"
        except Exception as e:
            logger.error(f"#{album_id} 未知: {e}")
            return f"❌ 未知错误: {e}"

    async def _download(
        self, album_id: int,
        sid: str = "", user_id: str = "",
        is_group: bool = False, group_id: Optional[str] = None,
    ) -> str:
        ml = self._desc_max_length

        # ── 缓存命中 ──
        cached = self._cache.get(album_id)
        if cached and Path(cached.pdf_path).exists():
            await self._notice(sid, f"📤 缓存命中 ({self._fmt(Path(cached.pdf_path).stat().st_size)})，发送中...")
            from .napcat_stream import send_file_via_stream
            send_result = await send_file_via_stream(
                self.ctx, sid, user_id, cached.pdf_path,
                is_group, group_id, self._upload_timeout,
            )
            cached_size = Path(cached.pdf_path).stat().st_size
            return (
                f"✅ 缓存命中\n"
                f"📖 {cached.title}\n"
                f"📝 {(cached.description[:ml] + '...') if len(cached.description) > ml else (cached.description or '无描述')}\n"
                f"📄 {cached.page_count} 页\n"
                f"💾 {self._fmt(cached_size)}\n"
                f"{send_result}"
            )

        # ── 下载 → PDF → 发送 ──
        await self._notice(sid, f"⏬ 开始下载 #{album_id} ...")
        logger.info(f"下载 #{album_id} ...")

        threads = int(self.plugin_cfg.get("download_threads", 45))
        _, image_dir, images, title, description = _download_images(
            album_id, self._download_dir, threads
        )

        await self._notice(sid, f"✅ 已下载 {len(images)} 张图片，合成 PDF 中...")

        pdf_path = self._cache_dir / f"{album_id}.pdf"
        size = _images_to_pdf(images, pdf_path, self._pdf_quality)
        _rmtree(image_dir)

        page_count = len(images)
        entry = CacheEntry(
            album_id=album_id, title=title, description=description,
            page_count=page_count, pdf_path=str(pdf_path.resolve()),
            size_bytes=size, downloaded_at=time.time(),
        )
        evicted = self._cache.put(entry)
        self._evict_cleanup(evicted)

        await self._notice(sid, f"📤 PDF ({self._fmt(size)}) 发送中...")
        from .napcat_stream import send_file_via_stream
        send_result = await send_file_via_stream(
            self.ctx, sid, user_id, str(pdf_path.resolve()),
            is_group, group_id, self._upload_timeout,
        )

        desc = description[:ml] + "..." if len(description) > ml else description
        lines = [
            f"✅ {title}",
            f"📝 {desc or '无描述'}",
            f"📄 {page_count} 页",
            f"💾 {self._fmt(size)}",
            send_result,
        ]
        if evicted:
            lines.append(f"🗑️ 淘汰 {len(evicted)} 本旧缓存")
        return "\n".join(lines)

    def _evict_cleanup(self, entries: list[CacheEntry]):
        for e in entries:
            p = Path(e.pdf_path)
            if p.exists():
                p.unlink()
                logger.info(f"淘汰缓存: {p.name}")
            img_dir = self._download_dir / str(e.album_id)
            if img_dir.exists():
                _rmtree(img_dir)

    def _clean_orphans(self):
        known = {e.album_id for e in self._cache.list_all()}
        for f in self._cache_dir.iterdir():
            if f.suffix == ".pdf" and f.stem.isdigit() and int(f.stem) not in known:
                f.unlink()
                logger.info(f"清理孤儿: {f.name}")
        for d in self._download_dir.iterdir():
            if d.is_dir() and d.name.isdigit() and int(d.name) not in known:
                _rmtree(d)
                logger.info(f"清理孤儿: {d.name}")

    @staticmethod
    def _fmt(b: int) -> str:
        if b < 1024:
            return f"{b} B"
        if b < 1024**2:
            return f"{b/1024:.1f} KB"
        return f"{b/1024**2:.1f} MB"
