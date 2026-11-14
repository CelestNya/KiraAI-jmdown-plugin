# 配置参考

## 配置项一览

| 分组 | 参数 | 类型 | 默认值 | 说明 |
|------|------|------|--------|------|
| 下载 | `download_threads` | `integer` | 45 | 下载图片的并行线程数 |
| | `max_concurrent` | `integer` | 2 | 同时最多下载任务数 |
| 内容审核 | `content_query` | `switch` | false | 允许搜索和查看本子元信息 |
| | `block_content_tools` | `switch` | true | content_query 关闭时：true=不注册，false=拦截提示 |
| | `allow_cross_session` | `switch` | false | 允许转发到其他会话，关闭时 target 只能设为当前会话 |
| 加密 | `zip_encrypt` | `switch` | false | 开启后加密 ZIP（AES-256），关闭后直接发 PDF |
| | `custom_password` | `string` | "" | 自定义密码，留空自动随机生成 |
| 质量 | `pdf_quality` | `integer` | 85 | JPEG 质量 (1-100) |
| 上传 | `upload_method` | `string` | path | 三选一：path / base64 / stream，下拉 hint 逐项说明 |
| | `max_file_mb` | `integer` | 0 | 插件侧文件大小上限（MB），0 = 不限制 |
| | `upload_timeout` | `integer` | 300 | 上传超时秒数（path / base64 的总超时；stream 的单片超时=1/10） |
| | `chunk_size` | `integer` | 524288 | 仅 stream 生效：分片字节数，默认 512KB |
| 缓存 | `max_cache` | `integer` | 10 | 最多缓存几本 PDF |
| 通知 | `notify_llm` | `switch` | true | 完成后是否触发 LLM 回复 |

## schema.json

配置项按功能分组（`type: "section"`），子字段嵌套在 `fields` 中：

```json
{
    "download": {
        "type": "section",
        "name": "下载",
        "hint": "下载相关配置",
        "collapsed": false,
        "fields": {
            "download_threads": {
                "type": "integer",
                "default": 45,
                "hint": "下载图片的并行线程数"
            },
            "max_concurrent": {
                "type": "integer",
                "default": 2,
                "hint": "同时最多下载任务数，防止大规模批量下载占用资源"
            }
        }
    },
    "content": {
        "type": "section",
        "name": "内容审核",
        "hint": "搜索与元信息查询工具的控制",
        "collapsed": false,
        "fields": {
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
            "allow_cross_session": {
                "type": "switch",
                "default": false,
                "hint": "允许转发到其他会话。关闭时 send_jm_album 的 target 只能设置为调用者所在的会话"
            }
        }
    },
    "encryption": {
        "type": "section",
        "name": "加密",
        "hint": "ZIP 加密发送相关配置",
        "collapsed": false,
        "fields": {
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
    },
    "quality": {
        "type": "section",
        "name": "质量",
        "hint": "PDF 合成质量配置",
        "collapsed": false,
        "fields": {
            "pdf_quality": {
                "type": "integer",
                "default": 85,
                "hint": "JPEG 质量 (1-100)，越高文件越大画质越好"
            }
        }
    },
    "upload": {
        "type": "section",
        "name": "上传",
        "hint": "文件发送方式与超时配置",
        "collapsed": false,
        "fields": {
            "upload_method": {
                "type": "string",
                "default": "path",
                "options": ["path", "base64", "stream"],
                "hint": "① path 同机路径直传；② base64 跨机内联；③ stream 分片（仅 NapCat / SnowLuma）"
            },
            "max_file_mb": {
                "type": "integer",
                "default": 0,
                "hint": "0 = 不限制；>0 时发送前拦截超限文件（插件侧闸门，不是实现端上限）"
            },
            "upload_timeout": {
                "type": "integer",
                "default": 300,
                "hint": "上传超时秒数（大文件建议 300+）"
            },
            "chunk_size": {
                "type": "integer",
                "default": 524288,
                "hint": "Stream 上传分片字节数，默认 512KB。调整需重启，建议 512KB-4MB"
            }
        }
    },
    "cache": {
        "type": "section",
        "name": "缓存",
        "hint": "PDF 缓存配置",
        "collapsed": false,
        "fields": {
            "max_cache": {
                "type": "integer",
                "default": 10,
                "hint": "最多缓存几本 PDF，超限自动删最旧"
            }
        }
    },
    "notification": {
        "type": "section",
        "name": "通知",
        "hint": "任务完成通知配置",
        "collapsed": false,
        "fields": {
            "notify_llm": {
                "type": "switch",
                "default": true,
                "hint": "任务完成后是否在目标会话触发 LLM 自动回复"
            }
        }
    }
}
```

