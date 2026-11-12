"""元数据契约测试 —— manifest / schema / README / 源码之间的漂移守卫。

插件没有编译期校验，最容易出的低级故障是「四处文档各说一套」：
版本号、依赖列表、配置键、工具名。这里把跨文件的不变式固化下来，
让 CI 在任何一处漏改时报错，而不是等用户装上插件才发现。
"""

from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path
from typing import Any

import _core_stub

_core_stub.bootstrap()

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "manifest.json"
SCHEMA_PATH = ROOT / "schema.json"
README_PATH = ROOT / "README.md"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
MAIN_PATH = ROOT / "main.py"

# 与 schema.json 的 type 取值保持一致（core.config.config_field 支持的控件）
FIELD_TYPES = {"section", "integer", "switch", "string", "select", "float"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_leaf_keys(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """展开所有分节下的叶子字段：key -> 字段定义。"""
    leaves: dict[str, dict[str, Any]] = {}
    for section in schema.values():
        for key, field in section.get("fields", {}).items():
            leaves[key] = field
    return leaves


def readme_config_keys(readme: str) -> set[str]:
    return set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", readme, flags=re.MULTILINE))


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_json(MANIFEST_PATH)

    def test_required_keys(self) -> None:
        for key in (
            "display_name",
            "plugin_id",
            "version",
            "author",
            "description",
            "core_version",
            "repo",
        ):
            self.assertIn(key, self.manifest, f"manifest 缺少必需字段 {key}")

    def test_plugin_id_is_safe_identifier(self) -> None:
        # plugin_id 会被用作配置文件名（data/config/plugins/<id>.json）与注册表键，
        # 含路径分隔符或空格会静默写错位置。
        # 注意：不能用「plugin_id == 目录名」来校验 —— CI checkout 的目录名是仓库名
        # （KiraAI-jmdown-plugin），与安装目录（jmdown）本来就不一致。
        self.assertRegex(self.manifest["plugin_id"], r"^[a-z][a-z0-9_]*$")

    def test_readme_clones_into_plugin_id_directory(self) -> None:
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertRegex(
            readme,
            rf"git clone \S+\s+{self.manifest['plugin_id']}\b",
            "安装步骤必须把仓库克隆到与 plugin_id 同名的目录",
        )

    def test_version_is_semver(self) -> None:
        self.assertRegex(self.manifest["version"], r"^\d+\.\d+\.\d+$")

    def test_core_version_is_specifier(self) -> None:
        if importlib.util.find_spec("packaging") is None:
            self.skipTest("packaging 未安装")
        from packaging.specifiers import SpecifierSet

        SpecifierSet(self.manifest["core_version"])  # 解析失败会直接抛异常

    def test_entry_script_exists(self) -> None:
        self.assertTrue(MAIN_PATH.is_file(), "缺少 main.py 入口")

    def test_requirements_match_manifest_dependencies(self) -> None:
        # manifest.dependencies 与 requirements.txt 是两份手写副本，core 只读后者；
        # 依赖实际安装走 requirements.txt，漂移后 manifest 会误导阅读者。
        declared = {
            line.strip()
            for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertEqual(set(self.manifest.get("dependencies", [])), declared)


class ReadmeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = README_PATH.read_text(encoding="utf-8")
        self.manifest = load_json(MANIFEST_PATH)

    def test_badge_version_matches_manifest(self) -> None:
        badges = re.findall(r"badge/version-([0-9.]+)-", self.readme)
        self.assertTrue(badges, "README 缺少版本徽章")
        self.assertEqual(badges[0], self.manifest["version"])

    def test_relative_links_resolve(self) -> None:
        links = re.findall(r"\]\((?!https?://|#)([^)]+)\)", self.readme)
        broken = [link for link in links if not (ROOT / link).exists()]
        self.assertEqual(broken, [], f"README 中存在失效的相对链接: {broken}")

    def test_documents_every_config_key(self) -> None:
        leaves = schema_leaf_keys(load_json(SCHEMA_PATH))
        missing = set(leaves) - readme_config_keys(self.readme)
        self.assertEqual(missing, set(), f"README 配置表缺少字段: {sorted(missing)}")

    def test_documents_every_tool(self) -> None:
        tools = set(re.findall(r'@tool\(\s*"([a-z_]+)"', MAIN_PATH.read_text(encoding="utf-8")))
        documented = set(re.findall(r"### 工具：`([a-z_]+)`", self.readme))
        self.assertEqual(tools - documented, set(), "README 缺少工具文档")
        self.assertEqual(documented - tools, set(), "README 文档了不存在的工具")


class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = load_json(SCHEMA_PATH)

    def test_sections_are_well_formed(self) -> None:
        for name, section in self.schema.items():
            with self.subTest(section=name):
                self.assertEqual(section.get("type"), "section")
                self.assertIn("name", section)
                self.assertIsInstance(section.get("fields"), dict)
                self.assertTrue(section["fields"], "空分节没有意义")

    def test_fields_are_well_formed(self) -> None:
        for key, field in schema_leaf_keys(self.schema).items():
            with self.subTest(field=key):
                self.assertIn(field.get("type"), FIELD_TYPES, f"{key} 的 type 非法")
                self.assertIn("name", field, f"{key} 缺少 name")
                self.assertIn("default", field, f"{key} 缺少 default")
                if field["type"] == "select":
                    self.assertIn("options", field, f"{key} 是 select 但缺少 options")

    def test_schema_keys_match_code_usage(self) -> None:
        # 双向校验：声明了没读 = 死配置；读了没声明 = 只能吃默认值。
        leaves = set(schema_leaf_keys(self.schema))
        code_keys = set(re.findall(r'_g\(\s*\w+\s*,\s*"([a-z_]+)"', MAIN_PATH.read_text(encoding="utf-8")))
        self.assertEqual(leaves - code_keys, set(), "schema 声明了但代码未读取的配置")
        self.assertEqual(code_keys - leaves, set(), "代码读取了但 schema 未声明的配置")

    def test_switch_defaults_are_bool(self) -> None:
        for key, field in schema_leaf_keys(self.schema).items():
            if field["type"] == "switch":
                with self.subTest(field=key):
                    self.assertIsInstance(field["default"], bool)


class DocsTest(unittest.TestCase):
    """docs/ 是 VitePress 站点，链接与配置项同属「文档漂移」高发区。"""

    def test_config_reference_covers_all_keys(self) -> None:
        reference = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
        leaves = schema_leaf_keys(load_json(SCHEMA_PATH))
        missing = [key for key in leaves if key not in reference]
        self.assertEqual(missing, [], f"docs/config-reference.md 缺少字段: {missing}")

    def test_index_links_resolve(self) -> None:
        index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\(\./([a-z-]+)\)", index)
        broken = [name for name in links if not (ROOT / "docs" / f"{name}.md").exists()]
        self.assertEqual(broken, [], f"docs/index.md 存在失效链接: {broken}")


class PackageEntryTest(unittest.TestCase):
    def test_init_does_not_eagerly_import_main(self) -> None:
        """__init__.py 必须保持惰性。

        main.py 在导入期依赖 core 与 jmcomic；若 __init__.py 在模块层做
        `from .main import`，任何把它当顶层模块加载的收集器都会失败，把
        「环境缺失」伪装成插件缺陷（CI 上 100% 复现）。
        """
        spec = importlib.util.spec_from_file_location("_jmdown_entry_probe", ROOT / "__init__.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        self.assertIn("JMdownPlugin", module.__all__)


if __name__ == "__main__":
    unittest.main()
