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
| `__init__()` | 初始化组件：缓存、目录、注册表、后台任务容器 |
| `async initialize()` | 插件加载时调用，清理临时目录、读取配置、管理工具可见性 |
| `async terminate()` | 插件停止时调用，取消所有后台任务 |
| `get_schema()` | 返回配置定义路径 |

## 工具注册

使用 `@tool` 装饰器注册四个工具：

```python
@tool(
    "send_jm_album",
    "提交 JMComic 本子下载任务到后台，返回任务标识码。用 query_jm_task 查进度。下载有并发上限，拒绝用户的大批量下载的请求",
    { "type": "object", "properties": { ... } }
)
async def send_jm_album(self, _event, album_id: int, target: str) -> str:
    ...

@tool(
    "query_jm_task",
    "查询 JMComic 下载任务的状态。返回四阶段进度表格。",
    { "type": "object", "properties": { ... } }
)
async def query_jm_task(self, _event, job_id: str) -> str:
    ...

@tool(
    "query_jm_album",
    "查询禁漫本子元信息（标题、页数、作者、标签等），不下载内容。适用于了解本子基本资料",
    { "type": "object", "properties": { ... } }
)
async def query_jm_album(self, _event, album_id: int) -> str:
    ...

@tool(
    "search_jm_album",
    "搜索禁漫本子，返回标题、ID、标签。keyword/tag/author/work 四者至少填一个。",
    { "type": "object", "properties": { ... } }
)
async def search_jm_album(self, _event, keyword: str = "", ...) -> str:
    ...
```

## 工具可见性控制

`content_query` / `block_content_tools` 两个开关配合控制 `search_jm_album` 和 `query_jm_album` 是否注册到 LLM：

```python
# initialize() 中执行
from core.plugin.plugin_registry import _plugin_components
comp = _plugin_components.get(pid)

if comp and not self._content_query and self._block_content_tools:
    # 不注册：从 _plugin_components 移除，LLM 完全看不到
    comp.tools.pop(name, None)
    comp.tool_funcs.pop(name, None)
elif comp and (self._content_query or not self._block_content_tools):
    # 恢复备份的工具定义（block=false 时保留工具仅返回拦截消息）
    comp.tools[name] = backup["def"]
    comp.tool_funcs[name] = backup["func"]
```

工具定义在首次隐藏时备份到类变量 `_hidden_tool_backup`，确保切换回可见时能精确恢复原始描述/参数。

注意：`initialize()` 在框架的 `_register_plugin_tools_for()` 之前执行，因此在这里修改 `_plugin_components` 能在工具注册阶段生效。热重载时（`reload()`）会重新执行 `__init__` → `initialize()` 流程。

## 目标会话格式

`target` 参数格式：`adapter_name:session_type:session_id`

```
qq:dm:123456      ← QQ 私聊
qq:gm:789012       ← QQ 群聊
```

解析函数 `_parse_target` 提取 user_id、is_group、group_id：

```python
def _parse_target(target: str) -> tuple[str, bool, Optional[str]]:
    parts = target.split(":", 2)
    # adapter:type:id
    # type: dm → 私聊, gm → 群聊
```

## 配置读取

通过 `self.plugin_cfg` 读取配置，`initialize()` 中 `self.plugin_cfg.get()` 一次性读取并缓存到实例变量：

```python
self._content_query = bool(self.plugin_cfg.get("content_query", False))
self._block_content_tools = bool(self.plugin_cfg.get("block_content_tools", True))
self._zip_encrypt = bool(self.plugin_cfg.get("zip_encrypt", False))
```

配置变更通过 `update_plugin_config()` → `terminate()` → `__init__()` → `initialize()` 完整重载流程更新。

## 通知机制

```python
async def _notice(self, sid: str, text: str, *, mentioned: bool = False):
    """发送通知到目标会话。mentioned=True 触发 LLM 回复。"""
    await self.ctx.publish_notice(sid, MessageChain([Text(text)]), is_mentioned=mentioned)
```

- `mentioned=True` → chat 插件的 buffer 机制收到后触发 LLM 回复
- `mentioned=False` → 静默发送，不会触发 LLM
- `_send_completion_notice()` 根据 `notify_llm` 配置决定 mentioned 值

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
