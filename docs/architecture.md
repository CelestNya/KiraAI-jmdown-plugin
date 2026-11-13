# 架构总览

## 模块划分

```
__init__.py          ← 插件入口，导出 JMdownPlugin
main.py              ← 核心逻辑：工具注册、任务管理、下载、PDF、加密、通知
cache.py             ← FIFO 缓存队列，独立可测试
file_send.py         ← 标准上传（upload_private_file / upload_group_file）
napcat_stream.py     ← 分片上传（upload_file_stream，NapCat / SnowLuma）
impl_support.py      ← 实现方能力矩阵（提示文本唯一数据源）
manifest.json        ← 插件元信息
schema.json          ← 配置项定义
```

## 数据流

```
LLM 调用 send_jm_album (或 query_jm_album / search_jm_album)
  │
  ├─ query_jm_album ──► _fetch_album_meta → 返回元信息
  ├─ search_jm_album ──► _search_albums → 返回搜索结果
  │
  └─ send_jm_album ──► _task_runner (后台 asyncio task)
       │
       ├─ 1. 检查缓存 ── hit ──┬─ zip_encrypt=false → 直接上传
       │                       └─ zip_encrypt=true  → 加密 ZIP → 上传
       │
       └─ 2. 下载图片 (jmcomic, Bd_Aid 规则)
            │
            ▼
            3. 合成 PDF (Pillow → img2pdf)
            │
            ├─ zip_encrypt=false → PDF 直传
            └─ zip_encrypt=true  → 压缩为 AES-256 加密 ZIP
            │
            ▼
            4. `_deliver()` 按 upload_method 分派
            │   ├─ path   ：一次 upload_private_file / upload_group_file（本地路径）
            │   ├─ base64 ：同上的动作，file=base64:// 内联内容
            │   └─ stream ：upload_file_stream 逐片 → 远端 temp 路径 → 同上的发送动作
            │
            ▼
            5. 发送到目标会话
            │
            ▼
            6. 完成通知 (可选触发 LLM 回复)
```

新建任务经过完整六步。缓存命中时：

```
下载: 缓存 | 合成: 缓存 | 上传 → 发送 → 通知
```

## 四个核心阶段

| 阶段 | 耗时 | 说明 |
|------|------|------|
| 下载 | 数秒~数分钟 | jmcomic 并行下载，依赖网络 |
| 合成 | 数秒~数十秒 | 图片转 JPEG → img2pdf，可选 ZIP 加密 |
| 上传 | 数秒~数分钟 | path / base64 一次通用动作；stream 按 chunk_size 逐片上传 |
| 发送 | <1s | 实现端内部文件操作 |

## 关键设计决策

### 为何用后台任务而非同步 tool

KiraAI tool 有约 60s 超时。下载 + PDF + 上传大文件远超此限制。后台 task 配合 `query_jm_task` 轮询 + 完成通知，完全绕过超时。

### 为何默认 path，以及三种方案的取舍

`upload_private_file` / `upload_group_file` 是覆盖面最广的扩展动作：NapCat、SnowLuma、
LLOneBot、Lagrange.OneBot、OpenShamrock、go-cqhttp 都实现（见 `impl_support.py`）。
`path` 在此基础上只传绝对路径，请求体极小，一次 `send_action` 完成，没有多帧与超时叠加，
因此作为唯一不挑实现的默认值——代价是必须同机部署。

`base64` 把文件内容内联进 `file`，跨机可用但体积 +33%，且 Lagrange.OneBot / go-cqhttp
不解析 `base64://`；`stream` 走 `upload_file_stream` 分片，只有 NapCat 与 SnowLuma 提供，
适合跨机大文件，绕开内联膨胀与请求体上限，代价是每片一次 round-trip、每片独立超时，
以及进度回调与失败面的复杂度。三种方案由 `upload_method` 显式选择，不再按 WS 地址猜测，
避免「换了个部署位置就静默换链路」。

进度方面，标准动作是单次调用、协议不回报中间状态，只有 `stream` 分片期间能给出真实
百分比；其余阶段统一显示「进度未知」，不伪造数字误导 LLM。

**默认三条方案都不设本地体积上限**，上限由实现端决定；配了 `max_file_mb>0` 才会在发送前
拦截超限文件（信息里明确写「插件侧闸门、不是实现端上限」）。失败时 `FileSendError` 如实转述
实现端的失败响应，或说明「等待响应超时 / 连接中断 / 停滞」并附诊断指引，不腉断原因、不编造上限。
文本经 `TaskState.error` → 完成通知送达 LLM，且对无消息异常有非空兜底。

超时模型按方案分开：`path` / `base64` 是单次 `send_action`，用 `wait_for` 卡总时长；
`stream` 总时长 = 分片数 × 每片往返 + 实现端转存，不可预估，改用 `StallGuard`
记录最近一次实现端确认的时刻，只在「连续无 ack」时判失败——否则进度一路正常的
慢速传输会被总时长闸门误判成「未收到响应」。

### 为何用 `Bd_Aid` 目录规则

jmcomic 默认 `Bd_Pname`（按本子标题建目录）。但标题含特殊字符、中文、日文，导致路径不可靠且难以验证内容是否匹配。`Bd_Aid` 直接以 album_id 为目录名（如 `downloads/1248643/`），配合页数校验确保内容正确。

### 为何用 content_query / block_content_tools

部分场景只需要下载和发送功能，不希望 LLM 有搜索或查看元信息的能力。两个开关配合实现：

- `content_query=关闭` + `block_content_tools=true`：工具不注册，LLM 完全不知道它们存在
- `content_query=关闭` + `block_content_tools=false`：工具注册但调用时返回拦截提示
- `content_query=开启`：工具正常可用

### 为何用 zip_encrypt

QQ 会对发送的 PDF 做内容审查。加密 ZIP（AES-256）绕过审查机制，解压密码通过聊天消息单独发送。
