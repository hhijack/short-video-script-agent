# -*- coding: utf-8 -*-
"""全局配置：环境变量与估算常量。

真实模型通过环境变量接入（OpenAI 兼容接口）：
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
离线 Mock 入口不依赖任何环境变量。
"""
import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> None:
    """把 repo 根目录 .env（gitignored）载入 os.environ；已存在的环境变量优先。

    仅在 CLI 入口（src/cli.py main）调用，库代码与 pytest 不自动加载，
    保证测试不依赖本机 .env。
    """
    p = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout: int = 180
    max_tool_calls: int = 12
    # 时长估算模型常量（与规则检查共用同一口径，见 docs/02-solution.md）
    narration_speed: float = 4.0   # 旁白语速：字/秒（含标点，不含空白）
    min_scene_sec: float = 1.5     # 单分镜最短画面时长（秒）

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_base_url=os.environ.get("LLM_BASE_URL", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
            llm_timeout=int(os.environ.get("LLM_TIMEOUT", "180")),
            max_tool_calls=int(os.environ.get("MAX_TOOL_CALLS", "12")),
        )



