# -*- coding: utf-8 -*-
"""端到端场景测试：4 个内置用例 + 步数上限 + 真实模型接入与动作解析（离线部分）。

覆盖 5 个必须场景：
  正常生成 -> case1；缺关键信息 -> case2；无素材 -> case3；
  工具失败 -> case4；超时长后的修正 -> case1（38.1s 压缩为 28.0s）。
"""
import json
from pathlib import Path

import pytest

from src.cli import CASES_DIR, run_request
from src.model import (ActionFormatError, ModelError, RealModel, build_model,
                       normalize_finish_output, rewrite_query)

CASES = {}


def load_case(name: str) -> dict:
    if name not in CASES:
        CASES[name] = json.loads((CASES_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return CASES[name]


def run_case(name: str, **kwargs) -> dict:
    meta = load_case(name)
    return run_request(meta["request"], meta.get("model", "mock"),
                       kwargs.pop("max_tool_calls", 12),
                       kwargs.pop("inject_failures", meta.get("inject_failures", {})), **kwargs)


def tool_seq(result: dict) -> list:
    return [s.get("tool") for s in result["trace"]["steps"] if s.get("action_type") == "call_tool"]


# ---------------------------------------------------------------- case1 正常生成（含超时长修正）

def test_case1_normal_generation_with_compression():
    result = run_case("case1_normal_30s_compress")
    assert result["status"] == "completed"
    out = result["output"]
    # 交付结构完整：标题、分镜、旁白、字幕、预计总时长、素材来源 ID、风险、状态
    assert out["title"] and out["scenes"] and out["risks"]
    for s in out["scenes"]:
        assert s["visual"] and s["narration"] and s["subtitle"] is not None
        assert isinstance(s["duration_sec"], (int, float))
    assert 18.0 <= out["estimated_total_sec"] <= 30.0
    assert out["estimated_total_sec"] == 28.0  # 参考样例口径：修正结果 28 秒
    assert "P01" in out["material_ids"]        # 参考样例：输出包含 P01 引用
    # 执行路径：检索 → 读取 → 首稿估算 → 检查失败 → 压缩稿估算 → 检查通过
    seq = tool_seq(result)
    assert seq[0] == "search_materials"
    assert seq.count("read_material") == 5
    assert seq == ["search_materials"] + ["read_material"] * 5 + \
        ["estimate_duration", "check_rules", "estimate_duration", "check_rules"]
    # 首稿 38.1s 超时（观察驱动压缩）
    est1 = result["trace"]["steps"][6]["observation"]["result"]
    assert est1["total_sec"] == 38.1 and est1["total_sec"] > 30
    # 压缩决策依据里同时引用了 R1 与 R3 违规
    compress_step = result["trace"]["steps"][8]  # 第 9 步 = 压缩稿估算
    assert compress_step["tool"] == "estimate_duration"
    assert "R1_DURATION" in compress_step["decision_basis"]
    assert "R3_FACT_TRACEABILITY" in compress_step["decision_basis"]
    # 反幻觉：每个分镜的数字/效果表述都能在引用素材原文找到（用规则检查工具复核）
    from src.tools import tool_check_rules
    from src.data_store import MaterialStore
    script = {"title": out["title"],
              "scenes": [{"id": s["id"], "narration": s["narration"], "subtitle": s["subtitle"],
                          "duration_sec": s["duration_sec"], "material_ids": s["material_ids"]}
                         for s in out["scenes"]],
              "target_duration_sec": out["target_duration_sec"],
              "estimated_total_sec": out["estimated_total_sec"]}
    assert tool_check_rules(MaterialStore(), script)["pass"]


# ---------------------------------------------------------------- case2 缺关键信息

def test_case2_missing_duration_requests_info():
    result = run_case("case2_missing_duration")
    assert result["status"] == "incomplete_missing_info"
    assert result["output"] is None
    trace = result["trace"]
    assert trace["tool_calls_used"] == 0  # 模型决定先请求补充，不猜默认值
    finish_step = trace["steps"][-1]
    assert finish_step["action_type"] == "finish"
    assert "目标时长" in finish_step["questions"][0]


# ---------------------------------------------------------------- case3 无素材

def test_case3_no_material_rewrites_then_asks():
    """模型因检索结果不同而改变动作：0 命中 → 改写查询再检索 → 仍 0 命中 → 请求补充。"""
    result = run_case("case3_no_material")
    assert result["status"] == "incomplete_no_material"
    seq = tool_seq(result)
    assert seq == ["search_materials", "search_materials"]
    q1 = result["trace"]["steps"][0]["tool_args"]["query"]
    q2 = result["trace"]["steps"][1]["tool_args"]["query"]
    assert q2 != q1  # 第二次检索使用了改写后的查询词
    assert result["trace"]["steps"][1]["decision_basis"].startswith("首次检索无结果")
    finish_step = result["trace"]["steps"][-1]
    assert "素材" in "".join(finish_step["questions"])


# ---------------------------------------------------------------- case4 工具失败

def test_case4_tool_failure_reports_clearly():
    """估算工具持续失败：重试一次仍失败 → 明确未完成，不伪造时长。"""
    result = run_case("case4_tool_failure")
    assert result["status"] == "incomplete_tool_failure"
    seq = tool_seq(result)
    assert seq == ["search_materials"] + ["read_material"] * 5 + \
        ["estimate_duration", "estimate_duration"]
    fails = [s for s in result["trace"]["steps"]
             if s.get("tool") == "estimate_duration"]
    assert all(not s["observation"]["ok"] for s in fails)
    assert result["output"] is None or "estimated_total_sec" not in (result["output"] or {})
    assert "未完成" in result["trace"]["final_reason"]


# ---------------------------------------------------------------- 步数上限

def test_step_limit_returns_explicit_incomplete():
    """达到工具调用上限必须显式返回未完成状态，不静默截断。"""
    result = run_case("case1_normal_30s_compress", max_tool_calls=6)
    assert result["status"] == "incomplete_step_limit"
    trace = result["trace"]
    assert trace["tool_calls_used"] == 6
    executed = [s for s in trace["steps"] if s.get("action_type") == "call_tool"]
    assert len(executed) == 6
    limit_step = trace["steps"][-1]
    assert limit_step["action_type"] == "step_limit"
    assert "上限" in limit_step["observation_summary"]
    assert "上限" in trace["final_reason"]


def test_step_limit_default_is_configurable():
    result = run_case("case1_normal_30s_compress", max_tool_calls=3)
    assert result["status"] == "incomplete_step_limit"
    assert result["trace"]["tool_calls_used"] == 3


# ---------------------------------------------------------------- 模型接入（离线部分）

def test_real_model_requires_env(monkeypatch):
    for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ModelError):
        build_model("real")


def test_real_model_base_url_normalization(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1/")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    m = RealModel()
    assert m.base_url == "https://api.example.com/v1/chat/completions"


def test_parse_valid_action():
    obj = RealModel._parse(json.dumps(
        {"action": "call_tool", "tool": "search_materials",
         "args": {"query": "映记"}, "reason": "先检索素材"}))
    assert obj["action"] == "call_tool" and obj["tool"] == "search_materials"


def test_parse_fenced_action():
    text = "```json\n" + json.dumps({"action": "finish", "status": "completed",
                                     "output": None, "reason": "done"}) + "\n```"
    obj = RealModel._parse(text)
    assert obj["action"] == "finish" and obj["status"] == "completed"


def test_parse_invalid_action_raises():
    with pytest.raises(ActionFormatError):
        RealModel._parse("我觉得应该先检索一下素材")
    with pytest.raises(ActionFormatError):
        RealModel._parse(json.dumps({"action": "fly_to_moon"}))
    with pytest.raises(ActionFormatError):
        RealModel._parse(json.dumps({"action": "call_tool", "args": {}}))


def test_rewrite_query():
    assert rewrite_query("映记 剪片") == "映记 剪辑"
    assert rewrite_query("量子计算机维护 vlog") == "量子计算机维护 视频日志"


# ---------------------------------------------------------------- finish.output 结构规整（审查 P1 修复）

def test_normalize_colloquial_status_and_string_risks():
    """真实模型首跑缺陷回归：口语 status + 整句字符串 risks 必须被规整为协议结构。"""
    out, status, fixes = normalize_finish_output(
        {"status": "已通过全量规则校验", "risks": "无违规风险，所有产品事实均可溯源", "scenes": []},
        "completed")
    assert status == "completed"
    assert out["status"] == "completed"
    assert out["risks"] == ["无违规风险，所有产品事实均可溯源"]
    assert any("risks" in f for f in fixes) and any("output.status" in f for f in fixes)
    assert any("notes" in f for f in fixes)  # notes 缺失 → 默认事实来源说明


def test_normalize_valid_risks_list_untouched():
    out, _, fixes = normalize_finish_output({"risks": ["风险一", "风险二"], "notes": "x"}, "completed")
    assert out["risks"] == ["风险一", "风险二"]
    assert not any("risks" in f for f in fixes) and not any("notes" in f for f in fixes)


def test_normalize_non_enum_status_mapped():
    out, status, fixes = normalize_finish_output({"scenes": []}, "搞定了")
    assert status == "completed" and any("映射" in f for f in fixes)


def test_normalize_non_enum_status_without_output():
    out, status, _ = normalize_finish_output(None, "缺东西")
    assert out is None and status == "incomplete_unsatisfiable"


def test_parse_finish_attaches_normalization_record():
    obj = RealModel._parse(json.dumps({"action": "finish", "status": "completed",
                                       "output": {"risks": "整句风险", "scenes": []}, "reason": "r"}))
    assert obj["output"]["risks"] == ["整句风险"]
    assert obj["_normalization"]  # 修复清单随动作返回，由 agent 写入 trace


def test_parse_finish_clean_output_no_fixes():
    obj = RealModel._parse(json.dumps({"action": "finish", "status": "completed",
                                       "output": {"risks": ["a"], "notes": "n"}, "reason": "r"}))
    assert obj["_normalization"] == []


def test_real_model_name_carries_model_id(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "doubao-seed-2.1-lite")
    assert RealModel().name == "real:doubao-seed-2.1-lite"
