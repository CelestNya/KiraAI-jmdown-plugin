"""元数据契约测试 —— manifest / schema / README / 源码之间的漂移守卫。

插件没有编译期校验，最容易出的低级故障是「四处文档各说一套」：
版本号、依赖列表、配置键、工具名。这里把跨文件的不变式固化下来，
让 CI 在任何一处漏改时报错，而不是等用户装上插件才发现。
"""

from __future__ import annotations

import importlib.util
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

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


# schema.json 是静态契约文件，导入期读一次即可；parametrize 的 id 取自它
SCHEMA_DATA = load_json(SCHEMA_PATH)
LEAVES = schema_leaf_keys(SCHEMA_DATA)


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return load_json(MANIFEST_PATH)


@pytest.fixture(scope="module")
def readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return SCHEMA_DATA


# ── manifest.md 必需字段与语义 ────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["display_name", "plugin_id", "version", "author", "description", "core_version", "repo"],
)
def test_manifest_has_required_key(manifest: dict[str, Any], key: str) -> None:
    assert key in manifest, f"manifest 缺少必需字段 {key}"


def test_plugin_id_is_safe_identifier(manifest: dict[str, Any]) -> None:
    # plugin_id 会被用作配置文件名（data/config/plugins/<id>.json）与注册表键，
    # 含路径分隔符或空格会静默写错位置。
    # 注意：不能用「plugin_id == 目录名」来校验 —— CI checkout 的目录名是仓库名
    # （KiraAI-jmdown-plugin），与安装目录（jmdown）本来就不一致。
    assert re.fullmatch(r"[a-z][a-z0-9_]*", manifest["plugin_id"])


def test_readme_clones_into_plugin_id_directory(manifest: dict[str, Any], readme: str) -> None:
    assert re.search(rf"git clone \S+\s+{manifest['plugin_id']}\b", readme), (
        "安装步骤必须把仓库克隆到与 plugin_id 同名的目录"
    )


def test_version_is_semver(manifest: dict[str, Any]) -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_core_version_is_specifier(manifest: dict[str, Any]) -> None:
    if importlib.util.find_spec("packaging") is None:
        pytest.skip("packaging 未安装")
    from packaging.specifiers import SpecifierSet

    SpecifierSet(manifest["core_version"])  # 解析失败会直接抛异常


def test_entry_script_exists() -> None:
    assert MAIN_PATH.is_file(), "缺少 main.py 入口"


