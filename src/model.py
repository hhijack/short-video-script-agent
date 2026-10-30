# -*- coding: utf-8 -*-
"""模型接入层：真实模型（OpenAI 兼容）与离线 Mock 模型双入口。

两者实现同一协议：decide(context) -> 动作 dict
  {"action": "call_tool", "tool": str, "args": dict, "reason": str}
  {"action": "finish", "status": str, "output": dict|None, "reason": str, "questions": [str]}

两个模型都是「无状态」的：下一步动作只由 request 与历史观察结果决定，
不依赖隐藏状态——这正是 ReAct 循环的观察驱动特性。

Mock 模型为「脚本化决策序列」：决策逻辑是预先编写的策略函数，但每一步
走向由上一步工具观察结果决定（检索命中与否、估算是否超时、检查是否通过、
失败类型是基础设施故障还是数据缺失）。Mock 只验证流程与工具链路，
不证明真实模型的决策效果（边界见 README / docs/03）。

真实模型按 OpenAI 兼容 Chat Completions 接口接入，环境变量
LLM_API_KEY / LLM_BASE_URL / LLM_MODEL 配置；要求模型每轮返回严格 JSON 动作。
"""
import json
import re
import urllib.error
import urllib.request

from .config import Settings
from .data_store import MaterialStore, _normalize

# 动作协议
ACTION_CALL_TOOL = "call_tool"
ACTION_FINISH = "finish"

STATUS_COMPLETED = "completed"
STATUS_MISSING_INFO = "incomplete_missing_info"
STATUS_NO_MATERIAL = "incomplete_no_material"
STATUS_TOOL_FAILURE = "incomplete_tool_failure"
STATUS_STEP_LIMIT = "incomplete_step_limit"
STATUS_UNSATISFIABLE = "incomplete_unsatisfiable"
STATUS_MODEL_ERROR = "incomplete_model_error"


def call_tool(tool: str, args: dict, reason: str) -> dict:
    return {"action": ACTION_CALL_TOOL, "tool": tool, "args": args, "reason": reason}


def finish(status: str, reason: str, output: dict | None = None, questions=None) -> dict:
    return {"action": ACTION_FINISH, "status": status, "output": output,
            "reason": reason, "questions": questions or []}


class ModelError(Exception):
    """模型接入失败（网络、鉴权、上游错误等）。"""


class ActionFormatError(Exception):
    """模型返回的动作不符合 JSON 协议。"""


# finish.output 的结构规整 ------------------------------------------------

_OUTPUT_STATUS_ENUM = {STATUS_COMPLETED, STATUS_MISSING_INFO, STATUS_NO_MATERIAL,
                       STATUS_TOOL_FAILURE, STATUS_STEP_LIMIT, STATUS_UNSATISFIABLE,
                       STATUS_MODEL_ERROR}

_DEFAULT_NOTE = ("本脚本由 Agent 生成：产品事实与效果数据以各分镜 material_ids 引用的素材原文为准，"
                 "素材库未包含的效果结论一律未写入。")


def normalize_finish_output(output, status: str) -> tuple[dict | None, str, list[str]]:
    """把真实模型返回的 finish.output 规整为协议结构，返回 (output, status, 修复清单)。

    真实模型首跑曾出现：risks 输出为整句字符串（协议要求数组）、status 为
    口语表述而非枚举。本函数做轻量确定性规整，并把每一处修改如实记录，
    由 agent 写入 trace 的 finish 步骤供审计（背景见 docs/03-evaluation.md 3.1）。
    """
    fixes: list[str] = []
    # ① 动作级 status 不在枚举 → 按 output 是否为交付对象映射
    if status not in _OUTPUT_STATUS_ENUM:
        new_status = STATUS_COMPLETED if isinstance(output, dict) else STATUS_UNSATISFIABLE
        fixes.append(f"status「{str(status)[:30]}」不在协议枚举，映射为 {new_status}")
        status = new_status
    if not isinstance(output, dict):
        return output, status, fixes

    output = dict(output)
    # ② output.status 必须与动作级状态一致（协议枚举）；缺失则从动作级继承（不算纠错）
    if "status" not in output:
        output["status"] = status
    elif output.get("status") != status:
        fixes.append(f"output.status「{str(output.get('status'))[:30]}」规整为 {status}")
        output["status"] = status
    # ③ risks 必须是字符串数组（首跑曾输出整句字符串）
    risks = output.get("risks")
    if isinstance(risks, str):
        risks = [risks] if risks.strip() else []
        fixes.append("risks 由字符串包装为数组")
    elif isinstance(risks, list):
        risks = [str(r) for r in risks if str(r).strip()]
    elif "risks" in output:
        fixes.append(f"risks 类型 {type(output.get('risks')).__name__} 非法，置空数组")
    output["risks"] = risks
    # ④ notes 缺失或为空 → 默认事实来源说明
    if not (isinstance(output.get("notes"), str) and output["notes"].strip()):
        output["notes"] = _DEFAULT_NOTE
        fixes.append("notes 缺失，填入默认事实来源说明")
    return output, status, fixes


