# -*- coding: utf-8 -*-
"""时长估算核心（确定性模型，供时长估算工具使用）。

口径（与品牌规则检查共用，见 docs/02-solution.md）：
  单分镜时长 = max(最短画面时长, 旁白字数 / 旁白语速)
  字数统计：含标点、不含空白字符；结果保留 1 位小数。
"""
import re

_WS = re.compile(r"\s+")


def count_chars(text: str) -> int:
    """旁白字数：去除全部空白字符后计数（标点计入，代表口播停顿）。"""
    return len(_WS.sub("", text))


def estimate_scene(narration: str, speed: float, min_sec: float) -> dict:
    chars = count_chars(narration)
    duration = max(min_sec, round(chars / speed, 2))
    return {"narration_chars": chars, "duration_sec": round(duration, 1)}


def estimate_scenes(narrations: list[str], speed: float, min_sec: float) -> dict:
    scenes = []
    for i, narration in enumerate(narrations, start=1):
        est = estimate_scene(narration, speed, min_sec)
        scenes.append({"index": i, "narration_chars": est["narration_chars"],
                       "duration_sec": est["duration_sec"]})
    total = round(sum(s["duration_sec"] for s in scenes), 1)
    return {"scenes": scenes, "total_sec": total, "speed_cps": speed, "min_scene_sec": min_sec}
