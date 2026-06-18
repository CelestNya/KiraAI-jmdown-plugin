# 配置参考

## 配置项一览

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_cache` | `integer` | 10 | 最多缓存几本 PDF |
| `desc_max_length` | `integer` | 80 | 描述截取字符数 |
| `download_threads` | `integer` | 45 | 下载图片的并行线程数 |
| `max_concurrent` | `integer` | 2 | 同时最多下载任务数 |
| `chunk_size` | `integer` | 524288 | Stream 分片字节数，默认 512KB |
| `pdf_quality` | `integer` | 85 | JPEG 质量 (1-100) |
| `upload_timeout` | `integer` | 300 | 上传超时秒数 |
| `notify_llm` | `switch` | true | 完成后是否触发 LLM 回复 |
| `content_query` | `switch` | false | 允许搜索和查看本子元信息 |
| `block_content_tools` | `switch` | true | content_query 关闭时：true=不注册，false=拦截提示 |
| `zip_encrypt` | `switch` | false | 开启后加密 ZIP（AES-256），关闭后直接发 PDF |
| `custom_password` | `string` | "" | 自定义密码，留空自动随机生成 |

## schema.json

```json
{
    "max_cache": {
        "type": "integer",
        "default": 10,
        "hint": "最多缓存几本 PDF，超限自动删最旧"
    },
    "desc_max_length": {
        "type": "integer",
        "default": 80,
        "hint": "描述截取字符数"
    },
    "download_threads": {
        "type": "integer",
        "default": 45,
        "hint": "下载图片的并行线程数"
    },
    "max_concurrent": {
        "type": "integer",
        "default": 2,
        "hint": "同时最多下载任务数，防止大规模批量下载占用资源"
    },
    "chunk_size": {
        "type": "integer",
        "default": 524288,
        "hint": "Stream 上传分片字节数，默认 512KB。调整需重启，建议 512KB-4MB"
    },
    "pdf_quality": {
        "type": "integer",
        "default": 85,
        "hint": "JPEG 质量 (1-100)，越高文件越大画质越好"
    },
    "upload_timeout": {
        "type": "integer",
        "default": 300,
        "hint": "上传超时秒数（大文件建议 300+）"
    },
    "notify_llm": {
        "type": "switch",
        "default": true,
        "hint": "任务完成后是否在目标会话触发 LLM 自动回复"
    },
    "content_query": {
        "type": "switch",
        "default": false,
        "hint": "允许搜索和查看本子元信息。关闭后 search_jm_album 和 query_jm_album 受下方开关控制"
    },
    "block_content_tools": {
        "type": "switch",
        "default": true,
        "hint": "当 content_query=关闭 时：true=直接不注册工具，LLM 完全看不到；false=保留工具但调用时返回「已关闭」提示。content_query=开启时此开关无效，工具正常可用"
    },
    "zip_encrypt": {
        "type": "switch",
        "default": false,
        "hint": "开启后压缩为加密 ZIP（AES-256），绕过 QQ 内容审查。关闭后直接发送原始 PDF，不打包不压缩"
    },
    "custom_password": {
        "type": "string",
        "default": "",
        "hint": "自定义加密密码。留空则自动生成随机强密码（需开启 zip_encrypt 才生效）"
    }
}
```

## 配置说明

### notify_llm

`type: "switch"` 是 KiraAI 配置系统的开关类型，渲染为 UI 开关而非 checkbox。控制完成/失败通知是否附带 `is_mentioned=True`，影响 chat 插件是否触发 LLM 回复。

- `true`：通知携带 mention，LLM 可能对结果做出回应
- `false`：静默通知，不触发 LLM

### content_query / block_content_tools

两个开关配合控制搜索和元查询工具的行为：

| content_query | block_content_tools | 行为 |
|:---:|:---:|---|
| 开启 | 任意 | `search_jm_album` / `query_jm_album` 正常注册 |
| 关闭 | true | 工具不注册，LLM 完全看不到 |
| 关闭 | false | 工具保留，调用时返回"已关闭"提示 |

### max_cache

FIFO 队列。新条目超出上限时，删除最早下载的条目。索引持久化到 `cache_index.json`。同时清理对应的 ZIP 文件。

### zip_encrypt / custom_password

开启 `zip_encrypt` 后，PDF 会被压缩为 AES-256 加密的 ZIP 文件再上传。`custom_password` 留空时自动生成 16 位随机强密码（含特殊字符），有内容则直接使用。

### pdf_quality

控制将图片转为 JPEG 时的质量参数。值越低文件越小但画质损失越大。85 是 libjpeg 的推荐默认值，在文件大小和质量间取得平衡。

### upload_timeout

NapCat Stream API 的单次 `send_action` 超时。大文件（50MB+）上传可能需要 300s 以上，建议按文件大小调整。外层 `asyncio.wait_for` 做硬超时兜底。

## 运行时配置目录

```
data/plugin_data/jmdown/
├── cache/               # PDF 缓存 + ZIP 文件
├── downloads/           # 下载临时目录（启动时清理孤立文件）
└── cache_index.json     # 缓存索引
```

## KiraAI 配置

插件数据目录通过 `self.ctx.get_plugin_data_dir()` 获取，自动映射到 `data/plugin_data/jmdown/`。PDF 写入 `cache/` 子目录，下载写入 `downloads/` 子目录。