def test_requirements_match_manifest_dependencies(manifest: dict[str, Any]) -> None:
    # manifest.dependencies 与 requirements.txt 是两份手写副本，core 只读后者；
    # 依赖实际安装走 requirements.txt，漂移后 manifest 会误导阅读者。
    declared = {
        line.strip()
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert set(manifest.get("dependencies", [])) == declared


# ── 图标契约（镜像 core 的 resolve_manifest_icon_path 规则） ──────────

ICON_KEYS = ("icon",)
# 与 core/utils/path_utils.py 的 _ICON_EXTENSIONS 保持一致
ALLOWED_ICON_SUFFIXES = {".avif", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


@pytest.mark.parametrize("key", ICON_KEYS)
def test_icon_field_resolves_to_local_file(manifest: dict[str, Any], key: str) -> None:
    # core 只接受「相对 manifest 目录且不越界」的路径；绝对路径或越界路径会被
    # 静默判成 None，前端就退回没有图标的样子 —— 这种失败在开发机上完全看不出来。
    value = manifest.get(key)
    assert isinstance(value, str) and value.strip(), f"manifest 缺少 {key}"
    path = Path(value)
    assert not path.is_absolute(), f"{key} 不能是绝对路径: {value}"
    assert ".." not in path.parts, f"{key} 不能越出插件目录: {value}"
    target = (ROOT / path).resolve()
    assert target.is_relative_to(ROOT), f"{key} 解析后越出插件目录: {value}"
    assert target.suffix.lower() in ALLOWED_ICON_SUFFIXES, f"{key} 扩展名不在 core 白名单内: {value}"
    assert target.is_file(), f"{key} 指向的文件不存在: {value}"


@pytest.mark.parametrize("key", ICON_KEYS)
def test_icon_asset_suits_the_card_size(manifest: dict[str, Any], key: str) -> None:
    # 卡片里图标按 40×40、object-fit:contain 显示，所以源图必须是正方形
    # （非正方形会在 contain 下留白），且边长不低于 128（4x 屏需要 160px）。
    target = ROOT / manifest[key]
    if target.suffix.lower() == ".svg":
        root = ET.parse(target).getroot()
        assert root.tag == "{http://www.w3.org/2000/svg}svg", f"{key} 根元素不是 svg"
        assert root.get("viewBox"), f"{key} 缺少 viewBox，缩放时会变形"
        # SVG 由前端 <img src> 加载，脚本不会执行；但它能携带外部引用，
        # 一个 href 指向远端地址就足以把插件面板变成外联信标。
        raw = target.read_text(encoding="utf-8").lower()
        assert "<script" not in raw, f"{key} 不能包含脚本"
        assert 'href="http' not in raw, f"{key} 不能外链外部资源"
        return

    from PIL import Image

    with Image.open(target) as im:
        assert im.width == im.height, f"{key} 必须是正方形，当前 {im.width}×{im.height}"
        assert im.width >= 128, f"{key} 分辨率过低（{im.width}px），40px 卡片在 4x 屏上需要 160px"


# ── README 与源码同步 ─────────────────────────────────────────────────


def test_badge_version_matches_manifest(manifest: dict[str, Any], readme: str) -> None:
    badges = re.findall(r"badge/version-([0-9.]+)-", readme)
    assert badges, "README 缺少版本徽章"
    assert badges[0] == manifest["version"]


def test_relative_links_resolve(readme: str) -> None:
    links = re.findall(r"\]\((?!https?://|#)([^)]+)\)", readme)
    broken = [link for link in links if not (ROOT / link).exists()]
    assert broken == [], f"README 中存在失效的相对链接: {broken}"


def test_documents_every_config_key(readme: str) -> None:
    missing = set(LEAVES) - readme_config_keys(readme)
    assert missing == set(), f"README 配置表缺少字段: {sorted(missing)}"


def test_documents_every_tool(readme: str) -> None:
    tools = set(re.findall(r'@tool\(\s*"([a-z_]+)"', MAIN_PATH.read_text(encoding="utf-8")))
    documented = set(re.findall(r"### 工具：`([a-z_]+)`", readme))
    assert tools - documented == set(), "README 缺少工具文档"
    assert documented - tools == set(), "README 文档了不存在的工具"


# ── schema 结构与代码读取一致性 ───────────────────────────────────────


@pytest.mark.parametrize("section_name", list(SCHEMA_DATA))
def test_sections_are_well_formed(schema: dict[str, Any], section_name: str) -> None:
    section = schema[section_name]
    assert section.get("type") == "section"
    assert "name" in section
    assert isinstance(section.get("fields"), dict)
    assert section["fields"], "空分节没有意义"


@pytest.mark.parametrize("key", list(LEAVES))
def test_fields_are_well_formed(schema: dict[str, Any], key: str) -> None:
    field = schema_leaf_keys(schema)[key]
    assert field.get("type") in FIELD_TYPES, f"{key} 的 type 非法"
    assert "name" in field, f"{key} 缺少 name"
    assert "default" in field, f"{key} 缺少 default"
    if field["type"] == "select":
        assert "options" in field, f"{key} 是 select 但缺少 options"


def test_schema_keys_match_code_usage(schema: dict[str, Any]) -> None:
    # 双向校验：声明了没读 = 死配置；读了没声明 = 只能吃默认值。
    leaves = set(schema_leaf_keys(schema))
    code_keys = set(re.findall(r'_g\(\s*\w+\s*,\s*"([a-z_]+)"', MAIN_PATH.read_text(encoding="utf-8")))
    assert leaves - code_keys == set(), "schema 声明了但代码未读取的配置"
    assert code_keys - leaves == set(), "代码读取了但 schema 未声明的配置"


def test_switch_defaults_are_bool() -> None:
    bad = [key for key, field in LEAVES.items() if field["type"] == "switch" and not isinstance(field["default"], bool)]
    assert bad == [], f"switch 字段默认值必须是 bool: {bad}"


# ── docs/ 站点（VitePress）链接与字段覆盖 ─────────────────────────────


def test_config_reference_covers_all_keys() -> None:
    reference = (ROOT / "docs" / "config-reference.md").read_text(encoding="utf-8")
    missing = [key for key in LEAVES if key not in reference]
    assert missing == [], f"docs/config-reference.md 缺少字段: {missing}"


def test_index_links_resolve() -> None:
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(\./([a-z-]+)\)", index)
    broken = [name for name in links if not (ROOT / "docs" / f"{name}.md").exists()]
    assert broken == [], f"docs/index.md 存在失效链接: {broken}"


# ── 包入口惰性 ────────────────────────────────────────────────────────


def test_init_does_not_eagerly_import_main() -> None:
    """__init__.py 必须保持惰性。

    main.py 在导入期依赖 core 与 jmcomic；若 __init__.py 在模块层做
    `from .main import`，任何把它当顶层模块加载的收集器都会失败，把
    「环境缺失」伪装成插件缺陷（CI 上 100% 复现）。
    """
    spec = importlib.util.spec_from_file_location("_jmdown_entry_probe", ROOT / "__init__.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "JMdownPlugin" in module.__all__
