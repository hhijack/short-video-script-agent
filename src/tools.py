# -*- coding: utf-8 -*-
"""业务工具层：4 个本地 Mock 工具 + 工具注册表。

工具清单（四大业务工具）：
  search_materials  素材检索：关键词打分检索合成素材库
  read_material     素材读取：按 ID 读取素材全文（事实 / 效果数据）
  estimate_duration 时长估算：按确定性口径估算分镜与总时长
  check_rules       规则检查：品牌规则逐项检查，R3 为反幻觉检查项（事实必须可追溯到素材 ID）

故障注入：registry.set_failures({"estimate_duration": 2}) 表示该工具前 2 次
调用模拟基础设施失败（超时/服务不可用），用于「工具失败」场景的可复现测试。
"""
import re
from dataclasses import dataclass, field

from .data_store import MaterialNotFound, MaterialStore, _normalize, load_brand_rules
from .duration import estimate_scenes

_NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|倍|分钟|秒钟|秒|小时|天)")


class ToolError(Exception):
    """工具参数/执行错误（模型传参非法、素材不存在等）。"""


class ToolFailure(Exception):
    """模拟的基础设施级失败（由故障注入产生）。"""


# ---------------------------------------------------------------- 工具实现

def tool_search_materials(store: MaterialStore, query: str, top_k: int = 3) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("query 不能为空")
    top_k = int(top_k) if top_k else 3
    if not 1 <= top_k <= 10:
        raise ToolError("top_k 取值范围 1-10")
    hits = store.search(query.strip(), top_k=top_k)
    return {"query": query.strip(), "hit_count": len(hits), "hits": hits}


def tool_read_material(store: MaterialStore, material_id: str) -> dict:
    if not isinstance(material_id, str) or not material_id.strip():
        raise ToolError("material_id 不能为空")
    try:
        m = store.get(material_id.strip())
    except MaterialNotFound:
        raise ToolError(f"素材 {material_id} 不存在于素材库（现有: {', '.join(store.all_ids())}）")
    return {"id": m["id"], "title": m["title"], "tags": m.get("tags", []),
            "facts": m.get("facts", []), "effect_data": m.get("effect_data", [])}


def tool_estimate_duration(store: MaterialStore, scenes: list) -> dict:
    if not isinstance(scenes, list) or not scenes:
        raise ToolError("scenes 必须为非空数组，每项形如 {\"id\": \"S1\", \"narration\": \"...\"}")
    narrations = []
    for i, s in enumerate(scenes, 1):
        if not isinstance(s, dict) or not isinstance(s.get("narration"), str) or not s["narration"].strip():
            raise ToolError(f"scenes[{i}] 缺少非空 narration 字段")
        narrations.append(s["narration"])
    from .config import Settings  # 局部导入避免环
    cfg = Settings.load()
    return estimate_scenes(narrations, cfg.narration_speed, cfg.min_scene_sec)


