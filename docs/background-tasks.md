# 后台任务系统

## 为什么需要

KiraAI tool 响应有约 60s 超时限制。下载 JMComic + 合成 PDF + 上传大文件远超此限制。后台 task + 状态查询 + 异步通知的模式完全绕过此限制。

## 数据结构

```python
@dataclass
class TaskState:
    job_id: str          # JOB-YYMMDD-NNN
    album_id: int        # 本子 ID
    target: str          # 目标会话
    status: str          # running / done / failed
    phases: Dict[str, str]  # 四阶段状态
    result: Optional[dict]  # 完成结果（含 password、linked_episodes 等）
    error: Optional[str]    # 错误信息
    started_at: float    # 时间戳
    elapsed: float       # 耗时（完成时写入）
```

## 四阶段状态追踪

| 阶段 key | 可能的值 |
|----------|----------|
| 下载 | 排队中 / 进行中 / 0%-100%(速度) / 已完成 / 缓存 / 失败 |
| 合成 | 排队中 / 0%-100% / 已完成 / 缓存 / ZIP |
| 上传 | 排队中 / 0%-100%(速度) / 已完成 |
| 发送 | 排队中 / 已完成 |

`query_jm_task` 返回示例：

```
[完成] JOB-YZMMDD-001
下载: 已完成 | 合成: 已完成 | 上传: 已完成 | 发送: 已完成
耗时: 45s
标题: [Miyako] MY ROOMMATE 2 (EP.6-9)
描述: 无描述
页数: 35  大小: 8.5 MB
```

## 去重机制

同一 album_id 正在运行则复用已有任务。超过 `upload_timeout + 120s` 视为死任务，取消旧任务允许重新提交：

```python
if task and not task.done():
    elapsed = time.time() - state.started_at
    if elapsed > self._upload_timeout + 120:
        task.cancel()           # 死任务，允许重新提交
    else:
        return f"#{album_id} 已在下载队列中，标识码: {existing.job_id}"
```

## 并发限制

`max_concurrent` 配置控制同时最多任务数：

```python
if len(self._running_tasks) >= self._max_concurrent:
    return f"当前下载任务过多（{n}/{self._max_concurrent}），请等待现有任务完成后再试"
```

## 缓存命中流程

```python
if cached and Path(cached.pdf_path).exists():
    # 跳过下载和合成阶段
    # zip_encrypt=true 时额外走 ZIP 加密
    # 直接走 上传 → 发送 → 完成通知
```

缓存命中时 phases 显示：

```
下载: 缓存 | 合成: 缓存 | 上传: 0%(xx/s) | 发送: ...
```

缓存命中但开启了 `zip_encrypt` 时：

```
下载: 缓存 | 合成: ZIP | 上传: 0%(xx/s) | 发送: ...
```

## ZIP / 加密

`zip_encrypt` 开启时，PDF 被压缩为 AES-256 加密的 ZIP 文件再上传。密码来源：

| `custom_password` | 行为 |
|---|---|
| 空字符串 `""` | 自动生成 16 位随机强密码 |
| 非空字符串 | 直接使用 |

密码在完成通知中返回，`query_jm_task` 也能查到。

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
def _cleanup_task(self, state: TaskState):
    self._running_tasks.pop(state.album_id, None)
    # 保留 registry 条目供查询，上限 30 条
    if len(self._task_registry) > 30:
        for key in list(self._task_registry)[:-30]:
            self._task_registry.pop(key, None)
```

- `_running_tasks`：`dict[album_id, asyncio.Task]`，运行时集合，任务完成后移除
- `_task_registry`：`dict[job_id, TaskState]`，历史记录，保留最近 30 条
- `_cleanup_task` 在 `finally` 块中执行，确保异常路径也能清理
