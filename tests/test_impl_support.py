"""实现方能力矩阵、配置文案与失败信息的一致性守卫。

核查新实现一个框架后只改 ``impl_support.py`` 是不够的：``schema.json`` 的
开关提示、README / config-reference 的表格都是给人看的，很容易漏改。这里把
「文案必须覆盖矩阵」变成可失败的测试；同时锁定失败信息的语义：如实转述
实现端响应、不臆断原因、不编造体积上限、不留空字符串。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jmdown.impl_support import (
    BASE64_IMPLS,
    PATH_ONLY_IMPLS,
    STANDARD_IMPLS,
    STREAM_IMPLS,
    SUPPORTED_IMPLS,
    UPLOAD_METHODS,
    ImplSupport,
    diagnosis_hint,
    format_action_failure,
    format_no_response,
    is_ok_response,
    response_summary,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))["upload"]["fields"]
METHOD_FIELD = SCHEMA["upload_method"]
# 两套 hint 都要满足同一组断言：WebUI 与 LLM 各读一套，漏改任一侧都是 bug
METHOD_HINTS = (METHOD_FIELD["hint"], METHOD_FIELD["locales"]["zh"]["hint"])
IMPL_PARAMS = pytest.mark.parametrize("impl", SUPPORTED_IMPLS, ids=[i.name for i in SUPPORTED_IMPLS])
HINT_PARAMS = pytest.mark.parametrize("hint", METHOD_HINTS, ids=["default", "zh"])


# ── 能力矩阵 ──────────────────────────────────────────────────────────


def test_all_names_present_in_generated_lists() -> None:
    standard_names = [i.name for i in SUPPORTED_IMPLS if i.standard]
    assert STANDARD_IMPLS == "、".join(standard_names)
    assert STREAM_IMPLS == "、".join(i.name for i in SUPPORTED_IMPLS if i.stream)
    assert BASE64_IMPLS == "、".join(i.name for i in SUPPORTED_IMPLS if i.base64)
    assert PATH_ONLY_IMPLS == "、".join(i.name for i in SUPPORTED_IMPLS if i.standard and not i.base64)


def test_stream_impls_are_subset_of_standard_impls() -> None:
    stream_names = {i.name for i in SUPPORTED_IMPLS if i.stream}
    standard_names = {i.name for i in SUPPORTED_IMPLS if i.standard}
    assert stream_names <= standard_names
    # 分片不是「所有实现都能开的优化」
    assert len(stream_names) < len(standard_names)


@IMPL_PARAMS
def test_schema_method_hint_lists_every_supported_impl(impl: ImplSupport) -> None:
    for hint in METHOD_HINTS:
        assert impl.name in hint
    assert "upload_private_file" in METHOD_FIELD["hint"]
    assert "upload_file_stream" in METHOD_FIELD["hint"]


@HINT_PARAMS
def test_schema_method_hint_documents_every_option(hint: str) -> None:
    for method in UPLOAD_METHODS:
        assert method in hint
    assert "进度未知" in hint
    assert "同机" in hint
    assert "不设本地体积上限" in hint


def test_schema_method_options_are_the_three_choices() -> None:
    assert METHOD_FIELD["options"] == list(UPLOAD_METHODS)
    assert METHOD_FIELD["default"] == "base64"


def test_no_schema_field_can_reintroduce_a_size_gate() -> None:
    assert "standard_max_mb" not in SCHEMA
    assert "max_bytes" not in SCHEMA
    assert "诊断指引" in SCHEMA["upload_timeout"]["hint"]


# ── 文档与矩阵同步 ────────────────────────────────────────────────────


def _readme_matrix_rows() -> list[str]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    start = text.index("### OneBot 实现支持矩阵")
    section = text[start : text.index("\n## ", start)]
    return [ln for ln in section.splitlines() if ln.startswith("|")]


@IMPL_PARAMS
def test_readme_matrix_matches_source(impl: ImplSupport) -> None:
    rows = _readme_matrix_rows()
    row = next((ln for ln in rows if ln.split("|")[1].strip().startswith(impl.name)), None)
    assert row is not None, "README 矩阵缺少该实现"
    cells = [c.strip() for c in row.split("|")[2:6]]
    expected = ["✅" if flag else "❌" for flag in (impl.standard, impl.path, impl.base64, impl.stream)]
    assert cells == expected


@IMPL_PARAMS
def test_config_reference_mentions_every_impl(impl: ImplSupport) -> None:
    text = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
    assert impl.name in text


def test_config_reference_has_no_size_gate_leftover() -> None:
    text = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
    assert "### standard_max_mb" not in text


def test_diagnosis_hint_lists_impls() -> None:
    hint = diagnosis_hint()
    for impl in SUPPORTED_IMPLS:
        if impl.standard:
            assert impl.name in hint
    assert STREAM_IMPLS in hint
    assert "同机" in hint
    assert "Max payload size exceeded" in hint


# ── 失败文本：直达 LLM，必须自包含、如实、不臆断 ──────────────────────


def test_action_failure_quotes_implementation_response() -> None:
    msg = format_action_failure(
        "upload_private_file",
        {"status": "failed", "retcode": 1404, "message": "permission denied", "data": {}},
        stage="标准上传",
    )
    assert "upload_private_file" in msg
    assert "status='failed'" in msg
    assert "retcode=1404" in msg
    assert "permission denied" in msg
    assert "不设本地体积上限" in msg
    assert "文件太大" not in msg
    assert "10MB" not in msg


def test_action_failure_handles_empty_response() -> None:
    msg = format_action_failure("upload_group_file", {}, stage="实现端发送")
    assert msg.strip()
    assert "响应体为空" in msg
    assert "upload_group_file" in msg


def test_no_response_reports_facts_not_a_fabricated_limit() -> None:
    msg = format_no_response("upload_group_file", 420, stage="实现端发送", size_bytes=40 * 1024 * 1024)
    assert "420s" in msg
    assert "40.0 MB" in msg
    assert "未收到实现端响应" in msg
    assert "可能原因" in msg
    assert "Max payload size exceeded" in msg
    assert "文件太大" not in msg
    assert "超过内联上限" not in msg


@pytest.mark.parametrize(
    "msg",
    [
        format_action_failure("upload_private_file", {}, stage="标准上传"),
        format_no_response("upload_file_stream", 30, stage="分片 1/2"),
        response_summary({}),
    ],
    ids=["action_failure", "no_response", "response_summary"],
)
def test_every_failure_text_is_non_empty_and_actionable(msg: str) -> None:
    assert msg.strip(), "失败文本不能为空——LLM 收到空字符串等于没有失败原因"


# ── OneBot 响应信封兼容：status 与 retcode 两种写法都要认 ─────────────


@pytest.mark.parametrize(
    "resp",
    [
        {"status": "ok", "retcode": 0},
        {"retcode": 0, "data": None},
        {"retcode": "0"},
    ],
    ids=["status_ok", "retcode_int", "retcode_str"],
)
def test_ok_response_accepted(resp: dict) -> None:
    assert is_ok_response(resp)


@pytest.mark.parametrize(
    "resp",
    [
        # 只回 status=failed 却带 retcode=0 时不能算成功
        {"status": "failed", "retcode": 0},
        {"status": "async", "retcode": 1},
        {},
        {"retcode": "not-a-number"},
        {"retcode": None},
    ],
    ids=["status_wins", "status_async", "empty", "retcode_nan", "retcode_none"],
)
def test_bad_response_rejected(resp: dict) -> None:
    assert not is_ok_response(resp)
