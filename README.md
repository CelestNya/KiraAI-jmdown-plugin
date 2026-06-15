# KiraAI JMComic Downloader Plugin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

**jmdown** 是 [KiraAI](https://github.com/CelestNya/KiraAI) 的插件，用于下载禁漫天堂 (JMComic) 本子并合成为 PDF，自带 FIFO 缓存管理。

## 工作流程

```
用户请求本子 ID → 下载图片 → 合成为 PDF → 删除原图 → 存入缓存 → 返回路径给 LLM
```

- LLM 通过 `<file type="file">` 标签将 PDF 发送给用户
- 缓存满时自动淘汰最旧条目（FIFO）
- 插件启动时清理孤儿文件

## 安装

### 前提条件

- Python ≥ 3.11
- KiraAI（插件系统）

### 步骤

1. 克隆仓库到 KiraAI 的 `data/plugins/` 目录：

```bash
cd /path/to/KiraAI/data/plugins
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git jmdown
```

2. 确保 `requirements.txt` 中的依赖已安装（KiraAI 会自动安装）：

```
jmcomic>=2.7
Pillow>=11
img2pdf>=0.6
```

也可手动安装：

```bash
pip install jmcomic>=2.7 Pillow>=11 img2pdf>=0.6
```

3. 重启 KiraAI，插件自动发现加载。

## 使用

### 工具：`download_jm_album`

LLM 调用此工具下载指定 ID 的本子：

```
参数: album_id (integer) — 禁漫本子数字 ID
返回: 标题、描述、页数、PDF 文件路径
```

**示例：**

> 用户：帮我下载本子 421982
>
> LLM → 调用 `download_jm_album(album_id=421982)`
>
> LLM → 返回结果：
> ```
> ✅ 下载 & 合成完成
> 📖 [Miyako] MY ROOMMATE 2 (EP.6-9)
> 📝 无描述
> 📄 15 页
> 📎 /path/to/cache/421982.pdf
> ```
>
> LLM → 通过 `<file type="file">/path/to/cache/421982.pdf</file>` 发送文件给用户

## 配置

在 KiraAI 插件管理界面配置以下参数：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_cache` | integer | 10 | 最多缓存几本 PDF，超限自动删最旧 |
| `desc_max_length` | integer | 80 | 描述截取字符数，避免刷屏 |

### 配置文件示例（`schema.json`）

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
    "hint": "描述截取字符数，避免刷屏"
  }
}
```

## 缓存机制

- **存储位置**：`data/plugin_data/jmdown/cache/`（插件数据目录）
- **索引文件**：`data/plugin_data/jmdown/cache_index.json`
- **淘汰策略**：FIFO — 当缓存数量超过 `max_cache` 时，删除最早下载的条目
- **孤儿清理**：启动时自动清除索引中不存在的 PDF 和下载目录

## 项目结构

```
KiraAI-jmdown-plugin/
├── jmdown/
│   ├── __init__.py         # 插件入口
│   ├── main.py             # 核心实现（下载、PDF、缓存）
│   ├── manifest.json       # 插件元信息
│   ├── schema.json         # 配置参数定义
│   └── requirements.txt    # 依赖声明
├── tests/
│   ├── __init__.py
│   └── test_cache.py       # 缓存单元测试
├── pyproject.toml           # 项目元数据
├── .gitignore
├── LICENSE                  # MIT 许可
└── README.md
```

## 开发

### 设置开发环境

```bash
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git
cd KiraAI-jmdown-plugin
pip install -e ".[test]"
```

### 运行测试

```bash
pytest
```

## 许可

[MIT](LICENSE)