## 配置读取与兼容

- section 的子字段由核心**嵌套存储**到插件配置文件（如 `{"download": {"download_threads": 45}}`），插件内通过 `self.plugin_cfg.get("download", {}).get("download_threads")` 读取。
- 分组前生成的平铺配置仍被兼容：`_load_config()` 读取顺序为 section 嵌套值 > 旧版平铺值 > 默认值。用户首次在 WebUI 保存分组配置后，配置文件自动迁移为嵌套结构。

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

### max_file_mb

**默认 0 = 不限制**，文件原样交给实现端，成败由实现端真实上限决定。

设为正整数即在发送前拦截超限文件，失败信息里明确写「这是插件侧的保护性闸门，**不是 QQ 或实现端的上限**」，并给出调大 / 设为 0 的操作建议。它不改实现端行为，只决定要不要发出去。

主要用途是限制 `base64` 方案的内存峰值：base64 链路的字节 → 编码 → 字符串 → `json.dumps` 串行复制，实测峰值约为文件的 **5.3 倍**（50MB → 267MB），而 `path` / `stream` 的内存占用都是 O(1)。跨机大文件请优先用 `stream`。

### upload_timeout

`path` / `base64`：单次 `send_action` 的超时（外层再用 `wait_for` 兜 300+120s 的总时长）。大文件（50MB+）可能需要 300s 以上，按体积调整。

`stream`：这里是**单片**超时（实际取 `max(10, upload_timeout // 10)`），发送阶段独立用 `upload_timeout + 120`。分片方案**不设总时长闸门**——总时长 = 分片数 × 每片往返 + 实现端转存，无法预估，用固定总时长只会把正常慢传输误判成失败。改为**无进展看门狗**：连续 `max(60, 单片超时 × 2)` 秒没有收到任何实现端确认才判失败，发送阶段（无中间 ack）自动放宽到 `send_timeout + 60` 秒。

### upload_method

三种上传方案显式三选一，不再按 WS 地址自动猜测：

| 值 | 链路 | 适用 | 进度 |
|----|------|------|------|
| `path` | 一次 `upload_private_file` / `upload_group_file`，`file` 为实现端机器上的绝对路径 | 同机部署；零膨胀、零编码内存；6 种实现全部支持；**不跨机**（Docker 部署的实现端读不到宿主机路径） | 进度未知 |
| `base64`（默认） | 同一组动作，`file` 为 `base64://` 内联内容 | 跨机（不要求同机）；体积 +33%、编码期内存约为文件的 6 倍；NapCat 实测 37MB 上限；仅 NapCat / SnowLuma / LLOneBot / OpenShamrock 支持 | 进度未知 |
| `stream` | `upload_file_stream` 逐片 → 实现端临时路径 → 同一组发送动作 | 跨机大文件；仅 NapCat / SnowLuma 提供 | 分片期间真实百分比；实现端发送阶段进度未知 |

`upload_private_file` / `upload_group_file` 不是 OneBot 11 核心条款，而是 go-cqhttp 引入、
各家实现共同提供的扩展动作，覆盖面如下（源码核查 2026-11-11）：

