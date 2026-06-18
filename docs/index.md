# JMDown Plugin

KiraAI 插件：下载禁漫天堂 (JMComic) 本子，合成 PDF，通过 NapCat Stream API 分片上传发送到 QQ。

## 快速开始

```bash
# 克隆到 KiraAI 插件目录
cd /path/to/KiraAI/data/plugins
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git jmdown

# 安装依赖
pip install jmcomic>=2.7 Pillow>=11 img2pdf>=0.6 pyzipper>=0.4

# 重启 KiraAI，插件自动加载
```

## 核心能力

| 功能 | 说明 |
|------|------|
| 后台异步任务 | 绕过 KiraAI tool 60s 超时限制，可并发下载 |
| 分片流传输 | NapCat Stream API 可配置分片大小，支持超大文件 |
| FIFO 缓存 | 自动管理磁盘空间，默认缓存 10 本 |
| AES-256 加密 | 可选 ZIP 加密绕过 QQ 内容审查 |
| 工具可见性控制 | content_query/block_content_tools 开关精确控制 LLM 可见工具 |
| `notify_llm` 开关 | 完成后可选触发 LLM 自动回复 |
| 页数校验 | 下载后验证页数，不一致报错重试 |

## 文档

- [架构总览](./architecture) — 模块划分、数据流
- [插件系统集成](./plugin-system) — KiraAI BasePlugin 接入细节
- [后台任务系统](./background-tasks) — create_task、进度跟踪、状态查询
- [NapCat Stream 上传](./napcat-stream) — 分片协议、`is_complete`、hash 要求
- [配置参考](./config-reference) — 所有配置项说明
- [开发指南](./development) — 环境搭建、测试、构建
