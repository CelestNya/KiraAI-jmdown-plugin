"""
JMComic 下载器 — KiraAI 插件

流程: 下载本子 → 合 PDF → 删原图 → 缓存管理
最终返回标题、描述、页数、PDF 路径.
"""

from __future__ import annotations

import asyncio
import logging
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


class JMDownError(RuntimeError):
    """JMdown 插件自定义错误"""
    pass


@dataclass
class TaskState:
    """后台任务状态"""
    job_id: str
    album_id: int
    target: str
    status: str = "running"  # running | done | failed
    phases: dict = field(default_factory=lambda: {
        "下载": "排队中", "合成": "排队中", "上传": "排队中", "发送": "排队中",
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


# ── 元信息查询 ──

def _fetch_album_meta(album_id: int) -> dict:
    """查询本子元信息, 不下载内容. 返回 dict."""
    try:
        client = jmcomic.JmOption.default().build_jm_client()
        album = client.get_album_detail(album_id)
    except jmcomic.jm_exception.MissingAlbumPhotoException:
        raise JMDownError("该号码对应的本子不存在")
    except Exception as e:
        raise JMDownError(f"查询失败: {e}") from e

    # 挂载的其他章节（与本子不同 ID 的额外内容）
    episodes = getattr(album, "episode_list", [])
    linked = []
    for ep_id, ep_idx, ep_title in episodes:
        if str(ep_id) != str(album_id):
            linked.append({"id": int(ep_id), "index": ep_idx, "title": ep_title or ""})

    # 主本子自身页数 = 第一章节的图片数
    first_ep = next(iter(album), None)
    main_page_count = len(first_ep) if first_ep is not None else 0

    return {
        "album_id": album_id,
        "title": getattr(album, "name", str(album_id)),
        "description": getattr(album, "description", ""),
        "page_count": main_page_count,
        "episode_count": len(episodes),
        "linked_episodes": linked,      # 额外挂载的本子，供 LLM 决定是否下载
        "authors": list(getattr(album, "authors", [])),
        "tags": list(getattr(album, "tags", [])),
        "likes": getattr(album, "likes", ""),
        "views": getattr(album, "views", ""),
        "comment_count": getattr(album, "comment_count", 0),
        "pub_date": getattr(album, "pub_date", ""),
        "update_date": getattr(album, "update_date", ""),
    }


# ── 下载 & PDF ──

def _download_images(album_id: int, download_dir: Path, threads: int = 45) -> tuple:
    """下载图片, 返回 (album_obj, image_dir, images[], title, desc)."""
    opt = jmcomic.JmOption.default()
    opt.dir_rule.base_dir = str(download_dir.resolve())
    # Bd_Aid: 按 album_id 建目录，不依赖标题
    opt.dir_rule.rule_dsl = "Bd_Aid"
    opt.dir_rule.parser_list = opt.dir_rule.get_rule_parser_list("Bd_Aid")
    opt.download.image.suffix = ".jpg"
    opt.download.image.decode = True
    opt.download.threading.image = threads
    opt.client.retry_times = 3
    opt.plugins.after_album = []

    try:
        # 先 clent 直查 album, 在当前线程捕获 MissingAlbumPhotoException
        # (download_album 在多线程内部抛的异常传不出来)
        opt.build_jm_client().get_album_detail(album_id)
    except jmcomic.jm_exception.MissingAlbumPhotoException:
        raise JMDownError("该号码对应的本子不存在")
    except Exception as e:
        raise JMDownError(f"获取本子信息失败: {e}") from e

    # 确认存在后再进下载(会开线程池, 异常出不来的)
    try:
        result_set = opt.download_album([album_id])
    except Exception as e:
        raise JMDownError(f"下载失败: {e}") from e
    if not result_set:
        raise JMDownError("该号码对应的本子不存在或下载失败")

    album_obj = next(iter(result_set))[0]
    title = getattr(album_obj, "name", str(album_id))
    description = getattr(album_obj, "description", "")

    # 只读主本子目录，不管 jmcomic 挂了哪些额外章节
    image_dir = download_dir / str(album_id)
    if not image_dir.is_dir():
        # 有时 jmcomic 会按 episode_id 建额外目录，删掉它们
        for d in list(download_dir.iterdir()):
            if d.is_dir() and d.name != str(album_id):
                _rmtree(d)
        if not image_dir.is_dir():
            raise JMDownError(f"下载目录不存在: {image_dir}")

    valid = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    images = sorted(
        p for p in image_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in valid
    )
    # 页数只算主本子的章节（episode_list[0] 即本子本身）
    first_ep = next(iter(album_obj), None)
    expected = len(first_ep) if first_ep is not None else 0
    if expected > 0 and len(images) != expected:
        raise JMDownError(f"页数不匹配: 实际 {len(images)} 张, 预期 {expected} 张")
    if not images:
        raise JMDownError(f"下载完成但目录中无图片文件: {image_dir}")
    # 清理 jmcomic 额外建的章节目录
    for d in list(download_dir.iterdir()):
        if d.is_dir() and d.name != str(album_id):
            _rmtree(d)

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


def _linked_episodes(album) -> list[dict]:
    """提取 album 里挂载的额外本子信息。"""
    episodes = getattr(album, "episode_list", [])
    album_id = getattr(album, "album_id", None)
    linked = []
    for ep_id, ep_idx, ep_title in episodes:
        if str(ep_id) != str(album_id):
            linked.append({"id": int(ep_id), "index": ep_idx, "title": ep_title or ""})
    return linked


def _rmtree(dir_path: Path):
    if not dir_path.exists():
        return
    for child in dir_path.iterdir():
        if child.is_symlink():
            child.unlink()
        elif child.is_dir():
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
        self._chunk_size: int = 512 * 1024

        # 后台任务系统
        self._task_registry: dict[str, TaskState] = {}   # job_id → state
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
        self._upload_timeout = max(1, int(self.plugin_cfg.get("upload_timeout", 300)))
        self._chunk_size = min(
            16 * 1024 * 1024,  # NapCat WS 帧上限
            max(4096, int(self.plugin_cfg.get("chunk_size", 512 * 1024))),
        )
        self._notify_llm = bool(self.plugin_cfg.get("notify_llm", True))
        self._cache = CacheIndex(self._data_dir / "cache_index.json", self._max_cache)
        self._clean_orphans()

        # 静音 jmcomic 的冗赘日志（下载进度、API 报错等）
        logging.getLogger("jmcomic").setLevel(logging.WARNING)

        logger.info("JMdown 就绪")

    async def terminate(self):
        tasks = [t for t in self._running_tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            # 等被 cancel 的任务真正退出, 吞掉 CancelledError
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running_tasks.clear()
        logger.info("JMdown 已终止")

    async def _notice(self, sid: str, text: str, *, mentioned: bool = False):
        """通过会话发送进度通知。mentioned=True 会触发目标会话 LLM 回复。"""
        if not sid:
            return
        try:
            await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=mentioned)
        except Exception:
            pass  # 通知失败不中断主流程

    async def _send_completion_notice(self, sid: str, state: TaskState):
        """notify_llm=true 则触发 LLM 回复，false 仅静默通知。"""
        if self._notify_llm:
            await self._notice(sid, self._completion_notice(state), mentioned=True)
        else:
            await self._notice(sid, self._completion_notice(state), mentioned=False)

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
            existing = next(
                (s for s in self._task_registry.values()
                 if s.album_id == album_id and s.status == "running"),
                None,
            )
            if existing:
                return f"#{album_id} 已在下载队列中，标识码: {existing.job_id}"
            return f"#{album_id} 已在下载队列中"

        # 生成 Job ID
        self._task_counter += 1
        job_id = f"JOB-{datetime.now().strftime('%y%m%d')}-{self._task_counter:03d}"

        try:
            _parse_target(target)  # 提前校验 target 格式
        except JMDownError as e:
            return f"错误: {e}"

        state = TaskState(job_id=job_id, album_id=album_id, target=target)
        self._task_registry[job_id] = state

        task = asyncio.create_task(self._task_runner(state))
        self._running_tasks[album_id] = task

        logger.info(f"#{album_id} 入队列 → {job_id}")
        return f"任务已加入队列\n标识码: {job_id}"

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
        state = self._task_registry.get(job_id)
        if state:
            return self._format_state(state)
        return f"未找到任务: {job_id}"

    # ── 工具: 查询本子元信息 ──

    @tool(
        "query_jm_album",
        "查询禁漫本子元信息（标题、页数、作者、标签等），不下载内容。适用于了解本子基本资料",
        {
            "type": "object",
            "properties": {
                "album_id": {
                    "type": "integer",
                    "description": "禁漫本子数字 ID"
                }
            },
            "required": ["album_id"]
        }
    )
    async def query_jm_album(self, _event, album_id: int) -> str:
        if album_id <= 0:
            return "错误: album_id 须为正整数"
        try:
            info = _fetch_album_meta(album_id)
        except JMDownError as e:
            return f"错误: {e}"
        return self._format_album_info(info)

    def _format_album_info(self, info: dict) -> str:
        ml = self._desc_max_length
        desc = info.get("description", "")
        if len(desc) > ml:
            desc = desc[:ml] + "..."
        lines = [
            f"#{info['album_id']}",
            f"标题: {info['title']}",
        ]
        # jmcomic 可能返回 ["N/A"], 过滤掉
        authors = [a for a in info.get("authors", []) if a not in ("", "N/A", "none")]
        if authors:
            lines.append(f"作者: {', '.join(authors)}")
        tags = info.get("tags", [])
        if tags:
            lines.append(f"标签: {', '.join(tags[:10])}{'...' if len(tags) > 10 else ''}")
        pc = info.get("page_count", 0)
        ep = info.get("episode_count", 0)
        lines.append(f"页数: {pc if pc > 0 else '未知'}  章节: {ep}")
        linked = info.get("linked_episodes", [])
        if linked:
            ep_info = "  |  ".join(
                f"#{e['id']} ({e.get('title','') or '?'})"
                for e in linked
            )
            lines.append(f"挂载章节: {ep_info}")
            lines.append("(这些是作为该本子章节挂载的其他本子号，如有需要可单独下载)")
        if info.get("likes") or info.get("views"):
            likes = info.get("likes", "")
            views = info.get("views", "")
            comments = info.get("comment_count", 0)
            lines.append(f"喜欢: {likes}  观看: {views}  评论: {comments}")
        pub = info.get("pub_date", "")
        upd = info.get("update_date", "")
        if pub and pub != "0":
            lines.append(f"发布: {pub}{'  更新: ' + upd if upd and upd != '0' else ''}")
        if desc:
            lines.append(f"---\n{desc}")
        lines.append(
            "---\n注: 若用户无特别要求，请不要给用户输出格式化文本或\"系统通知\""
        )
        return "\n".join(lines)

    def _format_state(self, s: TaskState) -> str:
        p = s.phases
        elapsed = time.time() - s.started_at
        lines = [
            f"{'[完成]' if s.status == 'done' else '[失败]' if s.status == 'failed' else '[进行中]'} {s.job_id}",
            f"下载: {p['下载']} | 合成: {p['合成']} | 上传: {p['上传']} | 发送: {p['发送']}",
            f"耗时: {elapsed:.0f}s",
        ]
        if s.status == "done" and s.result:
            r = s.result
            ml = self._desc_max_length
            desc = r.get("description", "")
            if len(desc) > ml:
                desc = desc[:ml] + "..."
            linked = r.get("linked_episodes", [])
            ep_str = ("  挂载: " + ", ".join(f"#{e['id']}" for e in linked)) if linked else ""
            lines.extend([
                f"标题: {r.get('title', '')}",
                f"描述: {desc or '无描述'}",
                f"页数: {r.get('page_count', 0)}  大小: {self._fmt(r.get('file_size', 0))}{ep_str}",
            ])
        if s.status == "failed" and s.error:
            lines.append(f"错误: {s.error}")
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
                state.phases["上传"] = "0%"
                await self._notice(sid, f"缓存命中 [{state.job_id}], 发送中...")

                async def _cache_upload_progress(pct: int, spd: str):
                    state.phases["上传"] = f"{pct}% ({spd})"

                from .napcat_stream import send_file_via_stream
                send_result = await send_file_via_stream(
                    self.ctx, sid, user_id, cached.pdf_path,
                    is_group, group_id, self._upload_timeout,
                    progress_cb=_cache_upload_progress,
                    chunk_size=self._chunk_size,
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
                await self._send_completion_notice(sid, state)
                return

            # ── 2. 下载 ──
            await self._notice(sid, f"[{state.job_id}] 下载 #{aid} ...")
            state.phases["下载"] = "进行中"

            def _update_download(*_):
                # jmcomic after_photo 同步回调, 只能存值
                pass

            threads = int(self.plugin_cfg.get("download_threads", 45))
            # jmcomic 同步阻塞 + 自建线程池, 丢到线程避免冻结事件循环 (ctrl+c 才能打断)
            album_obj, image_dir, images, title, description = await asyncio.to_thread(
                _download_images, aid, self._download_dir, threads,
            )
            state.phases["下载"] = "已完成"

            # ── 3. 合成 PDF ──
            await self._notice(sid, f"[{state.job_id}] 合成 PDF ({len(images)} 页)...")
            state.phases["合成"] = "0%"

            pdf_path = self._cache_dir / f"{aid}.pdf"

            def _pdf_progress(pct: int):
                state.phases["合成"] = f"{pct}%"

            size = await asyncio.to_thread(
                _images_to_pdf, images, pdf_path, self._pdf_quality, _pdf_progress,
            )
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
            await self._notice(sid, f"[{state.job_id}] 上传中...")
            state.phases["上传"] = "0%"

            async def _upload_progress(pct: int, spd: str):
                state.phases["上传"] = f"{pct}% ({spd})"

            from .napcat_stream import send_file_via_stream
            send_result = await send_file_via_stream(
                self.ctx, sid, user_id, str(pdf_path.resolve()),
                is_group, group_id, self._upload_timeout,
                progress_cb=_upload_progress,
                chunk_size=self._chunk_size,
            )
            state.phases["上传"] = "已完成"

            # ── 5. 发送 ──
            state.phases["发送"] = "已完成"

            state.status = "done"
            state.elapsed = time.time() - state.started_at
            linked = _linked_episodes(album_obj)
            state.result = {
                "title": title,
                "description": description,
                "page_count": page_count,
                "file_size": size,
                "send_result": send_result,
                "from_cache": False,
                "linked_episodes": linked,
            }

            await self._send_completion_notice(sid, state)

        except Exception as e:
            state.status = "failed"
            state.elapsed = time.time() - state.started_at
            state.error = str(e)
            logger.error(f"#{aid} 后台任务失败: {e}")
            await self._send_completion_notice(sid, state)
        finally:
            self._cleanup_task(state)

    def _cleanup_task(self, state: TaskState):
        self._running_tasks.pop(state.album_id, None)
        # state 留 registry 供 query_jm_task 查询，按 job_id 上限 30 条
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
            linked = r.get("linked_episodes", [])
            extra = ""
            if linked:
                ep_str = ", ".join(f"#{e['id']}" for e in linked)
                extra = f"挂载章节: {ep_str}\n"
            return (
                f"任务 [{s.job_id}] #{s.album_id} 全部完成\n"
                f"下载: {p['下载']} | 合成: {p['合成']} | 上传: {p['上传']} | 发送: {p['发送']}\n"
                f"标题: {r.get('title', '')}\n"
                f"描述: {desc or '无描述'}\n"
                f"{extra}"
                f"页数: {r.get('page_count', 0)}  大小: {self._fmt(r.get('file_size', 0))}  耗时: {s.elapsed:.0f}s\n"
                f"---\n"
                f"注: 若用户无特别要求，请不要给用户输出格式化文本或\"系统通知\""
            )
        # failed
        return (
            f"任务 [{s.job_id}] #{s.album_id} 失败\n"
            f"下载: {p['下载']} | 合成: {p['合成']} | 上传: {p['上传']} | 发送: {p['发送']}\n"
            f"错误: {s.error}"
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
