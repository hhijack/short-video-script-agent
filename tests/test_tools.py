# -*- coding: utf-8 -*-
"""工具层单元测试：素材检索、素材读取、时长估算、规则检查（含反幻觉 R3）。"""
import pytest

from src.data_store import MaterialStore
from src.tools import ToolRegistry, ToolError, tool_check_rules


@pytest.fixture()
def store():
    return MaterialStore()


@pytest.fixture()
def registry():
    return ToolRegistry()


# ---------------------------------------------------------------- 素材检索

def test_search_hits_all_materials(store):
    hits = store.search("映记 新手 出片", top_k=5)
    assert [h["id"] for h in hits] == ["P01", "P02", "P03", "P04", "P05"]
    assert hits[0]["id"] == "P01" and hits[0]["score"] >= 2  # 命中「映记」「新手」


def test_search_empty_then_rewritten_query_hits(store):
    """检索无结果 → 同义改写后能命中（Mock 模型改写策略的数据基础）。"""
    assert store.search("剪片", top_k=5) == []
    hits = store.search("剪辑", top_k=5)
    assert "P02" in [h["id"] for h in hits]


def test_search_irrelevant_topic_empty(store):
    assert store.search("量子计算机维护 vlog", top_k=5) == []


def test_search_validates_args(registry):
    obs = registry.execute("search_materials", {"query": "  "})
    assert not obs["ok"] and obs["error_type"] == "ToolError"


# ---------------------------------------------------------------- 素材读取

def test_read_material_returns_full_text(store):
    m = store.get("P01")
    assert m["facts"] == ["映记 App 支持自动生成字幕"]  # P01 严格保持仅此一条事实
    assert m["effect_data"] == []


def test_read_unknown_material_errors(registry):
    obs = registry.execute("read_material", {"material_id": "P99"})
    assert not obs["ok"] and "不存在" in obs["error"]
    assert obs["args"] == {"material_id": "P99"}  # 观察含 args 回显


# ---------------------------------------------------------------- 时长估算

def test_estimate_duration_math():
    obs = ToolRegistry().execute("estimate_duration", {"scenes": [
        {"id": "S1", "narration": "刷了三个小时的短视频，还是剪不出一条能发的片子？"},
    ]})
    assert obs["ok"]
    r = obs["result"]
    assert r["scenes"][0]["narration_chars"] == 24
    assert r["scenes"][0]["duration_sec"] == 6.0  # 24 字 / 4 字每秒


def test_estimate_duration_min_scene_floor():
    obs = ToolRegistry().execute("estimate_duration", {"scenes": [{"narration": "短"}]})
    assert obs["result"]["scenes"][0]["duration_sec"] == 1.5


def test_estimate_duration_rejects_bad_args(registry):
    assert not registry.execute("estimate_duration", {"scenes": []})["ok"]
    assert not registry.execute("estimate_duration", {"scenes": [{"narration": " "}]})["ok"]


# ---------------------------------------------------------------- 规则检查

def _script(total, scenes, target=30):
    return {"title": "测试脚本", "scenes": scenes,
            "target_duration_sec": target, "estimated_total_sec": total}


def test_check_rules_pass_on_compliant_script(store):
    script = _script(28.0, [
        {"id": "S1", "narration": "映记支持自动生成字幕。", "subtitle": "自动生成字幕",
         "duration_sec": 5.0, "material_ids": ["P01"]},
        {"id": "S2", "narration": "平均成片时间 3 分钟。", "subtitle": "3 分钟成片",
         "duration_sec": 5.0, "material_ids": ["P05"]},
    ])
    r = tool_check_rules(store, script)
    assert r["pass"], r["violations"]


def test_check_rules_r1_duration_over():
    r = tool_check_rules(MaterialStore(), _script(38.0, [
        {"id": "S1", "narration": "开场。", "subtitle": "开场", "duration_sec": 38.0, "material_ids": []},
    ]))
    assert not r["pass"]
    v = [x for x in r["violations"] if x["rule_id"] == "R1_DURATION"]
    assert v and "38.0s" in v[0]["message"] and "超出" in v[0]["message"]


