# 配置参考

## 配置项一览

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_cache` | `integer` | 10 | 最多缓存几本 PDF |
| `desc_max_length` | `integer` | 80 | 描述截取字符数 |
| `download_threads` | `integer` | 45 | 下载图片的并行线程数 |
| `pdf_quality` | `integer` | 85 | JPEG 质量 (1-100) |
| `upload_timeout` | `integer` | 300 | 上传超时秒数 |
| `notify_llm` | `switch` | true | 完成后是否触发 LLM 回复 |

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
    "pdf_quality": {
        "type": "integer",
        "default": 85,
        "hint": "JPEG 质量 (1-100)"
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
    }
}
```

## 配置说明

### notify_llm

`type: "switch"` 是 KiraAI 配置系统的开关类型，渲染为 UI 开关而非 checkbox。控制完成/失败通知是否附带 `is_mentioned=True`，影响 chat 插件是否触发 LLM 回复。

- `true`：通知携带 mention，LLM 可能对结果做出回应
- `false`：静默通知，不触发 LLM

### max_cache

FIFO 队列。新条目超出上限时，删除最早下载的条目。索引持久化到 `cache_index.json`。如果 PDF 文件已被外部删除，启动时清理索引中无效条目。

### pdf_quality

控制将图片转为 JPEG 时的质量参数。值越低文件越小但画质损失越大。85 是 libjpeg 的推荐默认值，在文件大小和质量间取得平衡。

### upload_timeout

NapCat Stream API 的单次 `send_action` 超时。大文件（50MB+）上传可能需要 300s 以上，建议按文件大小调整。

## 运行时配置目录

```
data/plugin_data/jmdown/
├── cache/               # PDF 缓存
├── download/            # 下载临时目录（运行后清空）
└── cache_index.json     # 缓存索引
```

## KiraAI 配置

插件数据目录通过 `self.ctx.get_data_path("jmdown")` 获取，自动映射到 `data/plugin_data/jmdown/`。PDF 写入 `cache/` 子目录，下载写入 `download/` 子目录。
