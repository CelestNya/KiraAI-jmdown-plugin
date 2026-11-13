# KiraAI JMComic Downloader Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](requirements.txt)
[![Version](https://img.shields.io/badge/version-2.10.1-blue)](manifest.json)

**jmdown** 是 [KiraAI](https://github.com/CelestNya/KiraAI) 的插件，用于下载禁漫天堂 (JMComic) 本子 → 合成 PDF → 按所选上传方案发送文件到 QQ。

## 工作流程

```
用户请求本子 ID → 后台异步任务 → 下载图片 → 合成 PDF（可选加密 ZIP）→ 上传发送（三选一：path / base64 / stream）→ 完成通知（可选触发 LLM 回复）
```

| 阶段 | 说明 |
|------|------|
| 提交任务 | LLM 调用 `send_jm_album` 工具，返回 `JOB-<随机串>` 标识码 |
| 下载 | jmcomic 库并行下载图片，按 album_id 建目录，实时百分比+速度 |
| 合成 | img2pdf + Pillow 合成为 PDF，实时报进度 |
| 上传 | `path` / `base64` 一次 `upload_private_file` / `upload_group_file`；`stream` 走 `upload_file_stream`（512KB/片） |
| 发送 | `stream` 方案下用实现端临时路径调用 `upload_private_file` / `upload_group_file` |
| 通知 | 完成/失败时发通知到目标会话，可选触发 LLM 自动回复 |

## 安装

### 前提条件

- Python ≥ 3.11
- KiraAI（正向 WebSocket 连接）
- 支持文件上传动作的 OneBot 实现（见下方支持矩阵）

### 步骤

1. 克隆仓库到 KiraAI 的 `data/plugins/` 目录：

```bash
cd /path/to/KiraAI/data/plugins
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git jmdown
```

2. 安装依赖（KiraAI 首次加载时会自动安装 `requirements.txt`，也可手动执行）：

```bash
python -m pip install -r requirements.txt
```

3. 重启 KiraAI，插件自动加载。

## 使用

### 工具：`send_jm_album`

提交下载任务到后台，返回任务标识码。

```
参数:
  album_id (integer) — 禁漫本子数字 ID
  target   (string)  — 目标会话，格式 "adapter:type:id"
                       示例: qq:dm:123456（私聊）、qq:gm:789012（群聊）
返回: 任务标识码 JOB-<随机串>
```

### 工具：`search_jm_album`

搜索禁漫本子，返回标题、ID、标签。

```
参数:
  keyword  (string) — 搜索关键词，多词空格分隔（必填其一）
  tag      (string) — 按标签搜索（必填其一）
  author   (string) — 按作者搜索（必填其一）
  work     (string) — 按作品搜索（必填其一）
  page     (integer) — 页码，默认第1页
  order_by (enum)   — 排序: relevance/views/likes
返回: 搜索结果列表，含 ID、标题、标签
```

### 工具：`query_jm_album`

查询本子元信息（标题、作者、标签、页数等），不下载内容。

```
参数:
  album_id (integer) — 禁漫本子数字 ID
返回: 标题、作者、标签、页数、章节、喜欢/观看/评论、描述
```

### 工具：`query_jm_task`

查询后台任务进度。

```
参数:
  job_id (string) — 任务标识码
返回: 阶段状态、耗时、结果/错误
```

### 示例

```
用户：帮我下载本子 421982，发到我QQ私聊
LLM → send_jm_album(album_id=421982, target="qq:dm:123456")
     → "已提交任务 JOB-X1a1MVQB0k0"
LLM → 告知用户任务已提交
...异步完成后自动发通知到会话...
```

### 缓存

- FIFO 淘汰，默认缓存 10 本
- 缓存命中跳过下载 + 合成，直接上传发送
- `query_jm_task` 可查历史任务记录（最多保留 30 条）

## 配置

在 KiraAI 插件管理界面配置（`schema.json`）：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| **下载** ||||
| `download_threads` | integer | 45 | 下载图片并行线程 |
| `max_concurrent` | integer | 2 | 同时最多下载任务数 |
| **内容审核** ||||
| `content_query` | switch | false | 允许搜索和查看本子元信息。关闭后受 `block_content_tools` 控制 |
| `block_content_tools` | switch | true | content_query 关闭时：true=不注册工具，false=保留但返回拦截提示 |
| `allow_cross_session` | switch | false | 允许转发到其他会话。关闭时 target 只能设为当前会话 |
| **加密** ||||
| `zip_encrypt` | switch | false | 开启后加密 ZIP（AES-256）。关闭后直接发原始 PDF |
| `custom_password` | string | "" | 自定义密码，留空自动随机生成（需开启 `zip_encrypt`） |
| **质量** ||||
| `pdf_quality` | integer | 85 | JPEG 质量 (1-100) |
| **上传** ||||
| `upload_method` | string | path | 三选一：`path`=标准动作+本地绝对路径（同机，6 种实现全支持）/ `base64`=标准动作+内联内容（跨机，+33%，仅 4 种实现支持）/ `stream`=分片（仅 NapCat、SnowLuma，唯一有真实上传百分比）。下拉框的 hint 有逐项说明 |
| `upload_timeout` | integer | 300 | 上传超时秒数。`path`/`base64` 是单次动作的总超时；`stream` 是**单片**超时（取 1/10），发送阶段独立用 +120s |
| `max_file_mb` | integer | 0 | 插件侧文件大小上限（MB），**0 = 不限制（默认）**。>0 时超限文件发送前被拒，信息里会说明这是插件闸门、不是实现端上限。主要用来限 `base64` 的内存峰值（约为文件的 5.3 倍） |
| `chunk_size` | integer | 524288 | 仅 `stream` 生效：分片字节数，默认 512KB |
| **缓存** ||||
| `max_cache` | integer | 10 | 最多缓存几本 PDF |
| **通知** ||||
| `notify_llm` | switch | true | 完成后是否触发 LLM 回复 |

### OneBot 实现支持矩阵

`upload_private_file` / `upload_group_file` 不是 OneBot 11 核心条款，而是 go-cqhttp 引入、
各家实现共同提供的**扩展动作**。下表依据各项目源码核查（2026-11-11），不是运行时探测：

| 实现 | 标准上传 | path 直传 | `base64://` | 分片上传 |
|------|:--------:|:---------:|:-----------:|:--------:|
| NapCat | ✅ | ✅ | ✅ | ✅ |
| SnowLuma | ✅ | ✅ | ✅ | ✅ |
| LLOneBot | ✅ | ✅ | ✅ | ❌ |
| Lagrange.OneBot (v1) | ✅ | ✅ | ❌ | ❌ |
| OpenShamrock | ✅ | ✅ | ✅ | ❌ |
| go-cqhttp（已停维护） | ✅ | ✅ | ❌ | ❌ |

三种方案按部署形态显式选择：同机用 `path`；跨机且实现支持内联用 `base64`；跨机大文件且实现为 NapCat / SnowLuma 用 `stream`。Lagrange.OneBot 与 go-cqhttp 既不解析 `base64://` 也不提供分片动作，只能同机 + `path`。

进度语义：`path` / `base64` 是单次动作，协议不回报中间状态，状态显示「进度未知」；`stream` 分片期间显示真实百分比，分片完成后实现端发送阶段同样显示「进度未知」。

体积上限：三条方案**均不设本地闸门**，由实现端决定成败。2026-11-12 实测 NapCat.Core 4.18.14 的 `base64` 私聊文件 37 MB 成功、38 MB 触发 `Max payload size exceeded`（与 `maxPayload=50MiB` 一致）；该值仅作排障参考，不写回配置。失败时如实报告实现端返回的 `status` / `retcode` / `message`，或说明「等待响应超时」并附诊断指引，不腉断原因、不编造上限。

旧配置迁移：`use_napcat_stream` / `file_payload` 已合并为 `upload_method`。旧值 `auto` 已移除，升级后按 `path` 处理并写告警日志；跨机部署请在 WebUI 重新选 `base64` 或 `stream`。

## 缓存位置

- **PDF 缓存**：`data/plugin_data/jmdown/cache/`
- **索引文件**：`data/plugin_data/jmdown/cache_index.json`
- **下载临时目录**：`data/plugin_data/jmdown/downloads/`

## 技术要点

- `upload_method` 三选一：`path` / `base64` 走 `upload_private_file` / `upload_group_file` 通用扩展动作；`stream` 走 `upload_file_stream` 分片（`is_complete` 组装，分片大小可配），仅 NapCat / SnowLuma 提供
- 目录规则使用 `Bd_Aid`（按 album_id 命名），不依赖标题
- 页数从 `get_photo_detail.page_arr` 获取，确保与实际下载文件精确匹配
- 使用 `download_photo` 只下单章，挂载章节（分P本子）不自动下载，ID 暴露给 LLM 决定
- 下载/合成显示真实百分比；上传阶段仅 `stream` 分片期间有真实百分比，`path` / `base64` 及实现端发送阶段一律显示「进度未知」
- 后台任务（Background task）绕过 KiraAI tool 60s 超时限制
- `content_query` 开关关闭时，搜索、元信息查询、完成通知中的元数据全部隐藏，仅保留下载发送功能
- 上传用 `asyncio.wait_for` 做外层硬超时，避免 WS 半开连接卡死任务；`stream` 例外——总时长不可预估，改用**无进展看门狗**（连续 `max(60, 单片超时×2)` 秒无 ack 才判失败）
- 死任务检测：最近 `upload_timeout+120s` 内毫无进展（`TaskState.progress_at`，每个分片 ack 都会刷新）才视为死任务并取消，避免把跑得久但仍在推进的大文件上传误杀
- 同 ID 并发提交原子占位（`_pending_aids`）：预查元信息是 await，不占位会重复下载同一本并丢掉先注册的任务

## 项目结构

```
├── __init__.py          # 包入口（惰性导出，不做模块级导入）
├── main.py              # 核心实现（工具、任务管理、下载、PDF、通知）
├── cache.py             # FIFO 缓存模块
├── file_send.py         # 标准上传（通用扩展动作）
├── napcat_stream.py     # 分片上传（upload_file_stream）
├── impl_support.py      # OneBot 实现方能力矩阵（提示文本唯一数据源）
├── manifest.json        # 插件元信息
├── schema.json          # 配置参数定义
├── requirements.txt     # 依赖声明（KiraAI 安装时读取此文件）
├── pytest.ini           # 测试入口配置（testpaths = tests）
├── ruff.toml            # lint / format 规则
├── tests/               # 单元测试 + 协议 e2e（不依赖 KiraAI 运行时）
├── scripts/             # 真实 core 联动 e2e、上传上限探测、集成冒烟
├── .github/workflows/   # CI：lint + format + pytest（3.11 / 3.12 / 3.13）
├── docs/                # VitePress 开发者文档
├── LICENSE
└── README.md
```

## 开发

```bash
python -m pytest -q          # 128 个用例（含 11 个协议 e2e），约 4s
ruff check .                 # 规则见 ruff.toml
ruff format --check .
```

测试通过 `tests/_core_stub.py` 注入 `core` 桩，因此不需要完整 KiraAI 运行时。
`tests/test_upload_e2e.py` 会起本地假 OneBot WS 服务端，验证插件发出的真实协议帧
（含分片重组与 SHA256），同样不需要 NapCat / QQ 在线。

另有两个手动脚本：`scripts/e2e_real_core.py` 换成 KiraAI 真实客户端做联动验证；
`scripts/probe_upload_limit.py --dry-run` 自检上限探测链路（`--execute` 才会真的发文件）。
详见 [docs/development.md](docs/development.md)。

## 许可

[MIT](LICENSE)