def test_check_rules_r2_banned_word():
    r = tool_check_rules(MaterialStore(), _script(28.0, [
        {"id": "S1", "narration": "这是最最好用的剪辑。", "subtitle": "最好用",
         "duration_sec": 5.0, "material_ids": []},
    ]))
    assert any(x["rule_id"] == "R2_BANNED_WORDS" for x in r["violations"])


def test_check_rules_r3_unsupported_effect_claim_is_violation(store):
    """反幻觉核心用例：素材库无「效率提升 80%」，引用 P01 后仍必须判违规。"""
    script = _script(28.0, [
        {"id": "S1", "narration": "映记支持自动生成字幕，效率提升 80%。",
         "subtitle": "效率提升 80%", "duration_sec": 7.0, "material_ids": ["P01"]},
    ])
    r = tool_check_rules(store, script)
    r3 = [x for x in r["violations"] if x["rule_id"] == "R3_FACT_TRACEABILITY"]
    assert r3 and any("80%" in x["message"] for x in r3)
    assert any("效率" in x["message"] for x in r3)


def test_check_rules_r3_supported_claim_passes(store):
    """「3 分钟」能追溯到 P05 原文，不违规。"""
    script = _script(28.0, [
        {"id": "S1", "narration": "平均成片时间 3 分钟。", "subtitle": "3 分钟成片",
         "duration_sec": 5.0, "material_ids": ["P05"]},
    ])
    assert not [x for x in tool_check_rules(store, script)["violations"]
                if x["rule_id"] == "R3_FACT_TRACEABILITY"]


def test_check_rules_r3_claim_without_any_citation(store):
    script = _script(28.0, [
        {"id": "S1", "narration": "成片只要 3 分钟。", "subtitle": "3 分钟",
         "duration_sec": 5.0, "material_ids": []},
    ])
    assert any(x["rule_id"] == "R3_FACT_TRACEABILITY" for x in tool_check_rules(store, script)["violations"])


def test_check_rules_r4_brand_scene_needs_citation(store):
    script = _script(28.0, [
        {"id": "S1", "narration": "打开映记试试。", "subtitle": "映记",
         "duration_sec": 5.0, "material_ids": []},
    ])
    assert any(x["rule_id"] == "R4_PRODUCT_CLAIM_CITATION" for x in tool_check_rules(store, script)["violations"])


def test_check_rules_r5_unknown_material_id(store):
    script = _script(28.0, [
        {"id": "S1", "narration": "开场。", "subtitle": "开场", "duration_sec": 5.0, "material_ids": ["P99"]},
    ])
    assert any(x["rule_id"] == "R5_MATERIAL_EXISTS" for x in tool_check_rules(store, script)["violations"])


def test_check_rules_r6_speed(store):
    script = _script(28.0, [
        {"id": "S1", "narration": "十个字十个字十个字十个字十个字十", "subtitle": "快",
         "duration_sec": 1.5, "material_ids": []},
    ])
    assert any(x["rule_id"] == "R6_NARRATION_SPEED" for x in tool_check_rules(store, script)["violations"])


# ---------------------------------------------------------------- 故障注入

def test_injected_failure_observation(registry):
    registry.set_failures({"estimate_duration": 2})
    for remaining in (1, 0):
        obs = registry.execute("estimate_duration", {"scenes": [{"narration": "x"}]})
        assert not obs["ok"] and obs["error_type"] == "ToolFailure"
        assert f"剩余注入失败次数 {remaining}" in obs["error"]
    obs = registry.execute("estimate_duration", {"scenes": [{"narration": "x"}]})
    assert obs["ok"]  # 注入次数耗尽后恢复正常


def test_unknown_tool_name(registry):
    obs = registry.execute("no_such_tool", {})
    assert not obs["ok"] and "未知工具" in obs["error"]


def test_set_failures_rejects_unknown_tool(registry):
    with pytest.raises(ToolError):
        registry.set_failures({"nope": 1})
