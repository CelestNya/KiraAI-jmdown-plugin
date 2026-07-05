# KiraAI JMComic Downloader 插件 (jmdown)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)  [![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](requirements.txt)  [![Version](https://img.shields.io/badge/version-2.9.3-blue)](manifest.json)

jmdown 是 KiraAI 的插件，用于：下载 禁漫天堂 (JMComic) 本子 → 合成为 PDF → 通过 NapCat Stream 分片上传并发送到 QQ 会话（私聊 / 群聊）。

## 目录

- 工作流程
- 安装
- 使用
  - 工具: `send_jm_album`
  - 工具: `search_jm_album`
  - 工具: `query_jm_album`
  - 工具: `query_jm_task`
  - 示例
- 缓存
- 配置
- 缓存位置
- 技术要点
- 项目结构
- 许可

## 工作流程

用户提交本子 ID → 后台异步任务 → 下载图片 → 合成 PDF → NapCat Stream 分片上传 → 发送到目标会话 → 完成通知（可选触发 LLM 回复）。

```
用户请求本子 ID → 后台异步任务 → 下载图片 → 合成 PDF → NapCat Stream 分片上传 → 发送到目标会话 → 完成通知（可选触发 LLM 回复）
```

阶段说明：

| 阶段 | 说明 |
|------|------|
| 提交任务 | LLM 调用 `send_jm_album` 工具，返回内部任务标识码（示例: `JOB-<随机唯一串>`），该标识为代理/后台使用，请勿向终端用户公开 |
| 下载 | 使用 jmcomic 并发下载图片，按 album_id 建目录，实时百分比 + 速度 |
| 合成 | 使用 img2pdf + Pillow 合成为 PDF，实时报进度 |
| 上传 | NapCat Stream API 分片上传（默认 512KB/片）绕过 WS 帧限制 |
| 发送 | 使用 `upload_private_file` / `upload_group_file` 发送到目标 |
| 通知 | 完成/失败时发通知到目标会话，可选触发 LLM 自动回复 |

> 说明：为了保证任务标识在并发/测﻿试场景下唯一，插件当前使用不可预测的随机唯一串（URL-safe token）作为 job id 前缀（格式如 `JOB-abcdef12_Gh`）。这些 ID 仅用于内部追踪和工具间通信，插件会在返回值中附加说明性提示，避免 LLM 或机器人把该内部标识直接展示给最终用户。

## 安装

前提：

- Python ≥ 3.11
- 已部署的 KiraAI（含 NapCatQQ + WebSocket 连接）
- QQ 机器人框架（NapCat）

安装步骤：

1. 将本插件克隆到 KiraAI 的 `data/plugins/` 目录（注意：此处使用本仓库的克隆地址）：

```bash
cd /path/to/KiraAI/data/plugins
git clone https://github.com/AuroNyaa/KiraAI-jmdown-plugin.git jmdown
```

2. 安装依赖（KiraAI 会自动安装依赖，也可手动安装）：

```bash
pip install "jmcomic>=2.7" "Pillow>=11" "img2pdf>=0.6" "pyzipper>=0.4"
```

3. 重启 KiraAI，插件会自动加载。

## 使用

工具：`send_jm_album`

将下载任务提交到后台并返回任务标识码（内部使用）。

参数:
  - album_id (integer) — 禁漫本子数字 ID
  - target   (string)  — 目标会话，格式 "adapter:type:id"
    - 示例: `qq:dm:123456`（私聊）、`qq:gm:789012`（群聊）
返回: 内部任务标识码，如 `JOB-<随机唯一串>`（注意：该标识供工具/开发者追踪使用，不应作为对终端用户的可读引用）


工具：`search_jm_album`

搜索禁漫本子，返回标题、ID、标签等基本信息。

参数:
  - keyword  (string) — 搜索关键词，多词空格分隔
  - tag      (string) — 按标签搜索
  - author   (string) — 按作者搜索
  - work     (string) — 按作品搜索
  - page     (integer) — 页码，默认第1页
  - order_by (enum)   — 排序: relevance/views/likes
