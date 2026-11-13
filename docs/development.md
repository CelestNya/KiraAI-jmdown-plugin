# 开发指南

## 环境搭建

插件不是独立应用，它运行在 KiraAI 自己的 Python 环境里。**不要为插件另建 venv**，
否则依赖装在了 KiraAI 看不到的地方，加载时仍会报 ModuleNotFoundError。

```bash
# 1. 克隆到 KiraAI 的插件目录；目录名必须是 jmdown（与 manifest.plugin_id 一致）
cd /path/to/KiraAI/data/plugins
git clone https://github.com/CelestNya/KiraAI-jmdown-plugin.git jmdown

# 2. 用 KiraAI 的解释器装依赖（KiraAI 首次加载插件时也会自动装 requirements.txt）
cd jmdown
../../../venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
../../../venv/bin/python       -m pip install -r requirements.txt    # POSIX
```

> 依赖一律写在 `requirements.txt` 里，不要在命令行直接写 `pip install jmcomic>=2.7`：
> `>` 会被 shell 当成重定向，生成一个名为 `=2.7` 的文件，而包其实没装对版本。

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

```bash
cd /path/to/KiraAI/data/plugins/jmdown
python -m pytest -q                          # 全量（含协议 e2e），约 10s
python -m pytest -q tests/test_upload_e2e.py  # 只跑上传 e2e
python -m pytest -q -k manifest              # 只跑元数据契约
python -m pytest -q -x --lf                  # 失败即停 + 只重跑上次失败
```

### 测试风格

全部用例是 pytest 函数风格，不用 `unittest`：

| 需求 | 写法 |
|------|------|
| 共享上下文（临时文件、插件实例） | 模块内 fixture，作用域按需 `function` / `module` |
| 参数矩阵（原先的 `subTest`） | `@pytest.mark.parametrize`，每个组合是独立用例、独立报错 |
| 断言（原先的 `assertEqual` 等） | 裸 `assert`，pytest 会重写断言表达式并给出 diff |
| 异步用例（原先的 `IsolatedAsyncioTestCase`） | 直接 `async def test_*`，`pytest.ini` 已开 `asyncio_mode = auto` |
| 期望异常 | `with pytest.raises(X) as cm: ...`，再断言文本 |

要求：
- 所有测试函数标参数与返回类型（`-> None`），fixture 标返回类型；
- 每个用例只断言一件事的失败语义，参数化后不要用循环+`subTest` 把多个组合塞回一个用例
  —— 那样失败时只报第一个，还得手动重跑才能看到其余的。

`pytest` 与 `pytest-asyncio` 在 CI 里钉版本（见 `ci.yml`）：异步用例的收集与 loop
作用域行为随版本变，不钉会让「本地绿、CI 红」。

### 为什么测试不依赖 KiraAI 运行时

`tests/_core_stub.py` 用 `sys.modules` 注入 `core.plugin` / `core.chat.*` 的最小契约，
`tests/conftest.py` 在任何用例被收集前完成注入并修正 `sys.path`。因此同一份用例在
插件目录、KiraAI 根目录、CI 的干净 checkout 下结果一致。

两条不能碰的红线：

- **不要恢复 `tests/__init__.py`**：它会让 pytest 把插件根当成包导入，进而在导入期
  触发 `core` 缺失（表现为「测试收集失败」，很容易被误判成插件坏了）。
- **不要在 `__init__.py` 里做模块级 `from .main import`**：`main.py` 依赖 `core` 与
  `jmcomic`，把入口改成惰性 `__getattr__` 之后，`import` 本身才是无副作用的。

桩与真实框架的差异由 `scripts/e2e_real_core.py`（真实客户端 + 假服务端）与
`scripts/local_smoke_test.py`（真实 KiraAI + 网络）兜底，三者职责不重叠。

### 用例分布

| 文件 | 覆盖 |
|------|------|
| `test_cache.py` | FIFO 淘汰、索引损坏容错、`_load_error` 防御 |
| `test_main.py` | 输入校验、去重、并发上限、跨会话拦截、后台 task 生命周期 |
| `test_napcat.py` | 分片上传链路、SHA256、适配器查找、stream_id 白名单 |
| `test_manifest.py` | manifest / schema / README / 源码的跨文件一致性 |
| `test_file_send.py` | 三方案分派、载荷形态、无本地闸门、旧配置迁移、进度未知、失败信息 |
| `test_impl_support.py` | 实现方能力矩阵 ↔ 方案提示 ↔ 文档表格一致性 |
| `test_upload_e2e.py` | **协议 e2e**：真实 WS 帧、服务端重组/SHA256、任务状态机 + 通知 |

### 元数据契约：改一处要同步三处

新增或改名一个配置项，必须同时改：

