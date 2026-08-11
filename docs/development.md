# 开发指南

## 环境搭建

```bash
# 克隆
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git
cd KiraAI-jmdown-plugin

# 依赖
pip install jmcomic>=2.7 Pillow>=11 img2pdf>=0.6
```

## 项目文件说明

| 文件 | 职责 | 关键类/函数 |
|------|------|-------------|
| `main.py` | 核心逻辑 | `JMdownPlugin`, `_task_runner`, `_download_images`, `_images_to_pdf` |
| `cache.py` | 缓存模块 | `CacheIndex`, `CacheEntry` |
| `napcat_stream.py` | 上传封装 | `stream_upload_file`, `send_file_via_stream` |
| `__init__.py` | 入口 | 导出 `JMdownPlugin` |

## 开发注意事项

### 首次下载目录规则

```python
opt.dir_rule.rule_dsl = "Bd_Aid"
opt.dir_rule.parser_list = opt.dir_rule.get_rule_parser_list("Bd_Aid")
```

`DirRule` 没有 `rule` 属性（只有 `rule_dsl`），`rule = "Aid"` 是静默无操作。必须设置 `rule_dsl` **并** 重建 `parser_list`。

### 页数校验

```python
expected_stems = {Path(p).stem for p in (photo_detail.page_arr or [])}
expected = len(expected_stems)
if expected > 0 and len(images) != expected:
    raise JMDownError(f"页数不匹配: 实际 {len(images)} 张, 预期 {expected} 张")
```

- `album.page_count` 可能是 0（API 不保证填充）
- 正确方式：用 `photo_detail.page_arr`（精确文件名列表）的 stem 集合计数
- `expected > 0` 保护：某些情况下 API 返回不完整，跳过校验

### Stream Upload Hash

```python
# 分块读取计算 SHA256，避免一次性读入大文件
def _sha256_file(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
```

- `expected_sha256` 必须是完整文件的 hash
- 每个 chunk 传同样的值
- 不是分片的 hash（那会导致 NapCat 校验失败）

### 目标格式

```
adapter:type:id
  adapter: QQ、Telegram 等
  type: dm(私聊 direct message) / gm(群聊 group message)
  id: 用户 QQ 号 / 群号
```

解析时只验证格式合法性，不验证 adapter 是否存在：

```python
def _parse_target(target: str) -> tuple[str, bool, Optional[str]]:
    parts = target.split(":", 2)
    if len(parts) != 3:
        raise JMDownError(f"目标会话格式错误: {target}, 应为 adapter:type:id")
    _adapter, stype, sid = parts
    if stype == "dm":
        return sid, False, None
    if stype == "gm":
        return sid, True, sid
    raise JMDownError(f"未知会话类型: {stype}, 应为 dm(私聊) 或 gm(群聊)")
```

## 测试

### 单元测试

测试在 `KiraAI-src` 的 uv 环境下运行（提供 jmcomic/PIL/img2pdf 等依赖）。`test_main.py` 通过 `sys.modules` 注入 `core` 框架桩，无需完整 KiraAI 运行时即可测插件逻辑。

```bash
# pytest（tests/pytest.ini 已配 rootdir 与 import-mode）
cd ../KiraAI-src && uv run python -m pytest ../KiraAI-jmdown-plugin/tests -v

# 或 unittest discover
cd ../KiraAI-src && uv run python -m unittest discover -s ../KiraAI-jmdown-plugin/tests -v
```

覆盖范围：`cache.py` FIFO/加载容错、`main.py` 输入校验/去重/并发/跨会话/死任务/ZIP 清理/后台 task 生命周期、`napcat_stream.py` 分片上传链路/SHA256/适配器查找。

### 手动测试

```bash
# 在 KiraAI 中测试
# 发送到 QQ 私聊：send_jm_album(album_id=1248643, target="qq:dm:2263130787")
# 查询任务状态：query_jm_task(job_id="JOB-X1a1MVQB0k0")
```

### 验证清单

- [ ] 下载目录是 `download/{album_id}/` 而非标题
- [ ] 图片数与 API 返回一致
- [ ] PDF 生成后源图被删除
- [ ] 缓存命中跳过下载+合成
- [ ] 分片上传 + `is_complete` 返回 `file_path`
- [ ] 文件正常到达 QQ
- [ ] 完成通知不带 emoji
- [ ] `notify_llm=false` 不触发 LLM 回复
- [ ] 错误（如不存在的 ID）返回友好消息
- [ ] 同 ID 正在下载时不重复提交

## 版本兼容

| 组件 | 要求 |
|------|------|
| Python | ≥ 3.11 |
| KiraAI | 支持 `plugin_cfg` 和 `publish_notice` 的版本 |
| jmcomic | ≥ 2.7 |
| NapCat | 支持 `upload_file_stream` 动作 |

## 构建与发布

无需构建步骤。插件以源码形式运行。发布时：

1. 更新 `manifest.json` 版本号
2. 更新 `README.md` 文档
3. 提交并打 tag

## 贡献

- 保持 `napcat_stream.py` 独立可测试，不依赖 KiraAI plugin 上下文
- `cache.py` 同样独立，可单元测试
- 阶段性通知使用中文（无 emoji），保持简洁
- 异常用 `JMDownError` 包装，不传播底层异常到 LLM
