# Python 专项规则

Python 项目（含 `.py` 文件）的附加规范。固化在此文件，不依赖对话上下文。

每条原则包含 Rule（规则）+ Why（原因）+ How to apply（何时怎么应用）。

---

## PY01 强制 uv 包管理

**Rule:** 唯一的包/环境管理器是 `uv`，禁止 pip / poetry / pdm / conda / venv / virtualenv / pipx。

**Why:** uv 是 Rust 写的，比 pip 快 10~100x，同时管理 Python 版本、虚拟环境和依赖锁定。2026 年已成为 Python 包管理的默认选择。

**How to apply:**
- 运行 Python：`uv run python ...`，禁止裸 `python` / `python3` / `python3.xx`
- 安装依赖：`uv add <pkg>`；dev 依赖 `uv add --dev <pkg>`
- 全局工具：`uv tool install <tool>` / `uvx <tool>`
- 新项目：`uv init` → `uv add` → `uv run`
- 已有仓库：有 `uv.lock`/`[tool.uv]` 直接 `uv sync`；只有 requirements/poetry/conda 的，先提议迁移到 uv 再动手
- 一次性脚本：`uv run --with <pkg> python script.py`（PEP 723 内联依赖）

---

## PY02 全员类型注解

**Rule:** 所有函数参数和返回值必须标注类型。对复杂结构用 TypedDict / dataclass / Pydantic。

**Why:** 类型注解在 2026 年已不是可选项。pyright 能在 hook 阶段捕获大量 bug。现代 Python（3.10+）语法足够简洁：`list[str]` 替代 `List[str]`，`X | Y` 替代 `Union[X, Y]`。

**How to apply:** `def func(name: str) -> int:` 而非裸参数。对 JSON 响应结构用 `TypedDict`，对业务对象用 `dataclass`。

---

## PY03 使用 Ruff 做 lint + format

**Rule:** 用 Ruff 替代 Black/isort/flake8/pyupgrade。一个工具，一套配置。

**Why:** Ruff 是 Rust 写的，比 Black 快 10~100x，内置 800+ 规则，同时处理 lint、import 排序、格式化和自动升级语法。

**How to apply:** `ruff check` 做静态检查，`ruff format` 做格式化。

---

## PY04 使用 pytest 做测试，AAA 结构

**Rule:** 用 pytest 替代 unittest。每个测试函数遵循 Arrange（准备）→ Act（执行）→ Assert（断言）三段结构。

**Why:** pytest 是 Python 测试的事实标准，fixture 作用域管理、参数化、标记系统完善。AAA 模式让测试意图清晰可读。

**How to apply:** `conftest.py` 共享 fixture。`@pytest.mark.parametrize` 做数据驱动测试。新功能/修 bug 必须配套写测试，优先 TDD。

---

## PY05 优先 async 处理 IO

**Rule:** 对网络请求、数据库查询、文件读写等 IO 操作，使用 async/await。

**Why:** async 在 Python 3.13+ 已非常成熟，单线程内并发处理 IO 可大幅提升吞吐量。

**How to apply:** httpx 替代 requests。CPU 密集型任务继续用同步代码或 multiprocessing。

---

## PY06 结构化日志替代 print

**Rule:** 用标准库 `logging` + JSON 格式化输出，替代 `print()`。

**Why:** print 无法控制日志级别、不能结构化输出、无法被日志系统聚合。

**How to apply:** 日志级别：DEBUG 开发，INFO 流程追踪，WARNING 异常但不中断，ERROR 异常且需要人工介入。

---

## PY07 注释说明 WHY 而非 WHAT

**Rule:** 代码本身应该表明 WHAT（通过命名和结构），注释只说明 WHY（为什么这么实现，有什么约束或历史原因）。

**Why:** WHAT 在读代码时就可以看到，WHY 却无法从代码本身推断。如果注释只是重复代码在做什么，那它就是噪音。

**How to apply:** 写完一段复杂逻辑后，检查是否有需要说明"为什么是这种写法而不是另一种"的地方。有就加注释，没有就不要加。

---

## PY08 pyright 依赖环境

pyright 需要正确的 Python 环境才能解析导入。依赖不在当前项目时，找同级或上级 `KiraAI-src/`：

- `KiraAI-jmdown-plugin/` 无 `pyproject.toml` → 自动用 `../KiraAI-src/` 的 uv 环境
- `KiraAI-src/` 有 `pyproject.toml` 和完整依赖 → 作为项目根
- 其他布局：上溯目录树找最近的 `pyproject.toml` / `uv.lock`

---

## PY09 Stop 前验证清单

**Rule:** 结束任务前，必须运行以下验证，确保全部通过后才能 Stop。

**Why:** 这些检查原来由 stop hook 硬编码执行，现在移入 rules 由模型自行执行。模型自己跑比 hook 跑更灵活——可以选择只检查改动的文件，而不是跑全量。

**How to apply:** 在尝试 Stop 前，按优先级运行以下命令。全部通过后方可结束：

1. **ruff 检查改动的文件**（语法 + 风格）：
   ```bash
   ruff check <改动的文件...>
   ```
   不通过则修复后再继续。

2. **pyright 类型检查改动的文件**（依赖 KiraAI-src 环境）：
   ```bash
   cd <项目根> && pyright <改动的文件...>
   ```
   如果文件在 `KiraAI-jmdown-plugin/` 下，先 `cd ../KiraAI-src` 让 pyright 找到依赖。
   过滤 `reportMissingImports` 和 `reportOptional*` 类已知预存错误。

3. **unittest / pytest 跑测试**：
   ```bash
   uv run python -m unittest discover -v
   ```
   或
   ```bash
   uv run pytest -q
   ```

4. **Python 语法检查**（只编译不导入）：
   ```bash
   uv run python -m py_compile <文件>
   ```

全部通过后再结束本轮。若有失败，修复后重跑。
