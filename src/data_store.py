# -*- coding: utf-8 -*-
"""数据访问：加载合成素材库与品牌规则。

素材检索为关键词打分检索（Mock 实现）：查询按空格分词，
token 在素材的 标题/产品名/标签/事实/效果数据 文本中出现即算命中，
得分 = 命中 token 数；得分相同按素材 ID 升序，保证结果可复现。
"""
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class MaterialNotFound(KeyError):
    """素材 ID 不存在。"""


def _normalize(text: str) -> str:
    """去掉空白，便于数字表述比对（如 "3 分钟" vs "3分钟"）。"""
    return "".join(text.split())


@lru_cache(maxsize=1)
def load_materials() -> dict:
    with open(DATA_DIR / "materials.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_brand_rules() -> dict:
    with open(DATA_DIR / "brand_rules.json", encoding="utf-8") as f:
        return json.load(f)


class MaterialStore:
    """素材库访问 + 检索。tools.py 的素材检索/素材读取工具基于它实现。"""

    def __init__(self, materials: dict | None = None):
        self._data = materials or load_materials()
        self._by_id = {m["id"]: m for m in self._data["materials"]}

    @property
    def product(self) -> str:
        return self._data["product"]

    def get(self, material_id: str) -> dict:
        if material_id not in self._by_id:
            raise MaterialNotFound(material_id)
        return self._by_id[material_id]

    def exists(self, material_id: str) -> bool:
        return material_id in self._by_id

    def all_ids(self) -> list[str]:
        return sorted(self._by_id)

    def support_text(self, material_ids: list[str]) -> str:
        """拼接引用素材的全部原文（事实 + 效果数据），供事实可追溯比对。"""
        parts = []
        for mid in material_ids:
            if mid in self._by_id:
                m = self._by_id[mid]
                parts.extend(m.get("facts", []))
                parts.extend(m.get("effect_data", []))
        return _normalize(" ".join(parts))

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        tokens = [t for t in query.split() if t]
        hits = []
        for m in self._data["materials"]:
            searchable = _normalize(
                " ".join(
                    [m["title"], self._data["product"]]
                    + m.get("tags", [])
                    + m.get("facts", [])
                    + m.get("effect_data", [])
                )
            )
            matched = [t for t in tokens if t in searchable]
            if matched:
                hits.append(
                    {
                        "id": m["id"],
                        "title": m["title"],
                        "matched_tokens": matched,
                        "score": len(matched),
                        "snippet": (m.get("facts") or m.get("effect_data") or [""])[0],
                    }
                )
        hits.sort(key=lambda h: (-h["score"], h["id"]))
        return hits[:top_k]
