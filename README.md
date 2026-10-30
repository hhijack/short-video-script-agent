# 短视频脚本 Agent

帮助创作者生成**有事实依据、满足时长限制**的可编辑短视频脚本：给定主题/受众/平台/目标时长/风格，Agent 通过 ReAct 循环调用素材检索、素材读取、时长估算、规则检查四个工具，产出含标题、分镜、旁白、字幕、预计总时长、素材来源 ID、风险与任务状态的脚本；脚本中每个产品事实都可追溯到素材库原文，素材中没有的效果数据（如「效率提升 80%」）会被规则检查拦截。

## 环境与安装

- Python 3.10+（开发验证用 3.14.4）
- 运行时**零第三方依赖**（纯标准库）；pytest 仅为测试依赖

```bash
pip install -r requirements.txt   # 或：pip install pytest
```

## 运行

**一条命令端到端复现全部 5 组验证材料**（离线 Mock 模型，无需任何配置；仓库内 `examples/` 是随仓库交付的运行快照，重跑建议输出到 /tmp 以免覆盖）：

```bash
python3 -m src.cli run-all --out /tmp/examples_check
```

跑单个用例 / 自定义请求：

```bash
python3 -m src.cli run --case examples/cases/case1_normal_30s_compress.json
python3 -m src.cli run --topic "映记 新手 出片" --duration 30 --model mock --out /tmp/demo
# 注入工具故障：--inject estimate_duration=99；调步数上限：--max-steps 6
```

跑测试：

```bash
python3 -m pytest tests/ -q    # 预期 33 passed, 2 skipped（skip 的是真实模型 live 测试，无 key 自动跳过）
```

### Mock 与真实模型：各自验证的边界

| 入口 | 配置 | 能验证 | 不能验证 |
| --- | --- | --- | --- |
| `--model mock`（离线，默认） | 无需任何环境变量 | ReAct 循环、4 个工具、步数上限、故障处理、5 个必须场景的完整流程与产物（确定性，可 diff 复现） | **真实模型的决策质量**——Mock 的决策是预编写的策略函数，只按观察结果路由 |
| `--model real`（OpenAI 兼容） | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`（写入 gitignored 的 `.env`，CLI 自动加载；模板见 `.env.example`） | 真实模型驱动的 ReAct 决策（每轮携带完整历史，要求严格 JSON 动作） | 换用其他模型/端点时的表现（仅实测过下述模型） |

> **两组运行产物都在仓库里，可对照阅读**：
> - `examples/` —— Mock 入口（确定性，5 组）；
> - `examples_real/` —— **真实模型入口实测产物**（火山方舟 OpenAI 兼容接口，`doubao-seed-2.1-lite`，2026-10-30 运行）。5 个用例全部落盘 trace，包括真实模型的动态决策（case1 首稿 39.0s 超时 → 自主精简旁白 → 29.5s 通过）、一个 prompt 修复闭环（case2 模型曾自拟时长，加硬约束后正确请求补充，见 docs/03）与降级策略（case4 估算工具持续失败后按公开口径自估并用规则检查交叉验证）。
>
> 复现命令（需自备 key）：
>
> ```bash
> cp .env.example .env   # 填入 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
> python3 -m src.cli run-all --out examples_real --model real
> # 可选：真实模型 live 结构测试（无 key 自动跳过）
> set -a; source .env; set +a; python3 -m pytest tests/test_real_model_live.py -v
> ```

## 文档导读（建议阅读顺序）

1. 本 README → 2. [docs/02-solution.md](docs/02-solution.md)（调用流程与动态决策，含设计动机）→ 3. `examples_real/case1_normal_30s_compress/trace.json`（真实模型决策路径）与 `examples/case1_normal_30s_compress/trace.json`（Mock 对照）→ 4. [docs/03-evaluation.md](docs/03-evaluation.md)（预期 vs 实际、真实模型发现、失败与限制）→ 5. [docs/04-ai-usage.md](docs/04-ai-usage.md)（AI 使用与待作者确认清单）。

10 组验证材料（Mock 5 组 + 真实模型 5 组）与场景对应关系：

| 目录 | 场景 | 覆盖 |
| --- | --- | --- |
| `examples/case1_normal_30s_compress/` | 正常生成（38.1s→压缩→28.0s） | ① 正常生成；⑤ 超时长修正 |
| `examples/case2_missing_duration/` | 缺目标时长 | ② 缺关键信息 |
| `examples/case3_no_material/` | 检索改写后仍无素材 | ③ 无素材 |
| `examples/case4_tool_failure/` | 估算工具持续失败 | ④ 工具失败 |
| `examples/case5_step_limit/` | 工具调用达到上限 | 步数上限：显式未完成，不静默截断 |
| `examples_real/case1…case5/` | **同 5 个场景由真实模型（doubao-seed-2.1-lite）重跑** | 5 个必须场景的真实模型处理证据；**步数上限的触发证据仅在 Mock 产物与单测**（真实模型 6 次调用内即完成 case5，未触达上限） |

每组含 `input.json`（输入）、`output.json`（实际输出）、`trace.json`（运行记录：步骤/决策依据/工具参数/返回摘要/完整观察）。

## 目录结构

```
src/        agent.py(ReAct 循环+步数上限) model.py(Mock 策略模型+OpenAI 兼容真实模型)
            tools.py(4 工具+故障注入) duration.py data_store.py config.py cli.py
tests/      pytest（33 用例）
examples/   cases/（用例定义）+ 5 组运行产物
data/       materials.json（合成素材库 P01-P05）brand_rules.json（品牌规则 R1-R6）
docs/       01-requirements / 02-solution / 03-evaluation / 04-ai-usage / 05-retrospective
```
