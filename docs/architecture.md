# 架构总览

## 模块划分

```
__init__.py          ← 插件入口，导出 JMdownPlugin
main.py              ← 核心逻辑：工具注册、任务管理、下载、PDF、加密、通知
cache.py             ← FIFO 缓存队列，独立可测试
napcat_stream.py     ← NapCat Stream API 封装，分片上传、发送
manifest.json        ← 插件元信息
schema.json          ← 配置项定义
test_send.py         ← WS 直连测试脚本
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
            4. 分片上传 (NapCat Stream API)
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
| 上传 | 数秒~数分钟 | WS 分片上传，chunk_size 可配置 |
| 发送 | <1s | NapCat 内部文件操作 |

## 关键设计决策

### 为何用后台任务而非同步 tool

KiraAI tool 有约 60s 超时。下载 + PDF + 上传大文件远超此限制。后台 task 配合 `query_jm_task` 轮询 + 完成通知，完全绕过超时。

### 为何用 NapCat Stream API

旧方案：`upload_private_file` 传 base64，文件体积膨胀 33%，且 NapCat WS 帧上限 16MB。Stream API 分片上传（chunk_size 默认 512KB），通过 `is_complete` 信号组装后仅传远端路径，无体积限制。

### 为何用 `Bd_Aid` 目录规则

jmcomic 默认 `Bd_Pname`（按本子标题建目录）。但标题含特殊字符、中文、日文，导致路径不可靠且难以验证内容是否匹配。`Bd_Aid` 直接以 album_id 为目录名（如 `downloads/1248643/`），配合页数校验确保内容正确。

### 为何用 content_query / block_content_tools

部分场景只需要下载和发送功能，不希望 LLM 有搜索或查看元信息的能力。两个开关配合实现：

- `content_query=关闭` + `block_content_tools=true`：工具不注册，LLM 完全不知道它们存在
- `content_query=关闭` + `block_content_tools=false`：工具注册但调用时返回拦截提示
- `content_query=开启`：工具正常可用

### 为何用 zip_encrypt

QQ 会对发送的 PDF 做内容审查。加密 ZIP（AES-256）绕过审查机制，解压密码通过聊天消息单独发送。
