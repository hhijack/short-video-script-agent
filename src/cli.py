# -*- coding: utf-8 -*-
"""命令行入口。

常用命令：
  # 跑单个用例（离线 Mock，输出写入 examples/<case_name>/）
  python3 -m src.cli run --case examples/cases/case1_normal_30s_compress.json

  # 跑全部内置用例并落盘（端到端复现命令）
  python3 -m src.cli run-all --out examples

  # 直接给请求参数
  python3 -m src.cli run --topic "映记 新手 出片" --duration 30 --model mock --out /tmp/demo

  # 接真实模型（需先配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL）
  python3 -m src.cli run --topic "映记 新手 出片" --duration 30 --model real --out /tmp/demo_real
"""
import argparse
import json
import sys
from pathlib import Path

from .agent import ReActAgent
from .config import Settings
from .model import build_model, ModelError
from .tools import ToolRegistry, ToolError

CASES_DIR = Path(__file__).resolve().parent.parent / "examples" / "cases"


def run_request(request: dict, model_kind: str, max_tool_calls: int,
                inject_failures: dict | None = None) -> dict:
    """构造工具注册表与模型，跑一次完整 ReAct 循环。"""
    registry = ToolRegistry()
    if inject_failures:
        registry.set_failures(inject_failures)
    try:
        model = build_model(model_kind)
    except ModelError as e:
        return {"status": "incomplete_model_error", "output": None,
                "trace": {"final_status": "incomplete_model_error",
                          "final_reason": str(e), "steps": [],
                          "model": model_kind, "request": request,
                          "max_tool_calls": max_tool_calls, "tool_calls_used": 0}}
    agent = ReActAgent(registry, model, max_tool_calls=max_tool_calls)
    return agent.run(request)


def save_case(result: dict, case_meta: dict, out_dir: Path, force: bool) -> Path:
    """把 input / output / trace 落盘到 examples/<case_name>/。"""
    case_dir = out_dir / case_meta["name"]
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "input.json").write_text(
        json.dumps(case_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (case_dir / "output.json").write_text(
        json.dumps(result["output"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (case_dir / "trace.json").write_text(
        json.dumps(result["trace"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return case_dir


def cmd_run(args) -> int:
    if args.case:
        meta = json.loads(Path(args.case).read_text(encoding="utf-8"))
        request = meta["request"]
        name = meta.get("name") or Path(args.case).stem
        inject = meta.get("inject_failures", {})
        description = meta.get("description", "")
    else:
        if not args.topic:
            print("错误：--case 与 --topic 至少提供一个", file=sys.stderr)
            return 2
        request = {"topic": args.topic, "audience": args.audience, "platform": args.platform,
                   "target_duration_sec": args.duration, "style": args.style}
        name, inject, description = "adhoc", args.inject or [], ""
    if isinstance(inject, dict):
        failures = {k: int(v) for k, v in inject.items()}
    else:
        failures = {}
        for item in inject:
            tool, _, n = item.partition("=")
            failures[tool] = int(n or 1)

    result = run_request(request, args.model, args.max_steps, failures)
    meta = {"name": name, "description": description, "request": request,
            "model": args.model, "inject_failures": failures,
            "max_tool_calls": args.max_steps}

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        case_dir = save_case(result, meta, out_dir, force=True)
        print(f"已保存 {case_dir}/(input|output|trace).json")
    print(json.dumps({"status": result["status"], "reason": result["trace"].get("final_reason", "")},
                     ensure_ascii=False))
    if result["output"]:
        print(json.dumps(result["output"], ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


def cmd_run_all(args) -> int:
    out_dir = Path(args.out)
    summary = []
    for case_path in sorted(CASES_DIR.glob("*.json")):
        meta = json.loads(case_path.read_text(encoding="utf-8"))
        # --model 覆盖用例定义（如用真实模型重跑全部用例：run-all --out examples_real --model real）
        model_kind = args.model or meta.get("model", "mock")
        result = run_request(meta["request"], model_kind,
                             meta.get("max_tool_calls", Settings.load().max_tool_calls),
                             meta.get("inject_failures", {}))
        meta = {"name": meta.get("name") or case_path.stem,
                "description": meta.get("description", ""),
                "request": meta["request"], "model": model_kind,
                "inject_failures": meta.get("inject_failures", {}),
                "max_tool_calls": meta.get("max_tool_calls",
                                           Settings.load().max_tool_calls)}
        case_dir = save_case(result, meta, out_dir, force=True)
        summary.append({"case": meta["name"], "status": result["status"],
                        "dir": str(case_dir)})
        print(f"[{meta['name']}] status={result['status']} -> {case_dir}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    from .config import load_dotenv
    load_dotenv()  # 仅 CLI 入口加载 .env（gitignored）；库代码与测试不自动加载

    parser = argparse.ArgumentParser(prog="short-video-script-agent",
                                     description="短视频脚本 Agent（ReAct 循环）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一次脚本生成任务")
    p_run.add_argument("--case", help="用例 JSON 文件（examples/cases/*.json）")
    p_run.add_argument("--topic", help="脚本主题（也是素材检索关键词）")
    p_run.add_argument("--audience", default="泛短视频用户", help="目标受众")
    p_run.add_argument("--platform", default="抖音", help="发布平台")
    p_run.add_argument("--duration", type=float, default=None, help="目标时长（秒）")
    p_run.add_argument("--style", default="轻松口播", help="风格")
    p_run.add_argument("--model", choices=["mock", "real"], default="mock",
                       help="模型入口：mock=离线脚本化决策；real=OpenAI 兼容真实模型")
    p_run.add_argument("--max-steps", type=int, default=None,
                       help="每轮工具调用次数上限（默认 12，可用环境变量 MAX_TOOL_CALLS 配置）")
    p_run.add_argument("--inject", action="append",
                       help="注入工具故障，如 --inject estimate_duration=2（可多次）")
    p_run.add_argument("--out", help="结果落盘目录（写入 <out>/<case_name>/{input,output,trace}.json）")
    p_run.set_defaults(func=cmd_run)

    p_all = sub.add_parser("run-all", help="执行 examples/cases/ 下全部内置用例并落盘")
    p_all.add_argument("--out", default="examples", help="输出根目录（默认 examples）")
    p_all.add_argument("--model", choices=["mock", "real"], default=None,
                       help="覆盖用例定义的模型入口（如 --model real 用真实模型重跑全部用例）")
    p_all.set_defaults(func=cmd_run_all)

    args = parser.parse_args(argv)
    if getattr(args, "max_steps", None) is None:
        args.max_steps = Settings.load().max_tool_calls
    try:
        return args.func(args)
    except ToolError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
