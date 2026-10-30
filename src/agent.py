# -*- coding: utf-8 -*-
"""ReAct 主循环：模型选择动作 -> 执行工具 -> 观察结果 -> 模型调整下一步或结束。

骨架职责（刻意保持极简，决策全部在模型侧）：
  1. 维护请求与观察历史；
  2. 执行模型选择的工具并把观察结果追加回历史；
  3. 强制执行「每轮工具调用次数上限」，达到上限返回明确的未完成状态，
     绝不静默截断；
  4. 记录逐步 trace：步骤、动作与决策依据、工具参数、返回摘要与完整观察。

循环本身不含任何业务分支（如「检索为空怎么办」）——那类判断全部由模型
依据观察结果做出，满足动态决策要求。
"""
import datetime as _dt
import json

from .model import (ACTION_CALL_TOOL, ACTION_FINISH, ActionFormatError, STATUS_MODEL_ERROR,
                    STATUS_STEP_LIMIT, ModelError)

# 动作格式错误的容忍次数（仅真实模型可能触发；超过后明确失败）
_MAX_ACTION_FORMAT_ERRORS = 3


def _summarize(obs: dict) -> str:
    """生成观察结果摘要（trace 用，一句话）。"""
    if not obs.get("ok"):
        return f"工具 {obs.get('tool')} 失败[{obs.get('error_type')}]：{str(obs.get('error'))[:120]}"
    tool, r = obs["tool"], obs.get("result", {})
    if tool == "search_materials":
        if r.get("hit_count"):
            ids = "、".join(h["id"] for h in r["hits"])
            return f"检索命中 {r['hit_count']} 条素材：{ids}"
        return "检索无结果（0 条命中）"
    if tool == "read_material":
        return (f"读取 {r.get('id')}《{r.get('title')}》："
                f"事实 {len(r.get('facts', []))} 条，效果数据 {len(r.get('effect_data', []))} 条")
    if tool == "estimate_duration":
        return f"预计总时长 {r.get('total_sec')}s（{len(r.get('scenes', []))} 个分镜）"
    if tool == "check_rules":
        if r.get("pass"):
            return f"规则检查通过（{len(r.get('checked_rules', []))} 项规则）"
        viols = "；".join(v["rule_id"] for v in r.get("violations", []))
        return f"规则检查不通过 {r.get('violation_count')} 项：{viols}"
    return json.dumps(r, ensure_ascii=False)[:120]


class ReActAgent:
    def __init__(self, registry, model, max_tool_calls: int = 12):
        self.registry = registry
        self.model = model
        self.max_tool_calls = max_tool_calls

    def run(self, request: dict) -> dict:
        """执行一次完整任务，返回 {status, output, trace}。"""
        started = _dt.datetime.now().isoformat(timespec="seconds")
        ctx = {
            "request": request,
            "history": [],          # 观察结果（含 args 回显），模型每轮可见
            "tool_calls_used": 0,
            "max_tool_calls": self.max_tool_calls,
            "tool_schemas": self.registry.schemas(),
        }
        steps: list[dict] = []
        format_errors = 0

        while True:
            # ---- 模型决策 ----
            try:
                action = self.model.decide(ctx)
            except ActionFormatError as e:
                format_errors += 1
                obs = {"ok": False, "tool": None, "args": None,
                       "error_type": "ActionFormatError", "error": str(e)}
                ctx["history"].append(obs)
                steps.append(self._step_record(len(steps) + 1, {"action": "format_error"}, obs))
                if format_errors >= _MAX_ACTION_FORMAT_ERRORS:
                    return self._result(STATUS_MODEL_ERROR,
                                        f"模型连续 {_MAX_ACTION_FORMAT_ERRORS} 次返回不合法动作，任务终止",
                                        request, started, steps, ctx, output=None)
                continue
            except ModelError as e:
                return self._result(STATUS_MODEL_ERROR, f"模型接入失败：{e}",
                                    request, started, steps, ctx, output=None)

            # ---- 结束动作 ----
            if action.get("action") == ACTION_FINISH:
                status = action.get("status", "completed")
                steps.append({
                    "step": len(steps) + 1,
                    "action_type": "finish",
                    "decision_basis": action.get("reason", ""),
                    "final_status": status,
                    "questions": action.get("questions", []),
                    # 真实模型 finish.output 的结构规整记录（空列表=无需规整）
                    "output_normalization": action.get("_normalization", []),
                })
                return self._result(status, action.get("reason", ""), request, started,
                                    steps, ctx, output=action.get("output"))

            # ---- 工具调用：先检查步数上限，达到上限必须显式返回未完成 ----
            if ctx["tool_calls_used"] >= self.max_tool_calls:
                steps.append({
                    "step": len(steps) + 1,
                    "action_type": "step_limit",
                    "attempted_action": {"tool": action.get("tool"), "args": action.get("args"),
                                         "decision_basis": action.get("reason", "")},
                    "observation_summary": (f"已达每轮工具调用上限 {self.max_tool_calls}，"
                                            "拒绝继续执行工具，任务以未完成状态结束"),
                })
                return self._result(STATUS_STEP_LIMIT,
                                    f"已达到每轮工具调用上限（{self.max_tool_calls} 次工具调用），"
                                    "任务未完成。已完成的步骤见 trace，未静默截断。",
                                    request, started, steps, ctx, output=None)

            # ---- 执行工具并观察 ----
            obs = self.registry.execute(action.get("tool", ""), action.get("args"))
            ctx["tool_calls_used"] += 1
            ctx["history"].append(obs)
            steps.append(self._step_record(len(steps) + 1, action, obs))

    # ---- 组装结果与 trace ----

    @staticmethod
    def _step_record(step_no: int, action: dict, obs: dict) -> dict:
        return {
            "step": step_no,
            "action_type": "call_tool",
            "tool": obs.get("tool"),
            "tool_args": obs.get("args"),
            "decision_basis": action.get("reason", ""),
            "observation_summary": _summarize(obs),
            "observation": obs,
        }

    def _result(self, status, reason, request, started, steps, ctx, output) -> dict:
        trace = {
            "model": getattr(self.model, "name", str(type(self.model))),
            "request": request,
            "started_at": started,
            "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "max_tool_calls": self.max_tool_calls,
            "tool_calls_used": ctx["tool_calls_used"],
            "final_status": status,
            "final_reason": reason,
            "steps": steps,
        }
        return {"status": status, "output": output, "trace": trace}