def tool_check_rules(store: MaterialStore, script: dict) -> dict:
    """品牌规则检查。任何一条规则不通过都会给出结构化违规项。"""
    if not isinstance(script, dict):
        raise ToolError("script 必须为对象")
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ToolError("script.scenes 必须为非空数组")
    target = script.get("target_duration_sec")
    if not isinstance(target, (int, float)) or target <= 0:
        raise ToolError("script.target_duration_sec 必须为正数")
    total = script.get("estimated_total_sec")
    if not isinstance(total, (int, float)) or total <= 0:
        raise ToolError("script.estimated_total_sec 必须为正数（来自时长估算工具）")

    rules_cfg = load_brand_rules()
    violations: list[dict] = []

    # R1 时长区间
    dur = rules_cfg["duration"]
    lo, hi = round(dur["min_ratio"] * target, 1), round(dur["max_ratio"] * target, 1)
    if not (lo <= total <= hi):
        over = total > hi
        violations.append({
            "rule_id": "R1_DURATION",
            "message": (f"预计总时长 {total}s 超出目标区间 [{lo}s, {hi}s]（目标 {target}s，"
                        f"{'超出 ' + str(round(total - hi, 1)) + 's' if over else '低于下限 ' + str(round(lo - total, 1)) + 's'}）"),
        })

    # 汇总全部文本（标题 + 各分镜旁白/字幕） + 全部引用的素材 ID
    texts = []   # (位置描述, 文本)
    if isinstance(script.get("title"), str) and script["title"].strip():
        texts.append(("title", script["title"]))
    cited_ids = []
    for s in scenes:
        sid = s.get("id", "?")
        for field_name in ("narration", "subtitle"):
            v = s.get(field_name)
            if isinstance(v, str) and v:
                texts.append((f"{sid}.{field_name}", v))
        for mid in s.get("material_ids", []) or []:
            if mid not in cited_ids:
                cited_ids.append(mid)

    # R2 绝对化用语
    for loc, text in texts:
        for w in rules_cfg["banned_words"]:
            if w in text:
                violations.append({"rule_id": "R2_BANNED_WORDS",
                                   "scene_id": loc,
                                   "message": f"{loc} 出现绝对化用语「{w}」"})

    # R3 反幻觉：数字表述与效果关键词必须能在引用素材原文中找到
    support = store.support_text(cited_ids)
    for loc, text in texts:
        norm = _normalize(text)
        for m in _NUM_UNIT.finditer(norm):
            claim = m.group(1) + m.group(2).replace("秒钟", "秒")
            if claim not in support:
                violations.append({
                    "rule_id": "R3_FACT_TRACEABILITY",
                    "scene_id": loc,
                    "message": f"{loc} 的数字表述「{claim}」在所引用素材 {cited_ids or '（无）'} 原文中找不到依据，禁止编造",
                })
        for kw in rules_cfg["effect_keywords"]:
            if kw in norm and kw not in support:
                violations.append({
                    "rule_id": "R3_FACT_TRACEABILITY",
                    "scene_id": loc,
                    "message": f"{loc} 出现效果表述「{kw}」，但所引用素材 {cited_ids or '（无）'} 中没有相应效果数据，禁止编造",
                })

    # R4 出现品牌名的分镜必须引用素材
    brand = rules_cfg["brand_name"]
    for s in scenes:
        joined = _normalize(str(s.get("narration", "")) + str(s.get("subtitle", "")))
        if brand in joined and not (s.get("material_ids") or []):
            violations.append({"rule_id": "R4_PRODUCT_CLAIM_CITATION",
                               "scene_id": s.get("id", "?"),
                               "message": f"分镜 {s.get('id', '?')} 出现品牌名「{brand}」但未引用任何素材 ID"})

    # R5 引用的素材 ID 必须存在
    for mid in cited_ids:
        if not store.exists(mid):
            violations.append({"rule_id": "R5_MATERIAL_EXISTS",
                               "message": f"引用的素材 {mid} 不存在于素材库"})

    # R6 旁白语速
    from .config import Settings
    cfg = Settings.load()
    max_cps = float(rules_cfg["narration_max_chars_per_sec"])
    from .duration import count_chars
    for s in scenes:
        if not isinstance(s.get("duration_sec"), (int, float)):
            continue
        d = float(s["duration_sec"])
        if d > 0:
            cps = count_chars(str(s.get("narration", ""))) / d
            if cps > max_cps:
                violations.append({"rule_id": "R6_NARRATION_SPEED",
                                   "scene_id": s.get("id", "?"),
                                   "message": f"分镜 {s.get('id', '?')} 旁白语速 {round(cps, 2)} 字/秒，超过上限 {max_cps}"})

    return {
        "pass": not violations,
        "checked_rules": [r["id"] for r in rules_cfg["rules"]],
        "cited_material_ids": cited_ids,
        "target_duration_sec": target,
        "estimated_total_sec": total,
        "violation_count": len(violations),
        "violations": violations,
    }


# ---------------------------------------------------------------- 注册表

@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict          # JSON Schema（供真实模型 system prompt 使用）
    func: callable