# ---------------------------------------------------------------- 离线 Mock 模型

# 查询改写用的同义词表（脚本化改写策略：逐 token 同义替换/泛化）
MOCK_QUERY_SYNONYMS = {
    "剪片": "剪辑",
    "出片": "成片",
    "vlog": "视频日志",
    "新手": "入门",
    "教程": "入门指南",
    "维护": "保养",
}


def rewrite_query(query: str) -> str:
    return " ".join(MOCK_QUERY_SYNONYMS.get(t, t) for t in query.split())


class MockModel:
    """离线 Mock 模型：脚本化决策序列（无需任何环境变量与网络）。

    决策依据完全来自 ctx（请求 + 历史观察），策略要点：
      ① 关键信息缺失（主题/目标时长）→ 立即请求补充，不猜默认值；
      ② 检索无结果 → 改写查询词再检索一次 → 仍无结果则请求补充素材；
      ③ 命中素材 → 逐条读取原文，作为唯一事实来源；
      ④ 估算失败（基础设施故障）→ 重试一次 → 仍失败则明确报告未完成；
      ⑤ 读取失败：素材不存在（ToolError）→ 跳过继续；基础设施故障 → 上报失败；
      ⑥ 估算通过 → 规则检查；检查不通过 → 压缩分镜并移除无依据表述后重新估算再检查；
      ⑦ 压缩稿仍不通过 → 明确返回无法满足。
    """

    name = "mock"

    def __init__(self):
        # Mock 模型解析素材引用时直接复用素材库（与 read_material 工具同源）
        self._store = MaterialStore()

    # ---- 脚本草稿（文字内容固定；引用与时长均来自工具观察结果） ----

    DRAFT_V1 = {
        "title": "新手快速出片：字幕、模板、免版权音乐一次搞定",
        "scenes": [
            {"id": "S1", "visual": "手机竖屏：人物瘫在沙发上无限下滑短视频信息流",
             "narration": "刷了三个小时短视频，还是剪不出一条能看的片子？",
             "subtitle": "刷了三小时，还是剪不出片子？", "claims": []},
            {"id": "S2", "visual": "快切三屏：相册里乱糟糟的素材、翻找配乐的界面、手动敲字幕的界面",
             "narration": "素材散在好几个相册里、配乐也难找、字幕要一个字一个字敲，做完天都亮了。",
             "subtitle": "素材散、配乐难、字幕难敲", "claims": []},
            {"id": "S3", "visual": "App 录屏：选中素材后自动生成字幕，模板库滑动画廊，音乐库列表",
             "narration": "映记支持自动生成字幕，还有两百多个一键成片模板可以套，音乐全部免版权。",
             "subtitle": "自动生成字幕＋一键成片模板", "claims": ["自动生成字幕", "一键成片模板", "免版权"]},
            {"id": "S4", "visual": "分屏对比：左侧深夜剪片的人，右侧快速成片并点击发布",
             "narration": "同事磨一下午的片子，你 3 分钟就能发新片，效率翻倍。",
             "subtitle": "3 分钟发新片", "claims": ["3分钟"]},
            {"id": "S5", "visual": "App 录屏：挑选模板到导出高清成片的操作流，结尾定格品牌 Logo",
             "narration": "现在就打开映记，挑个喜欢的模板，导出 1080P 高清成片，直接发抖音。",
             "subtitle": "1080P 高清成片，直接发抖音", "claims": ["模板", "1080P"]},
        ],
    }

    DRAFT_V2 = {
        "title": "新手快速出片：字幕、模板、免版权音乐一次搞定",
        "scenes": [
            {"id": "S1", "visual": "手机竖屏：人物瘫在沙发上无限下滑短视频信息流",
             "narration": "刷了三个小时的短视频，还是剪不出一条能发的片子？",
             "subtitle": "三小时刷屏，剪不出片子？", "claims": []},
            {"id": "S2", "visual": "App 录屏：选中素材后自动生成字幕，模板库滑动画廊，音乐库列表",
             "narration": "映记支持自动生成字幕，两百多个一键成片模板直接套用，音乐全部免版权随便挑。",
             "subtitle": "自动生成字幕＋一键成片模板", "claims": ["自动生成字幕", "一键成片模板", "免版权"]},
            {"id": "S3", "visual": "分屏对比：左侧深夜剪片的人，右侧快速成片并点击发布",
             "narration": "别人磨一下午的片子，你 3 分钟就能发新片。",
             "subtitle": "3 分钟发新片", "claims": ["3分钟"]},
            {"id": "S4", "visual": "App 录屏：挑选模板到导出高清成片的操作流，结尾定格品牌 Logo",
             "narration": "现在就打开映记，套个模板，导出 1080P 高清成片，直接发抖音。",
             "subtitle": "导出 1080P，直接发抖音", "claims": ["模板", "1080P"]},
        ],
    }

    RISKS = [
        "P05 的成片时间 3 分钟为内部实测数据（2026-09），非第三方统计，口播已按素材原文表述",
        "素材库暂无用户评价、销量类素材，脚本未包含相关内容",
    ]

    # ---- 决策入口 ----

    def decide(self, ctx: dict) -> dict:
        req = ctx["request"]
        hist = ctx["history"]

        # ① 关键信息检查：主题与目标时长是硬前提，缺失即请求补充，不猜默认值
        if not str(req.get("topic") or "").strip():
            return finish(STATUS_MISSING_INFO, "缺少关键信息：脚本主题未提供，无法检索素材与起稿",
                          questions=["请提供脚本主题（例如产品功能介绍、使用教程）"])
        target = req.get("target_duration_sec")
        if not isinstance(target, (int, float)) or target <= 0:
            return finish(STATUS_MISSING_INFO, "缺少关键信息：目标时长未提供，无法进行时长估算与约束检查",
                          questions=["请提供目标时长（秒），例如 30"])

        searches = [h for h in hist if h.get("tool") == "search_materials"]

        # ② 起稿前先检索素材库
        if not searches:
            return call_tool("search_materials", {"query": req["topic"], "top_k": 5},
                             reason="起稿前先检索素材库，确定有哪些可用产品事实与效果数据")

        last_search = searches[-1]
        if not last_search.get("ok"):
            # 检索工具基础设施失败（区别于「无结果」）：直接上报失败
            return finish(STATUS_TOOL_FAILURE, f"素材检索工具调用失败：{last_search.get('error')}",
                          questions=["可稍后重试，或直接提供素材清单"])

        hits = last_search["result"]["hits"]

        # ③ 检索无结果：改写查询词再检索一次；仍无结果则请求用户补充素材
        if not hits:
            if len(searches) == 1:
                new_query = rewrite_query(req["topic"])
                return call_tool("search_materials", {"query": new_query, "top_k": 5},
                                 reason=f"首次检索无结果，改写查询词为「{new_query}」再检索一次")
            return finish(STATUS_NO_MATERIAL, "改写查询后仍无任何素材命中，素材库无法支撑该主题",
                          questions=["请提供该主题的产品素材，或更换主题"])

        # ④ 逐条读取命中素材原文；读取失败按类型区分处理
        #    read_order 保持检索命中顺序（有序去重），保证引用顺序与产物可复现
        read_order: list = []
        for h in hist:
            if h.get("tool") != "read_material":
                continue
            mid = h["result"]["id"] if h.get("ok") else (h.get("args") or {}).get("material_id")
            if mid and mid not in read_order:
                read_order.append(mid)
        pending = [h["id"] for h in hits if h["id"] not in read_order]
        if pending:
            return call_tool("read_material", {"material_id": pending[0]},
                             reason=f"读取素材 {pending[0]} 原文，收集可用于脚本的产品事实")

        # ⑤ 起稿 / 改稿后交时长估算；估算失败（基础设施故障）重试一次
        #    历史上已有规则检查记录 → 当前稿是压缩修正稿；否则是首稿
        checks = [h for h in hist if h.get("tool") == "check_rules"]
        next_draft = self.DRAFT_V2 if checks else self.DRAFT_V1
        if not any(h.get("tool") == "estimate_duration" for h in hist):
            return call_tool("estimate_duration", self._est_args(self.DRAFT_V1),
                             reason="已读齐素材原文，基于其中的产品事实起草首稿（5 分镜），先交时长估算")

        last = hist[-1]
        if last.get("tool") == "estimate_duration":
            if not last.get("ok"):
                trailing_fails = self._trailing_failed_estimates(hist)
                if last.get("error_type") == "ToolFailure" and trailing_fails < 2:
                    return call_tool("estimate_duration", self._est_args(next_draft),
                                     reason="时长估算工具失败（疑似瞬时故障），重试一次")
                return finish(STATUS_TOOL_FAILURE,
                              f"时长估算工具{'重试后仍' if trailing_fails > 1 else ''}失败：{last.get('error')}，"
                              "无法给出预计总时长，任务未完成")
            return call_tool("check_rules", {"script": self._script_for_check(next_draft, hist, target)},
                             reason=f"估算总时长 {last['result']['total_sec']}s，交规则检查（时长区间、反幻觉等）")

        # ⑥ 依据规则检查结果决定收尾或修正
        #    刚被检查的稿：第 1 次检查对应首稿，第 2 次起对应压缩修正稿
        if last.get("tool") == "check_rules":
            result = last.get("result", {})
            checked_draft = self.DRAFT_V1 if len(checks) == 1 else self.DRAFT_V2
            if result.get("pass"):
                return finish(STATUS_COMPLETED, "时长与规则检查全部通过，输出最终脚本",
                              output=self._final_output(target, checked_draft, hist, passed=True))
            if checked_draft is self.DRAFT_V1:
                viol = "；".join(f"{v['rule_id']}({v['message']})" for v in result.get("violations", []))
                return call_tool("estimate_duration", self._est_args(self.DRAFT_V2),
                                 reason=f"首稿检查未通过：{viol}。压缩分镜并移除无素材依据的表述后重新估算")
            return finish(STATUS_UNSATISFIABLE, "压缩修正稿仍未通过规则检查，无法满足目标要求",
                          output=self._final_output(target, checked_draft, hist, passed=False))

        return finish(STATUS_TOOL_FAILURE, f"Mock 模型无法根据观察结果决定下一步：{json.dumps(last, ensure_ascii=False)[:200]}")

    # ---- 辅助：全部从历史观察推导，无隐藏状态 ----

    @staticmethod
    def _trailing_failed_estimates(hist: list) -> int:
        n = 0
        for h in reversed(hist):
            if h.get("tool") == "estimate_duration" and not h.get("ok"):
                n += 1
            else:
                break
        return n

    @staticmethod
    def _find_last_ok_estimate(hist: list) -> dict:
        for h in reversed(hist):
            if h.get("tool") == "estimate_duration" and h.get("ok"):
                return h["result"]
        return {}

    @staticmethod
    def _est_args(draft: dict) -> dict:
        return {"scenes": [{"id": s["id"], "narration": s["narration"]} for s in draft["scenes"]]}

    def _resolve_citations(self, draft: dict, hist: list) -> dict:
        """根据已读取素材的原文，为每个分镜解析可追溯的素材引用。

        分镜声明的事实/效果表述（claims）必须能在某条已读素材原文中找到，
        找不到的表述不产生引用（后续会被规则检查 R3 拦截）。
        """
        # 有序去重（检索命中顺序），保证 material_ids 顺序可复现
        read_order: list = []
        for h in hist:
            if h.get("tool") != "read_material":
                continue
            mid = h["result"]["id"] if h.get("ok") else (h.get("args") or {}).get("material_id")
            if mid and mid not in read_order:
                read_order.append(mid)
        texts = {}
        for mid in read_order:
            try:
                m = self._store.get(mid)
            except KeyError:
                continue
            texts[mid] = _normalize(" ".join(m.get("facts", []) + m.get("effect_data", [])))
        citations = {}
        for s in draft["scenes"]:
            cited = []
            for claim in s.get("claims", []):
                key = _normalize(claim)
                for mid, text in texts.items():
                    if key in text and mid not in cited:
                        cited.append(mid)
            citations[s["id"]] = cited
        return citations

    def _script_for_check(self, draft: dict, hist: list, target) -> dict:
        est = self._find_last_ok_estimate(hist)
        durations = {s["index"]: s["duration_sec"] for s in est.get("scenes", [])}
        citations = self._resolve_citations(draft, hist)
        scenes = [{
            "id": s["id"], "narration": s["narration"], "subtitle": s["subtitle"],
            "duration_sec": durations.get(i), "material_ids": citations.get(s["id"], []),
        } for i, s in enumerate(draft["scenes"], start=1)]
        return {"title": draft["title"], "scenes": scenes,
                "target_duration_sec": target, "estimated_total_sec": est.get("total_sec")}

    def _final_output(self, target, draft: dict, hist: list, passed: bool = True) -> dict:
        est = self._find_last_ok_estimate(hist)
        durations = {s["index"]: s["duration_sec"] for s in est.get("scenes", [])}
        citations = self._resolve_citations(draft, hist)
        scenes, all_ids = [], []
        for i, s in enumerate(draft["scenes"], start=1):
            mids = citations.get(s["id"], [])
            for mid in mids:
                if mid not in all_ids:
                    all_ids.append(mid)
            scenes.append({"id": s["id"], "visual": s["visual"], "narration": s["narration"],
                           "subtitle": s["subtitle"], "duration_sec": durations.get(i),
                           "material_ids": mids})
        return {
            "title": draft["title"],
            "status": STATUS_COMPLETED if passed else STATUS_UNSATISFIABLE,
            "target_duration_sec": target,
            "estimated_total_sec": est.get("total_sec"),
            "scenes": scenes,
            "material_ids": all_ids,
            "risks": list(self.RISKS),
            "notes": "脚本中的产品事实与效果数据均来自所引用素材原文（见各分镜 material_ids）；"
                     "素材库未包含的效果结论（如「效率提升 X%」）一律未写入。",
        }


