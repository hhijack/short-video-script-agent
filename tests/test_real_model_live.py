# -*- coding: utf-8 -*-
"""真实模型 live 测试（可选）。

默认跳过：仅当环境已配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 时运行。
运行方式：
    set -a; source .env; set +a
    python3 -m pytest tests/test_real_model_live.py -v

只做协议与结构断言（真实模型输出非确定性，不做逐字节断言）；
行为级断言见 examples_real/ 的实际产物。
"""
import json
import os

import pytest

from src.cli import CASES_DIR, run_request

_NEED = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
_skip = pytest.mark.skipif(
    not all(os.environ.get(v) for v in _NEED),
    reason=f"需要配置环境变量 {' / '.join(_NEED)} 才能运行真实模型 live 测试")

ALLOWED_STATUS = {
    "completed", "incomplete_missing_info", "incomplete_no_material",
    "incomplete_tool_failure", "incomplete_step_limit",
    "incomplete_unsatisfiable", "incomplete_model_error",
}


@_skip
def test_live_real_model_case1_protocol():
    """真实模型端到端跑 case1：断言协议与 trace 结构（不锁定具体决策路径）。"""
    meta = json.loads((CASES_DIR / "case1_normal_30s_compress.json").read_text(encoding="utf-8"))
    result = run_request(meta["request"], "real", 12, {})
    assert result["status"] in ALLOWED_STATUS
    trace = result["trace"]
    assert trace["tool_calls_used"] <= 12
    tool_steps = [s for s in trace["steps"] if s.get("action_type") == "call_tool"]
    assert tool_steps, "真实模型至少应调用一次工具"
    for s in trace["steps"]:
        assert "decision_basis" in s
        if s.get("action_type") == "call_tool":
            assert s["tool"], "工具调用步骤应记录工具名"
            assert s.get("tool_args") is not None, "观察结果应含 args 回显"
    if result["status"] == "completed":
        out = result["output"]
        assert out["scenes"] and out["estimated_total_sec"] <= meta["request"]["target_duration_sec"]
        assert out["material_ids"], "完成态输出必须引用素材 ID"


@_skip
def test_live_real_model_missing_duration_requests_info():
    """真实模型缺关键信息场景：期望请求补充而非自行假定（system prompt 硬性要求）。"""
    meta = json.loads((CASES_DIR / "case2_missing_duration.json").read_text(encoding="utf-8"))
    result = run_request(meta["request"], "real", 12, {})
    # 真实模型可能仍有非确定性；若未按约束请求补充，此处会失败提示复核 prompt
    assert result["status"] == "incomplete_missing_info"
    assert result["trace"]["steps"][-1]["questions"]
