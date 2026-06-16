# 插件系统集成

## 入口

`__init__.py` 导出 `JMdownPlugin`，KiraAI 的插件加载器自动发现。

```python
from .main import JMdownPlugin
__all__ = ["JMdownPlugin"]
```

## 插件基类

`JMdownPlugin` 继承 `BasePlugin`，必须实现：

| 方法 | 说明 |
|------|------|
| `__init__()` | 初始化组件：缓存、目录、注册表 |
| `async on_load()` | 插件加载时调用，清理临时目录 |
| `async on_stop()` | 插件停止时调用，终止所有后台任务 |
| `get_schema()` | 返回配置定义路径 |

## 工具注册

使用 `@tool` 装饰器注册两个工具：

```python
@tool(
    "send_jm_album",
    "提交 JMComic 本子下载任务到后台，返回任务标识码",
    { "type": "object", "properties": { ... } }
)
async def send_jm_album(self, _event, album_id: int, target: str) -> str:
    ...

@tool(
    "query_jm_task",
    "查询后台下载任务状态",
    { "type": "object", "properties": { ... } }
)
async def query_jm_task(self, _event, job_id: str) -> str:
    ...
```

## 目标会话格式

`target` 参数格式：`adapter_name:session_type:session_id`

```
qq:dm:2263130787      ← QQ 私聊
qq:gm:943393726       ← QQ 群聊
```

解析函数 `_parse_target` 提取 user_id、is_group、group_id：

```python
def _parse_target(target: str) -> tuple:
    parts = target.split(":", 2)
    # adapter:type:id
    # type: dm → 私聊, gm → 群聊
```

## 配置读取

通过 `self.plugin_cfg` 读取配置，`self._refresh_config()` 刷新：

```python
self._notify_llm = bool(self.plugin_cfg.get("notify_llm", True))
```

配置变更通过 `on_config_change` 回调通知，但需手动调用 `_refresh_config()` 更新本地缓存。

## 通知机制

```python
async def _notice(self, sid: str, text: str, *, mentioned: bool = False):
    """发送通知到目标会话。mentioned=True 触发 LLM 回复。"""
    await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=mentioned)
```

- `mentioned=True` → chat 插件的 buffer 机制收到后触发 LLM 回复
- `mentioned=False` → 静默发送，不会触发 LLM

## 适配器查找

动态查找 QQ adapter，不依赖硬编码注册名：

```python
def _find_qq_adapter(adapter_mgr):
    adapters = adapter_mgr.get_adapters()
    for name, inst in adapters.items():
        if inst.info.platform.upper() == "QQ":
            return inst
```

这样即使 QQ adapter 的注册名变更也能正常工作。
