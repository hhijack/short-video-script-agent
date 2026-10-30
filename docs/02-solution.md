# 02-solution.md — 调用流程、动态决策、技术取舍

## 设计动机

1. **反幻觉是本项目的核心要求**（编造产品事实属于关键缺陷），事实可追溯性最适合用「规则检查工具 + 素材原文比对」做成确定性、可测试的检查项（R3），比排期/预算类校验更能展示 Agent 与工具的配合。
2. 需求自带一条数值闭环参考样例（30 秒目标、首稿 38 秒、修正后 28 秒、P01 引用），容易把「估算 → 观察 → 修正 → 再估算」做成可复现、可断言的验收链路。
3. 四个工具职责清晰、互相独立，能在 3 小时内做到每条分支都有真实运行证据。

## 总体架构

```
CLI (src/cli.py)
 └─ ReActAgent.run(request)            src/agent.py   主循环骨架（无业务分支）
     ├─ model.decide(ctx)              src/model.py   决策（Mock 策略模型 / RealModel 二选一）
     │    ctx = request + 全部历史观察 + 已用/上限步数 + 工具 Schema
     ├─ ToolRegistry.execute(tool,args) src/tools.py  4 个 Mock 工具 + 故障注入
     └─ trace 逐步落盘（步骤/决策依据/工具参数/返回摘要/完整观察）
data/  合成素材库 + 品牌规则；tests/  pytest；examples/  每用例 input/output/trace
```

- **主循环骨架只做四件事**：调用模型取动作、执行工具、把观察追加回历史、强制步数上限。检索为空怎么办、超时长怎么办——这类判断全部在模型侧，由观察结果决定，循环内没有任何条件分支。
- **两个模型入口实现同一动作协议**（`call_tool` / `finish` + JSON），替换模型不改循环：
  - `RealModel`：OpenAI 兼容 Chat Completions，环境变量 `LLM_API_KEY / LLM_BASE_URL / LLM_MODEL`，每轮携带完整历史，要求严格 JSON 动作（system prompt 见 `src/model.py`）；**已用真实模型（doubao-seed-2.1-lite）实测全部 5 个用例，产物见 `examples_real/`**；
  - `MockModel`：离线脚本化策略（无网络、无环境变量），**无状态**——每步从观察历史推导下一步，同一起点因观察不同走出不同路径。

## 一次任务的调用流程（case1，examples/case1_normal_30s_compress/trace.json）

| 步 | 工具 | 观察 | 模型下一步（决策依据摘要） |
| --- | --- | --- | --- |
| 1 | search_materials(「映记 新手 出片」) | 命中 5 条 P01–P05 | 逐条读取素材原文（起稿前先拿到全部可用事实） |
| 2–6 | read_material ×5 | P01 仅「支持自动生成字幕」…P05 效果数据「平均成片时间 3 分钟」 | 读齐后基于素材事实起草首稿（5 分镜） |
| 7 | estimate_duration(首稿) | **总时长 38.1s**（目标 30s） | 仍交规则检查，确认全部违规项一起修 |
| 8 | check_rules(首稿) | **不通过 3 项**：R1 超时长 8.1s；R3「效率」「翻倍」无素材依据 | 压缩为 4 分镜、移除无依据效果表述，重新估算 |
| 9 | estimate_duration(压缩稿) | **总时长 28.0s**（落在 [18,30] 区间） | 再交规则检查 |
| 10 | check_rules(压缩稿) | 通过（6 项规则，引用 P01–P05） | finish(completed)，输出最终脚本 |

最终输出包含：标题、4 个分镜（visual/narration/subtitle/duration_sec/material_ids）、`estimated_total_sec: 28.0`、素材来源 ID（P01–P05）、risks、status。与参考样例同构（38→28、含 P01 引用、未编造效果数据）。

## 动态决策点（模型因观察不同而动作不同）

| 观察结果 | 模型动作 | 证据 |
| --- | --- | --- |
| 关键信息缺失（无目标时长/主题） | 不调用工具，直接 finish(incomplete_missing_info) 并列出问题 | case2 trace（0 次工具调用） |
| 检索 0 命中 | 改写查询词（同义替换）再检索一次 | case3 trace 步 1→2，query 由「量子计算机维护 vlog」改写为「量子计算机维护 视频日志」 |
| 改写后仍 0 命中 | finish(incomplete_no_material)，请求补充素材 | case3 trace 末步 |
| 估算超时长 + 规则违规 | 压缩分镜 + 移除无依据表述 → 重新估算 → 再检查 | case1 trace 步 8→9→10 |
| 估算工具持续失败 | 重试一次 → 仍失败则 finish(incomplete_tool_failure)，不伪造时长 | case4 trace 步 7→8→9 |
| 读取单条素材不存在（ToolError） | 跳过该条继续读其余（与基础设施故障区分处理） | src/model.py 策略 ④（含单测覆盖的观察分类） |
| 工具调用达到上限 | 循环拒绝执行，finish 前显式返回 incomplete_step_limit | case5 trace 末步 |

## 关键设计

- **反幻觉（R3）**：脚本中每个「数字+单位」表述（3 分钟/80%/1080P…中的可追溯类）与效果关键词（效率/翻倍/提升/节省）都必须能在所引用素材的原文中找到；引用素材列表本身来自 `read_material` 的观察，不是模型记忆。素材里没有的效果数据一律判违规——「效率提升 80%」用例见 tests/test_tools.py。
- **步数上限**：`MAX_TOOL_CALLS`（默认 12，可配置）。循环在执行第 N+1 次工具调用前检查，达到上限即返回 `incomplete_step_limit`，trace 完整保留已执行步骤，绝不静默截断。
- **故障注入**：`ToolRegistry.set_failures` 在包装层模拟基础设施故障，用例 JSON 携带配置，业务代码零污染。
- **可复现**：估算口径、检索打分、Mock 策略全部确定性，`run-all` 任意重跑产物逐字节一致（时间戳字段除外）。

## 技术取舍

核心取舍三条：原生实现 ReAct 不引框架（决策来源可审，备选 LangGraph/ADK 被放弃的原因是图编排会掩盖决策来源并引入依赖）；Mock 模型按观察路由而非固定序列（固定序列无法响应故障注入与检索差异）；时长估算采用确定性口径（验收可断言，且估算与规则检查共用同一口径避免互相矛盾）。
