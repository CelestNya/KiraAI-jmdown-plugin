# 后台任务系统

## 为什么需要

KiraAI tool 响应有约 60s 超时限制。下载 JMComic + 合成 PDF + 上传大文件远超此限制。后台任务 + 状态查询 + 异步通知的模式完全绕过此限制。

## 数据结构

```python
@dataclass
class TaskState:
    job_id: str          # JOB-YYMMDD-NNN
    album_id: int        # 本子 ID
    target: str          # 目标会话
    status: str          # running / done / failed
    phases: Dict[str, str]  # 四阶段状态
    result: Optional[dict]  # 完成结果
    error: Optional[str]    # 错误信息
    started_at: float    # 时间戳
```

## 四阶段状态追踪

| 阶段 key | 可能的值 |
|----------|----------|
| 下载 | 进行中 / 已完成 / 缓存 / 失败 |
| 合成 | 0%-100% / 已完成 / 缓存 |
| 上传 | 0%-100%(速度) / 已完成 |
| 发送 | 已完成 |

`query_jm_task` 返回示例：

```
[完成] JOB-241215-001
下载: 已完成 | 合成: 已完成 | 上传: 已完成 | 发送: 已完成
耗时: 45s
标题: [Miyako] MY ROOMMATE 2 (EP.6-9)
描述: 无描述
页数: 35  大小: 8.5 MB
```

## 去重机制

```python
if album_id in self._running_tasks and not self._running_tasks[album_id].done():
    existing = self._task_registry.get(album_id)
    if existing:
        return f"#{album_id} 已在下载队列中，标识码: {existing.job_id}"
```

同一 album_id 正在运行则复用已有任务，不重复提交。

## 缓存命中流程

```python
if cached and Path(cached.pdf_path).exists():
    # 跳过下载和合成阶段
    # 直接走 上传 → 发送 → 完成通知
```

缓存命中时 phases 显示：

```
下载: 缓存 | 合成: 缓存 | 上传: 0%(xx/s) | 发送: ...
```

## 错误处理

所有异常被 `_task_runner` 的 `except Exception` 捕获：

```python
except Exception as e:
    state.status = "failed"
    state.error = str(e)
    await self._send_completion_notice(sid, state)
```

特定异常友好消息：

| 异常 | 显示 |
|------|------|
| `MissingAlbumPhotoException` | "该号码对应的本子不存在" |
| 其他 | "下载失败: {error_detail}" |

## 资源清理

```python
def _cleanup_task(self, album_id: int):
    self._running_tasks.pop(album_id, None)
    # 保留 registry 条目供查询，上限 30 条
    if len(self._task_registry) > 30:
        for key in list(self._task_registry)[:-30]:
            self._task_registry.pop(key, None)
```

- `_running_tasks`：运行时集合，任务完成后移除
- `_task_registry`：历史记录，保留最近 30 条
