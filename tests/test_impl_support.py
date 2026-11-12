"""实现方能力矩阵、配置文案与失败信息的一致性守卫。

核查新实现一个框架后只改 ``impl_support.py`` 是不够的：``schema.json`` 的
开关提示、README / config-reference 的表格都是给人看的，很容易漏改。这里把
「文案必须覆盖矩阵」变成可失败的测试；同时锁定失败信息的语义：如实转述
实现端响应、不臆断原因、不编造体积上限、不留空字符串。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import _core_stub

_core_stub.bootstrap()

from jmdown.impl_support import (  # noqa: E402
    BASE64_IMPLS,
    PATH_ONLY_IMPLS,
    STANDARD_IMPLS,
    STREAM_IMPLS,
    SUPPORTED_IMPLS,
    UPLOAD_METHODS,
    diagnosis_hint,
    format_action_failure,
    format_no_response,
    is_ok_response,
    response_summary,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))["upload"]["fields"]


class MatrixTest(unittest.TestCase):
    def test_all_names_present_in_generated_lists(self):
        standard_names = [i.name for i in SUPPORTED_IMPLS if i.standard]
        self.assertEqual(STANDARD_IMPLS, "、".join(standard_names))
        self.assertEqual(STREAM_IMPLS, "、".join(i.name for i in SUPPORTED_IMPLS if i.stream))
        self.assertEqual(BASE64_IMPLS, "、".join(i.name for i in SUPPORTED_IMPLS if i.base64))
        self.assertEqual(
            PATH_ONLY_IMPLS,
            "、".join(i.name for i in SUPPORTED_IMPLS if i.standard and not i.base64),
        )

    def test_stream_impls_are_subset_of_standard_impls(self):
        stream_names = {i.name for i in SUPPORTED_IMPLS if i.stream}
        standard_names = {i.name for i in SUPPORTED_IMPLS if i.standard}
        self.assertTrue(stream_names <= standard_names)
        # 分片不是「所有实现都能开的优化」
        self.assertLess(len(stream_names), len(standard_names))

    def test_schema_method_hint_lists_every_supported_impl(self):
        field = SCHEMA["upload_method"]
        for hint in (field["hint"], field["locales"]["zh"]["hint"]):
            for impl in SUPPORTED_IMPLS:
                with self.subTest(impl=impl.name):
                    self.assertIn(impl.name, hint)
            self.assertIn("upload_private_file", hint)
            self.assertIn("upload_file_stream", hint)

    def test_schema_method_hint_documents_every_option(self):
        field = SCHEMA["upload_method"]
        self.assertEqual(field["options"], list(UPLOAD_METHODS))
        self.assertEqual(field["default"], "path")
        for hint in (field["hint"], field["locales"]["zh"]["hint"]):
            for method in UPLOAD_METHODS:
                with self.subTest(method=method):
                    self.assertIn(method, hint)
            self.assertIn("进度未知", hint)
            self.assertIn("同机", hint)
            self.assertIn("不设本地体积上限", hint)

    def test_no_schema_field_can_reintroduce_a_size_gate(self):
        self.assertNotIn("standard_max_mb", SCHEMA)
        self.assertNotIn("max_bytes", SCHEMA)
        upload_timeout_hint = SCHEMA["upload_timeout"]["hint"]
        self.assertIn("诊断指引", upload_timeout_hint)

    def test_readme_matrix_matches_source(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        start = text.index("### OneBot 实现支持矩阵")
        section = text[start : text.index("\n## ", start)]
        rows = [ln for ln in section.splitlines() if ln.startswith("|")]

        for impl in SUPPORTED_IMPLS:
            with self.subTest(impl=impl.name):
                row = next(
                    (ln for ln in rows if ln.split("|")[1].strip().startswith(impl.name)),
                    None,
                )
                self.assertIsNotNone(row, "README 矩阵缺少该实现")
                cells = [c.strip() for c in row.split("|")[2:6]]
                expected = ["✅" if flag else "❌" for flag in (impl.standard, impl.path, impl.base64, impl.stream)]
                self.assertEqual(cells, expected)

    def test_config_reference_mentions_every_impl(self):
        text = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
        for impl in SUPPORTED_IMPLS:
            with self.subTest(impl=impl.name):
                self.assertIn(impl.name, text)
        self.assertNotIn("### standard_max_mb", text)

    def test_diagnosis_hint_lists_impls(self):
        hint = diagnosis_hint()
        for impl in SUPPORTED_IMPLS:
            if impl.standard:
                self.assertIn(impl.name, hint)
        self.assertIn(STREAM_IMPLS, hint)
        self.assertIn("同机", hint)
        self.assertIn("Max payload size exceeded", hint)


class FailureMessageTest(unittest.TestCase):
    """失败文本会直达 LLM：必须自包含、如实、不臆断、不为空。"""

    def test_action_failure_quotes_implementation_response(self):
        msg = format_action_failure(
            "upload_private_file",
            {"status": "failed", "retcode": 1404, "message": "permission denied", "data": {}},
            stage="标准上传",
        )
        self.assertIn("upload_private_file", msg)
        self.assertIn("status='failed'", msg)
        self.assertIn("retcode=1404", msg)
        self.assertIn("permission denied", msg)
        self.assertIn("不设本地体积上限", msg)
        self.assertNotIn("文件太大", msg)
        self.assertNotIn("10MB", msg)

    def test_action_failure_handles_empty_response(self):
        msg = format_action_failure("upload_group_file", {}, stage="实现端发送")
        self.assertTrue(msg.strip())
        self.assertIn("响应体为空", msg)
        self.assertIn("upload_group_file", msg)

    def test_no_response_reports_facts_not_a_fabricated_limit(self):
        msg = format_no_response("upload_group_file", 420, stage="实现端发送", size_bytes=40 * 1024 * 1024)
        self.assertIn("420s", msg)
        self.assertIn("40.0 MB", msg)
        self.assertIn("未收到实现端响应", msg)
        self.assertIn("可能原因", msg)
        self.assertIn("Max payload size exceeded", msg)
        self.assertNotIn("文件太大", msg)
        self.assertNotIn("超过内联上限", msg)

    def test_every_failure_text_is_non_empty_and_actionable(self):
        for msg in (
            format_action_failure("upload_private_file", {}, stage="标准上传"),
            format_no_response("upload_file_stream", 30, stage="分片 1/2"),
            response_summary({}),
        ):
            with self.subTest(msg=msg[:24]):
                self.assertTrue(msg.strip())


class OkResponseTest(unittest.TestCase):
    """OneBot 响应信封兼容：status 与 retcode 两种写法都要认。"""

    def test_status_ok(self):
        self.assertTrue(is_ok_response({"status": "ok", "retcode": 0}))

    def test_retcode_only(self):
        self.assertTrue(is_ok_response({"retcode": 0, "data": None}))
        self.assertTrue(is_ok_response({"retcode": "0"}))

    def test_status_wins_over_retcode(self):
        # 只回 status=failed 却带 retcode=0 时不能算成功
        self.assertFalse(is_ok_response({"status": "failed", "retcode": 0}))
        self.assertFalse(is_ok_response({"status": "async", "retcode": 1}))

    def test_malformed_or_empty(self):
        self.assertFalse(is_ok_response({}))
        self.assertFalse(is_ok_response({"retcode": "not-a-number"}))
        self.assertFalse(is_ok_response({"retcode": None}))


if __name__ == "__main__":
    unittest.main()
