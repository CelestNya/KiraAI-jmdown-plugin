"""
JMComic 下载器 — KiraAI 插件

流程: 下载本子 → 合 PDF → 删原图 → 缓存管理
最终返回标题、描述、页数、PDF 路径.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import jmcomic
from PIL import Image
import img2pdf

from core.plugin import BasePlugin, PluginContext, register_tool as tool, logger
from core.chat.message_elements import Text
from core.chat.message_utils import MessageChain

from .cache import CacheEntry, CacheIndex


@dataclass
class TaskState:
    """后台任务状态"""
    job_id: str
    album_id: int
    target: str
    status: str = "running"  # running | done | failed
    phases: dict = field(default_factory=lambda: {
        "下载": "待定", "合成": "待定", "上传": "待定", "发送": "待定",
    })
    result: Optional[dict] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    elapsed: float = 0.0


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


def _images_to_pdf(images: list[Path], output_path: Path, quality: int = 85,
                   progress_cb=None) -> int:
    """图片合 PDF, 返回字节数. 非 JPEG 先转 JPEG.
    progress_cb: callable(pct: int) 每 20% 回调.
    """
    total = len(images)
    last_report = -1
    final: list[Path] = []
    temps: list[Path] = []
    try:
        for idx, p in enumerate(images):
            if p.suffix.lower() in (".jpg", ".jpeg"):
                final.append(p)
            else:
                tmp = p.with_name(p.stem + ".jm_tmp.jpg")
                Image.open(p).convert("RGB").save(tmp, "JPEG", quality=quality, optimize=True)
                final.append(tmp)
                temps.append(tmp)
            if progress_cb:
                pct = int((idx + 1) / total * 100)
                report = pct // 20
                if report > last_report:
                    last_report = report
                    progress_cb(min(pct, 100))
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
        self._upload_timeout: int = 300

        # 后台任务系统
        self._task_registry: dict[int, TaskState] = {}
        self._task_counter: int = 0
        self._running_tasks: dict[int, asyncio.Task] = {}   # album_id → task

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
        self._upload_timeout = int(self.plugin_cfg.get("upload_timeout", 300))
        self._cache = CacheIndex(self._data_dir / "cache_index.json", self._max_cache)
        self._clean_orphans()

        logger.info("JMdown 就绪")

    async def terminate(self):
        for aid, task in self._running_tasks.items():
            if not task.done():
                task.cancel()
                logger.info(f"终止后台任务: #{aid}")
        self._running_tasks.clear()
        logger.info("JMdown 已终止")

    async def _notice(self, sid: str, text: str):
        """通过会话发送进度通知，LLM 后续能看见。"""
        if not sid:
            return
        try:
            await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=False)
        except Exception:
            pass  # 通知失败不中断主流程

    # ── 工具: 提交下载任务 ──

    @tool(
        "send_jm_album",
        "提交 JMComic 本子下载任务到后台，返回任务标识码。用 query_jm_task 查进度。",
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
    async def send_jm_album(self, _event, album_id: int, target: str) -> str:
        if album_id <= 0:
            return "错误: album_id 须为正整数"

        # 去重: 同一 album_id 正在运行则复用
        if album_id in self._running_tasks and not self._running_tasks[album_id].done():
            existing = self._task_registry.get(album_id)
            if existing:
                return f"⚠️ #{album_id} 已在下载队列中，标识码: {existing.job_id}"
            return f"⚠️ #{album_id} 已在下载队列中"

        # 生成 Job ID
        self._task_counter += 1
        job_id = f"JOB-{datetime.now().strftime('%y%m%d')}-{self._task_counter:03d}"

        _parse_target(target)  # 提前校验 target 格式

        state = TaskState(job_id=job_id, album_id=album_id, target=target)
        self._task_registry[album_id] = state

        task = asyncio.create_task(self._task_runner(state))
        self._running_tasks[album_id] = task

        logger.info(f"#{album_id} 入队列 → {job_id}")
        return f"🔖 任务已加入队列\n标识码: {job_id}"

    # ── 工具: 查询任务状态 ──

    @tool(
        "query_jm_task",
        "查询 JMComic 下载任务的状态。返回四阶段进度表格。",
        {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "任务标识码，如 JOB-240616-001"
                }
            },
            "required": ["job_id"]
        }
    )
    async def query_jm_task(self, _event, job_id: str) -> str:
        # 线性扫描 — 任务数很少
        for state in self._task_registry.values():
            if state.job_id == job_id:
                return self._format_state(state)
        return f"❌ 未找到任务: {job_id}"

    def _format_state(self, s: TaskState) -> str:
        p = s.phases
        elapsed = time.time() - s.started_at
        lines = [
            f"{'✅' if s.status == 'done' else '❌' if s.status == 'failed' else '🔖'} {s.job_id}",
            f"下载: {p['下载']} | 合成: {p['合成']} | 上传: {p['上传']} | 发送: {p['发送']}",
            f"⏱ {elapsed:.0f}s",
        ]
        if s.status == "done" and s.result:
            r = s.result
            ml = self._desc_max_length
            desc = r.get("description", "")
            if len(desc) > ml:
                desc = desc[:ml] + "..."
            lines.extend([
                f"📖 {r.get('title', '')}",
                f"📝 {desc or '无描述'}",
                f"📄 {r.get('page_count', 0)} 页  💾 {self._fmt(r.get('file_size', 0))}",
            ])
        if s.status == "failed" and s.error:
            lines.append(f"🚫 {s.error}")
        return "\n".join(lines)

    # ── 后台任务 ──

    async def _task_runner(self, state: TaskState):
        aid = state.album_id
        sid = state.target
        try:
            user_id, is_group, group_id = _parse_target(sid)

            # ── 1. 缓存命中 ──
            cached = self._cache.get(aid)
            if cached and Path(cached.pdf_path).exists():
                state.phases["下载"] = "缓存"
                state.phases["合成"] = "缓存"
                state.phases["上传"] = "上传中"
                await self._notice(sid, f"🔖 {state.job_id} 📤 缓存命中，发送中...")
                from .napcat_stream import send_file_via_stream
                send_result = await send_file_via_stream(
                    self.ctx, sid, user_id, cached.pdf_path,
                    is_group, group_id, self._upload_timeout,
                )
                state.phases["上传"] = "已完成"
                state.phases["发送"] = "已完成"
                state.status = "done"
                state.result = {
                    "title": cached.title,
                    "description": cached.description,
                    "page_count": cached.page_count,
                    "file_size": Path(cached.pdf_path).stat().st_size,
                    "send_result": send_result,
                    "from_cache": True,
                }
                await self._notice(sid, self._completion_notice(state))
                self._cleanup_task(aid)
                return

            # ── 2. 下载 ──
            await self._notice(sid, f"🔖 {state.job_id} ⏬ 下载 #{aid} ...")
            state.phases["下载"] = "进行中"

            def _update_download(*_):
                # jmcomic after_photo 同步回调, 只能存值
                pass

            threads = int(self.plugin_cfg.get("download_threads", 45))
            _, image_dir, images, title, description = _download_images(
                aid, self._download_dir, threads,
            )
            state.phases["下载"] = "已完成"

            # ── 3. 合成 PDF ──
            await self._notice(sid, f"🔖 {state.job_id} 📄 合成 PDF ({len(images)} 页)...")
            state.phases["合成"] = "0%"

            pdf_path = self._cache_dir / f"{aid}.pdf"

            def _pdf_progress(pct: int):
                state.phases["合成"] = f"{pct}%"

            size = _images_to_pdf(images, pdf_path, self._pdf_quality, _pdf_progress)
            _rmtree(image_dir)
            state.phases["合成"] = "已完成"

            page_count = len(images)
            entry = CacheEntry(
                album_id=aid, title=title, description=description,
                page_count=page_count, pdf_path=str(pdf_path.resolve()),
                size_bytes=size, downloaded_at=time.time(),
            )
            evicted = self._cache.put(entry)
            self._evict_cleanup(evicted)

            # ── 4. 上传 NapCat temp ──
            await self._notice(sid, f"🔖 {state.job_id} 📤 上传中...")
            state.phases["合成"] = "已完成"
            state.phases["上传"] = "0%"

            async def _upload_progress(pct: int):
                state.phases["上传"] = f"{pct}%"

            from .napcat_stream import send_file_via_stream
            send_result = await send_file_via_stream(
                self.ctx, sid, user_id, str(pdf_path.resolve()),
                is_group, group_id, self._upload_timeout,
                progress_cb=_upload_progress,
            )
            state.phases["上传"] = "已完成"

            # ── 5. 发送 ──
            state.phases["发送"] = "已完成"

            state.status = "done"
            state.elapsed = time.time() - state.started_at
            state.result = {
                "title": title,
                "description": description,
                "page_count": page_count,
                "file_size": size,
                "send_result": send_result,
                "from_cache": False,
            }

            await self._notice(sid, self._completion_notice(state))

        except Exception as e:
            state.status = "failed"
            state.elapsed = time.time() - state.started_at
            state.error = str(e)
            logger.error(f"#{aid} 后台任务失败: {e}")
            await self._notice(sid, self._completion_notice(state))
        finally:
            self._cleanup_task(aid)

    def _cleanup_task(self, album_id: int):
        self._running_tasks.pop(album_id, None)
        # state 留 registry 供 query_jm_task 查询，上限 30 条
        if len(self._task_registry) > 30:
            for key in list(self._task_registry)[:-30]:
                self._task_registry.pop(key, None)

    def _completion_notice(self, s: TaskState) -> str:
        p = s.phases
        if s.status == "done":
            r = s.result
            ml = self._desc_max_length
            desc = r.get("description", "")
            if len(desc) > ml:
                desc = desc[:ml] + "..."
            return (
                f"🔔 任务完成 {s.job_id}\n"
                f"下载: {p['下载']} | 合成: {p['合成']} | 上传: {p['上传']} | 发送: {p['发送']}\n"
                f"📖 {r.get('title', '')}\n"
                f"📝 {desc or '无描述'}\n"
                f"📄 {r.get('page_count', 0)} 页  💾 {self._fmt(r.get('file_size', 0))}  ⏱ {s.elapsed:.0f}s"
            )
        # failed
        return (
            f"🔔 任务失败 {s.job_id}\n"
            f"下载: {p['下载']} | 合成: {p['合成']} | 上传: {p['上传']} | 发送: {p['发送']}\n"
            f"🚫 {s.error}"
        )

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