1. `schema.json` —— 声明控件与默认值
2. `main.py` —— `_g(..., "key")` 真正读取
3. `README.md` —— 配置表里的一行

新增工具同理：`@tool(...)` 与 README 的 ``### 工具：`name` `` 必须成对。
`test_manifest.py` 会双向比对，漏改直接红，不用等用户装上才发现。

## e2e 验证

分三层，从轻到重：

| 层 | 命令 | 验证什么 |
|----|------|----------|
| 单元 / 契约 | `python -m pytest -q` | 纯逻辑与跨文件一致性（CI 中跑） |
| 协议 e2e | 同上，自动收集 `tests/test_upload_e2e.py` | 插件真实代码 → 真实 WebSocket 帧 → 假 OneBot 服务端 |
| 真实 core 联动 | `cd /path/to/KiraAI && ./venv/Scripts/python.exe data/plugins/jmdown/scripts/e2e_real_core.py` | 换成 KiraAI 真正的 `NapCatWebSocketClient` |

协议 e2e 的服务端在 `tests/_onebot_server.py`：校验 Bearer 鉴权、回 lifecycle 元事件、
按 echo 回包、解码 `path` / `base64://` 载荷、按分片重组并比对 SHA256、
按 NapCat/SnowLuma 格式给完成帧。覆盖三种上传方案、无本地体积闸门、进度语义、失败信息、
只回 `retcode` 的实现、以及 `_task_runner` 缓存命中 → 上传 → 完成通知的完整任务链。
**不需要 NapCat / QQ / KiraAI 在线**，CI 可直接跑（需 `pip install websockets`）。

真实 core 联动脚本会先 `chdir` 到 KiraAI 根（`core.logging_manager` 按 CWD 打开
`data/log.log`），再注入 `plugins` 合成包；它验证的是登录闸门、lifecycle、
echo/Future 匹配与插件注册，对端仍是假服务端。

### 上传上限实测（排障用，不写回配置）

插件三条方案均不设本地体积上限，由实现端决定成败。2026-11-12 实测 NapCat.Core
4.18.14：`base64` 私聊文件 37 MB 成功、38 MB 触发 `Max payload size exceeded`
（与 `maxPayload=50MiB` 一致）。该值只作排障与容量规划参考：

```bash
python scripts/probe_upload_limit.py --dry-run     # 离线自检，不发任何文件
python scripts/probe_upload_limit.py --execute --user <测试号> \
    --url ws://127.0.0.1:3001 --token <token> --binary
```

`--execute` 下每次成功都会**真的往该会话发一个文件**，务必用测试号 / 测试群。
`path` 与 `base64` 分别探测：前者受实现端本地文件限制，后者受请求体上限约束。
超限时实现端通常丢帧不回包，脚本记为 `TimeoutError`，需查实现端日志确认原因。

### 手动集成冒烟

需要真实 KiraAI 环境与网络，手动执行：

```bash
cd /path/to/KiraAI/data/plugins/jmdown
../../../venv/Scripts/python.exe scripts/local_smoke_test.py
```

脚本用临时目录当数据目录（不碰 `data/plugin_data/jmdown`），只读生产配置；
KiraAI 根目录按 `<KiraAI>/data/plugins/jmdown` 上溯推断，也可用 `KIRAAI_ROOT` 覆盖。

### 代码质量门禁

```bash
ruff check .            # 规则见 ruff.toml：E/F/I/B/UP，line-length 120
ruff format --check .
```

`.github/workflows/ci.yml` 在 Python 3.11 / 3.12 / 3.13 上跑 lint + format + pytest，
PR 到 `master` / `dev` 均触发。KiraAI 主仓的 workflow 覆盖不到这里（其 ruff 配置
`extend-exclude = ["data"]`），所以门禁必须自建。

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
| Python | ≥ 3.11（CI 覆盖 3.11 / 3.12 / 3.13） |
| KiraAI | `>= v3.0.0`（见 `manifest.core_version`），需支持 `plugin_cfg` 与 `publish_notice` |
| jmcomic | ≥ 2.7 |
| NapCat | 支持 `upload_file_stream` 动作 |

## 构建与发布

无需构建步骤，插件以源码形式运行。发布检查：

1. `ruff check . && ruff format --check . && python -m pytest -q` 全绿（CI 会再跑一遍）
2. 更新 `manifest.json` 版本号
3. 同步 `README.md` 的版本徽章 —— `test_manifest.py` 会校验两者一致
4. 提交并打 tag

## 贡献

- 保持 `napcat_stream.py` 独立可测试，不依赖 KiraAI plugin 上下文
- `cache.py` 同样独立，可单元测试
- 阶段性通知使用中文（无 emoji），保持简洁
- 异常用 `JMDownError` 包装，不传播底层异常到 LLM