@dataclass
class ToolRegistry:
    store: MaterialStore = field(default_factory=MaterialStore)
    failures: dict = field(default_factory=dict)   # tool -> 剩余注入失败次数
    specs: list = field(default_factory=list)

    def __post_init__(self):
        self.specs = [
            ToolSpec(
                name="search_materials",
                description="按关键词检索产品素材库，返回素材 ID、标题与命中片段。检索无结果时通常应改写查询词重试一次，或向用户请求补充素材。",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词，空格分隔多个关键词"},
                        "top_k": {"type": "integer", "description": "返回条数上限，默认 3，最大 10"},
                    },
                    "required": ["query"],
                },
                func=lambda **kw: tool_search_materials(self.store, **kw),
            ),
            ToolSpec(
                name="read_material",
                description="按素材 ID 读取素材全文，返回该素材全部产品事实与效果数据。脚本中的产品事实只能来自本工具返回的原文。",
                parameters={
                    "type": "object",
                    "properties": {"material_id": {"type": "string", "description": "素材 ID，如 P01"}},
                    "required": ["material_id"],
                },
                func=lambda **kw: tool_read_material(self.store, **kw),
            ),
            ToolSpec(
                name="estimate_duration",
                description="按固定口径（旁白字数/语速 4.0 字每秒、单分镜最短 1.5 秒）估算各分镜时长与预计总时长。",
                parameters={
                    "type": "object",
                    "properties": {
                        "scenes": {
                            "type": "array",
                            "items": {"type": "object",
                                      "properties": {"id": {"type": "string"}, "narration": {"type": "string"}},
                                      "required": ["narration"]},
                        }
                    },
                    "required": ["scenes"],
                },
                func=lambda **kw: tool_estimate_duration(self.store, **kw),
            ),
            ToolSpec(
                name="check_rules",
                description="按品牌规则检查脚本：时长区间、绝对化用语、事实可追溯（反幻觉，R3）、品牌名分镜引用素材、素材 ID 存在、旁白语速。",
                parameters={
                    "type": "object",
                    "properties": {
                        "script": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "scenes": {"type": "array", "items": {"type": "object"}},
                                "target_duration_sec": {"type": "number"},
                                "estimated_total_sec": {"type": "number"},
                            },
                            "required": ["scenes", "target_duration_sec", "estimated_total_sec"],
                        }
                    },
                    "required": ["script"],
                },
                func=lambda **kw: tool_check_rules(self.store, **kw),
            ),
        ]

    def spec_map(self) -> dict:
        return {s.name: s for s in self.specs}

    def schemas(self) -> list[dict]:
        """给真实模型的工具清单（含名称、说明、参数 JSON Schema）。"""
        return [{"name": s.name, "description": s.description, "parameters": s.parameters}
                for s in self.specs]

    def set_failures(self, failures: dict) -> None:
        """注入工具故障：{tool_name: 失败次数}。"""
        unknown = set(failures) - set(self.spec_map())
        if unknown:
            raise ToolError(f"未知工具: {unknown}")
        self.failures = dict(failures)

    def execute(self, name: str, args: dict) -> dict:
        """执行工具并归一化返回观察结果（含 args 回显）；永不抛出异常。"""
        spec = self.spec_map().get(name)
        if spec is None:
            return {"ok": False, "tool": name, "args": args, "error_type": "ToolError",
                    "error": f"未知工具 {name}，可用工具: {list(self.spec_map())}"}
        if self.failures.get(name, 0) > 0:
            self.failures[name] -= 1
            return {"ok": False, "tool": name, "args": args, "error_type": "ToolFailure",
                    "error": f"{name} 调用失败（模拟基础设施故障，如服务超时），剩余注入失败次数 {self.failures[name]}"}
        args = args or {}
        try:
            result = spec.func(**args)
            return {"ok": True, "tool": name, "args": args, "result": result}
        except ToolError as e:
            return {"ok": False, "tool": name, "args": args, "error_type": "ToolError", "error": str(e)}
        except TypeError as e:
            return {"ok": False, "tool": name, "args": args, "error_type": "ToolError",
                    "error": f"工具 {name} 参数不合法: {e}"}
