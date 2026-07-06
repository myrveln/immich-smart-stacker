from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "immich-smart-stacker.py"
spec = importlib.util.spec_from_file_location("immich_smart_stacker", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load module spec from {MODULE_PATH}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


ImmichClient = module.ImmichClient
SmartStacker = module.SmartStacker


def test_normalize_api_url():
    assert ImmichClient._normalize_api_url("http://host:2283") == "http://host:2283/api"
    assert ImmichClient._normalize_api_url("http://host:2283/api") == "http://host:2283/api"


def test_extract_assets_page_dict_shape():
    items, next_page = ImmichClient._extract_assets_page({"assets": {"items": [{"id": "a"}], "nextPage": 2}})
    assert items == [{"id": "a"}]
    assert next_page == 2


def test_extract_assets_page_list_shape():
    items, next_page = ImmichClient._extract_assets_page({"assets": [{"id": "a"}]})
    assert items == [{"id": "a"}]
    assert next_page is None


def test_signature_is_order_insensitive():
    first = SmartStacker._signature(["b", "a", "a"])
    second = SmartStacker._signature(["a", "b"])
    assert first == second


def test_merge_overlapping_sets():
    merged = SmartStacker._merge_overlapping_sets([{"a", "b"}, {"b", "c"}, {"x"}])
    assert {frozenset(group) for group in merged} == {frozenset({"a", "b", "c"}), frozenset({"x"})}