返回: 搜索结果列表，含 ID、标题、标签


工具：`query_jm_album`

查询本子元信息（标题、作者、标签、页数等），不下载内容。

参数:
  - album_id (integer) — 禁漫本子数字 ID
返回: 标题、作者、标签、页数、章节、喜欢/观看/评论、描述


工具：`query_jm_task`

查询后台任务进度和状态（使用内部 job id 查询）。

参数:
  - job_id (string) — 任务标识码（如插件返回的 `JOB-...`）
返回: 阶段状态、耗时、结果或错误信息


示例：

```
用户：帮我下载本子 421982，发到我 QQ 私聊
LLM → send_jm_album(album_id=421982, target="qq:dm:2263130787")
     → "任务已加入队列，标识码: JOB-<内部唯一串>（仅供内部追踪）"
...后台异步完成后自动发通知到会话...
```

## 缓存

- 使用 FIFO 淘汰策略，默认缓存 10 本 PDF
- 缓存命中则跳过下载与合成，直接上传发送
- `query_jm_task` 可查询历史任务记录（最多保留 30 条）

## 配置

在 KiraAI 插件管理界面通过 `schema.json` 配置参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `download_threads` | integer | 45 | 下载图片并行线程 |
| `max_concurrent` | integer | 2 | 同时最多下载任务数 |
| `upload_timeout` | integer | 300 | 上传超时秒数 |
| `chunk_size` | integer | 524288 | Stream 分片字节数（字节），默认 512KB |
| `pdf_quality` | integer | 85 | JPEG 质量 (1-100) |
| `zip_encrypt` | switch | false | 开启后以 AES-256 加密 ZIP（关闭则直接发送原始 PDF） |
| `custom_password` | string | "" | 自定义密码，留空时自动随机生成（需开启 `zip_encrypt`） |
| `max_cache` | integer | 10 | 最多缓存几本 PDF |
| `content_query` | switch | false | 是否允许搜索与查看本子元信息（受 `block_content_tools` 控制） |
| `block_content_tools` | switch | true | 当 `content_query` 关闭时：true=不注册工具，false=保留但返回拦截提示 |
| `allow_cross_session` | switch | false | 是否允许转发到其他会话（关闭时 target 只能为当前会话） |
| `notify_llm` | switch | true | 完成后是否触发 LLM 回复 |

## 缓存位置

- PDF 缓存：`data/plugin_data/jmdown/cache/`
- 索引文件：`data/plugin_data/jmdown/cache_index.json`
- 下载临时目录：`data/plugin_data/jmdown/downloads/`

## 技术要点

- 大文件通过 NapCat Stream API 分片上传（分片 + is_complete 组装），分片大小可配置
- 目录规则使用 `Bd_Aid`（按 album_id 命名），不依赖标题
- 页数从 `get_photo_detail.page_arr` 获取，确保与实际下载文件精确匹配
- 使用 `download_photo` 只下载指定章（支持分P本子，是否挂载章节由 LLM/调用方决定）
- 下载 / 合成 / 上传 三阶段均实时显示百分比和速度
- 后台异步任务绕过 KiraAI tool 的 60s 超时限制
- `content_query` 关闭时，搜索、元信息查询、完成通知中的元数据会被隐藏，仅保留下载发送功能
- 上传使用 `asyncio.wait_for` 做外层硬超时，以防 WebSocket 半开连接卡死任务
- 死任务检测：超过 `upload_timeout + 120s` 未完成的任务会自动取消，允许重新提交

## 项目结构

```
├── __init__.py          # 插件入口
├── main.py              # 核心实现（工具、任务管理、下载、PDF、通知）
├── cache.py             # FIFO 缓存模块
├── napcat_stream.py     # NapCat Stream API 分片上传封装
├── manifest.json        # 插件元信息
├── schema.json          # 配置参数定义
├── requirements.txt     # 依赖声明
├── test_send.py         # WS 直连上传测试脚本
├── docs/                # VitePress 开发者文档
├── LICENSE
└── README.md
```

## 许可

本项目使用 MIT 许可（LICENSE 文件）。