| 实现 | 标准上传 | path | `base64://` | 分片 |
|------|:--------:|:----:|:-----------:|:----:|
| NapCat | ✅ | ✅ | ✅ | ✅ |
| SnowLuma | ✅ | ✅ | ✅ | ✅ |
| LLOneBot | ✅ | ✅ | ✅ | ❌ |
| Lagrange.OneBot (v1) | ✅ | ✅ | ❌ | ❌ |
| OpenShamrock | ✅ | ✅ | ✅ | ❌ |
| go-cqhttp（已停维护） | ✅ | ✅ | ❌ | ❌ |

标准动作是单次 `send_action`，实现端内部转存不回报中间状态，OneBot 也没有进度事件，
因此 `path` / `base64` 一律显示「进度未知」而不是伪造百分比。`stream` 的百分比来自
已确认分片数 / 总分片数，是真实字节进度；分片跑完后实现端把文件发给 QQ 的那一段仍无
进度，同样回落「进度未知」。

Lagrange.OneBot / go-cqhttp 不解析 `base64://`，也不提供分片动作，只能同机 + `path`。
数据来源是 `impl_support.py`，`schema.json` 的提示与运行时报错都由它生成/校验。

**旧配置迁移**：`use_napcat_stream`（开关）与 `file_payload`（载荷）已合并为本项。
`use_napcat_stream=true` → `stream`；`file_payload=path/base64` → 同名方案；
`file_payload=auto` → `base64`（auto 已移除，启动时写告警日志）。跨机且文件常超 37MB
请显式改选 `stream`。

### 体积上限与失败语义

**默认三条方案都不设本地体积上限**（`max_file_mb=0`）。`path` 的请求体只含路径字符串，
`stream` 按分片传输，`base64` 内联全文；能否成功完全由实现端的请求体 / 帧上限决定。
配了 `max_file_mb>0` 时插件会在发送前拦下超限文件，并在信息里说明这是插件配置、不是实现端上限。
`standard_max_mb` 已移除，旧配置里残留该键会被忽略（改用 `max_file_mb`）。

2026-11-12 在线实测 NapCat.Core 4.18.14（`upload_private_file` + `base64://`，私聊）：
1/4/8/10/16/24/32/37 MB 成功，38 MB 起失败，实现端日志为
`Client Error: Max payload size exceeded`，与 `maxPayload=50MiB` × 3/4 ≈ 37.5 MiB 一致。
旧文档「16MB 帧上限 → 取 10MB」的推导已被证伪。该实测值仅作排障参考，
**不会写回配置**，其他实现的上限需各自实测。

失败信息分两类，文本经 `TaskState.error` → 完成通知送达 LLM：

| 情形 | 报告内容 |
|------|----------|
| 实现端返回失败响应 | 原样列出 `status` / `retcode` / `message` / `wording` / `data`，不替实现端判断原因 |
| 等待响应超时 | 说明动作、阶段、超时秒数、文件体积与「未收到响应」，列可能原因并提示查实现端日志；不把超时说成「文件太大」 |
| 连接中断 | `websockets.ConnectionClosed` 不是 `OSError`，单独识别；说明中断的动作与阶段，**仅在逐片阶段**提示分片可能残缺 |
| stream 长时间无确认 | 报「停滞」（含 idle 秒数与阈值），并说明原因未知、实现端静默丢弃超限分片也是可能原因之一 |
| 超过 `max_file_mb` | 说明体积、上限、这是插件侧闸门，并给调大 / 设为 0 的指引 |

两类都附可操作指引（核对动作支持、目标会话权限、`path` 是否同机、换方案重试）。
注意超限帧在实现端可能被直接丢弃、**不返回失败响应**，客户端只看到超时，因此
`upload_timeout` 内的等待是预期行为；诊断要靠实现端日志，不是靠插件猜。

## 运行时配置目录

```
data/plugin_data/jmdown/
├── cache/               # PDF 缓存 + ZIP 文件
├── downloads/           # 下载临时目录（启动时清理孤立文件）
└── cache_index.json     # 缓存索引
```

## KiraAI 配置

插件数据目录通过 `self.ctx.get_plugin_data_dir()` 获取，自动映射到 `data/plugin_data/jmdown/`。PDF 写入 `cache/` 子目录，下载写入 `downloads/` 子目录。