# ---------------------------------------------------------------- 真实模型（OpenAI 兼容）

class RealModel:
    """真实模型入口：OpenAI 兼容 Chat Completions。

    配置（环境变量）：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
    每轮把完整观察历史交给模型，要求其返回严格 JSON 动作（见 SYSTEM_PROMPT）。
    """

    name = "real"

    SYSTEM_PROMPT = """你是短视频脚本 Agent 的决策模型，工作在 ReAct 循环中。
可用工具（JSON Schema）：
{tools}

每轮你必须只输出一个 JSON 对象（不要输出任何其他文字），格式二选一：
1) 调用工具：{"action": "call_tool", "tool": "<工具名>", "args": {...}, "reason": "<一句话决策依据>"}
2) 结束：{"action": "finish", "status": "completed|incomplete_missing_info|incomplete_no_material|incomplete_tool_failure|incomplete_unsatisfiable", "output": <最终交付或null>, "reason": "<一句话决策依据>", "questions": ["需要用户补充的信息，可为空数组"]}

硬性要求：
- 若 request 缺少主题（topic）或目标时长（target_duration_sec），这是关键信息：不得自行假定默认值，必须立即以 {"action": "finish", "status": "incomplete_missing_info"} 结束，并在 questions 中列出需要用户补充的信息。
- 脚本中每个产品事实、每个数字/效果表述，必须能在 read_material 返回的素材原文中找到，并在分镜 material_ids 中写明素材 ID；素材中没有的效果数据（如"效率提升80%"）一律不得编造。
- 交付结构：title、scenes[{id, visual, narration, subtitle, duration_sec, material_ids}]、estimated_total_sec、material_ids、risks、status。
- 时长超限或规则检查不通过时，先修改脚本，再重新 estimate_duration / check_rules；确实无法满足时以 incomplete_* 状态结束并说明原因。
- 注意上下文中的工具调用次数上限，必要时尽快收尾。"""

    def __init__(self, settings: Settings | None = None, timeout: int | None = None):
        s = settings or Settings.load()
        if not (s.llm_api_key and s.llm_base_url and s.llm_model):
            raise ModelError(
                "真实模型未配置：请设置环境变量 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL"
                "（OpenAI 兼容接口），或改用 --model mock 离线入口")
        self.api_key = s.llm_api_key
        self.base_url = s.llm_base_url.rstrip("/")
        if not self.base_url.endswith("/chat/completions"):
            self.base_url += "/chat/completions"
        self.model = s.llm_model
        self.timeout = timeout or s.llm_timeout
        self._max_retries = 2  # 瞬时网络错误（超时等）的总尝试次数
        # trace 里可自证实际使用的模型（P4 审查项）
        self.name = f"real:{s.llm_model}"

    def decide(self, ctx: dict) -> dict:
        tools = ctx.get("tool_schemas") or []
        system = self.SYSTEM_PROMPT.replace("{tools}", json.dumps(tools, ensure_ascii=False, indent=1))
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(
                    {"request": ctx["request"], "history": ctx["history"],
                     "tool_calls_used": ctx["tool_calls_used"],
                     "max_tool_calls": ctx["max_tool_calls"]},
                    ensure_ascii=False)},
            ],
        }
        content = self._post(payload)
        return self._parse(content)

    def _post(self, payload: dict) -> str:
        import time
        last_err: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            req = urllib.request.Request(
                self.base_url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                try:
                    return body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    raise ModelError(f"模型接口返回结构异常: {json.dumps(body, ensure_ascii=False)[:300]}")
            except urllib.error.HTTPError as e:
                raise ModelError(f"模型接口 HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
                if attempt < self._max_retries:
                    time.sleep(2 * attempt)
        raise ModelError(f"模型接口请求失败（重试 {self._max_retries} 次后）: {last_err}")

    @staticmethod
    def _parse(content: str) -> dict:
        text = content.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
        if fence:
            text = fence.group(1)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise ActionFormatError(f"动作 JSON 解析失败: {e}；原文片段: {content[:200]}")
        if not isinstance(obj, dict) or obj.get("action") not in (ACTION_CALL_TOOL, ACTION_FINISH):
            raise ActionFormatError(f"动作缺少合法 action 字段: {content[:200]}")
        if obj["action"] == ACTION_CALL_TOOL:
            if not isinstance(obj.get("tool"), str) or not obj["tool"]:
                raise ActionFormatError("call_tool 缺少 tool 字段")
            if "args" in obj and not isinstance(obj["args"], dict):
                raise ActionFormatError("args 必须是对象")
        obj.setdefault("reason", "")
        obj.setdefault("questions", [])
        if obj["action"] == ACTION_FINISH:
            obj["output"], obj["status"], fixes = normalize_finish_output(obj.get("output"), obj.get("status", ""))
            obj["_normalization"] = fixes
        return obj


def build_model(kind: str, settings: Settings | None = None):
    """模型工厂：kind 为 "mock" 或 "real"。"""
    if kind == "mock":
        return MockModel()
    if kind == "real":
        return RealModel(settings)
    raise ValueError(f"未知模型类型: {kind}（可选 mock / real）")
