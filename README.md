# KiraAI JMComic Downloader Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)
[![Version](https://img.shields.io/badge/version-2.3.0-blue)](manifest.json)

**jmdown** 是 [KiraAI](https://github.com/CelestNya/KiraAI) 的插件，用于下载禁漫天堂 (JMComic) 本子 → 合成 PDF → 分片流传输发送到 QQ。

## 工作流程

```
用户请求本子 ID → 后台异步任务 → 下载图片 → 合成 PDF → NapCat Stream 分片上传 → 发送到目标会话 → 完成通知（可选触发 LLM 回复）
```

| 阶段 | 说明 |
|------|------|
| 提交任务 | LLM 调用 `send_jm_album` 工具，返回 `JOB-YYMMDD-NNN` 标识码 |
| 下载 | jmcomic 库并行下载图片，按 album_id 建目录 |
| 合成 | img2pdf + Pillow 合成为 PDF，实时报进度 |
| 上传 | NapCat Stream API 分片上传（512KB/片），绕过 WS 帧限制 |
| 发送 | `upload_private_file` / `upload_group_file` 发送给目标 |
| 通知 | 完成/失败时发通知到目标会话，可选触发 LLM 自动回复 |

## 安装

### 前提条件

- Python ≥ 3.11
- KiraAI（含 NapCatQQ + WebSocket 连接）
- QQ 机器人框架（NapCat）

### 步骤

1. 克隆仓库到 KiraAI 的 `data/plugins/` 目录：

```bash
cd /path/to/KiraAI/data/plugins
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git jmdown
```

2. 安装依赖（KiraAI 自动安装，也可手动）：

```bash
pip install jmcomic>=2.7 Pillow>=11 img2pdf>=0.6
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
返回: 任务标识码 JOB-YYMMDD-NNN
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
LLM → send_jm_album(album_id=421982, target="qq:dm:2263130787")
     → "已提交任务 JOB-241215-001"
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
| `max_cache` | integer | 10 | 最多缓存几本 PDF |
| `desc_max_length` | integer | 80 | 描述截取字符数 |
| `download_threads` | integer | 45 | 下载图片并行线程 |
| `chunk_size` | integer | 524288 | Stream 分片字节数，默认 512KB |
| `pdf_quality` | integer | 85 | JPEG 质量 (1-100) |
| `upload_timeout` | integer | 300 | 上传超时秒数 |
| `notify_llm` | switch | true | 完成后是否触发 LLM 回复 |

## 缓存位置

- **PDF 缓存**：`data/plugin_data/jmdown/cache/`
- **索引文件**：`data/plugin_data/jmdown/cache_index.json`
- **下载临时目录**：`data/plugin_data/jmdown/download/`

## 技术要点

- 大文件上传走 NapCat Stream API（分片 + `is_complete` 组装），非旧版 base64 直传
- 目录规则使用 `Bd_Aid`（按 album_id 命名），不依赖标题
- 页数校验：`sum(len(ch) for ch in album_obj)` vs 实际图片数，不匹配则报错
- Background task 绕过 KiraAI tool 60s 超时限制

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

[MIT](LICENSE)
