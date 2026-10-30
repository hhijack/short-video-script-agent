# 03-evaluation.md — 测试预期、实际结果、失败和限制

> 所有「实际结果」均引用 `examples/`（Mock 入口）与 `examples_real/`（真实模型入口）下的真实运行产物，与 `python3 -m pytest tests/ -q` 的输出。生成环境：Python 3.14.4 / pytest 9.2.0 / Linux；真实模型为火山方舟 OpenAI 兼容接口 `doubao-seed-2.1-lite`（2026-10-30 运行，key 走 gitignored 的 `.env`，未进仓库）。

## 一、测试总览

- **pytest**：40 个离线用例全部通过（`tests/test_tools.py` 21 个 + `tests/test_agent_scenarios.py` 19 个），覆盖工具单元、反幻觉 R3 正反例、5 场景端到端、步数上限、真实模型接入的离线部分（配置校验/URL 规整/动作解析/finish.output 结构规整）。
- **live 测试**：`tests/test_real_model_live.py` 2 个用例（协议结构断言 + case2 行为断言），配置 key 后运行 2 passed（无 key 自动 skip，离线套件不受影响）。
- **场景运行**：5 组用例 × 2 个模型入口（Mock + 真实模型），Mock 全部与预期一致；真实模型 4 组与预期一致、1 组发现行为偏差并修复（见下文三）。

## 二、场景级预期 vs 实际

| 用例 | 场景 | 预期 | 实际（证据文件） | 结果 |
| --- | --- | --- | --- | --- |
| case1_normal_30s_compress | ① 正常生成；⑤ 超时长修正 | 首稿估算 >30s → 压缩并移除无依据表述 → 重估 ≤30s → 检查通过；输出含 P01 引用 | `completed`；步 7 估算 38.1s，步 8 检查不通过（R1+R3×2），步 9 重估 28.0s，步 10 通过；输出 `estimated_total_sec=28.0`、`material_ids` 含 P01–P05 | ✅ `examples/case1_normal_30s_compress/{output,trace}.json` |
| case2_missing_duration | ② 缺关键信息 | 不猜默认值，0 次工具调用，请求补充目标时长 | `incomplete_missing_info`，`tool_calls_used=0`，questions=「请提供目标时长（秒）…」 | ✅ `examples/case2_missing_duration/{output,trace}.json` |
| case3_no_material | ③ 无素材 | 0 命中 → 改写查询再检索 → 仍 0 命中 → 请求补充 | `incomplete_no_material`；两次 search 的 query 不同（改写可查 trace 步 1/2 的 tool_args）；末步 questions 请求提供素材 | ✅ `examples/case3_no_material/{output,trace}.json` |
| case4_tool_failure | ④ 工具失败 | 注入 estimate_duration 持续故障 → 重试一次仍失败 → 明确未完成，不伪造时长 | `incomplete_tool_failure`；trace 步 7/8 均为 ToolFailure，final_reason 含「无法给出预计总时长，任务未完成」；output 为 null | ✅ `examples/case4_tool_failure/{output,trace}.json` |
| case5_step_limit | 步数上限 | 上限 6 次用满后，模型再申请工具被拒，显式未完成 | `incomplete_step_limit`，`tool_calls_used=6`，末步 action_type=step_limit，已执行步骤完整保留 | ✅ `examples/case5_step_limit/{output,trace}.json` |

## 三、真实模型实测结果（examples_real/，doubao-seed-2.1-lite）

真实模型共运行两个批次：**批次 1**（结构规整修复前的代码，2026-10-30）用于发现行为差异；**批次 2**（当前仓库代码，2026-10-30 深夜–10-31 凌晨）在加入 finish.output 结构规整后全量重跑，即当前 examples_real/ 的内容。批次 1 发现的 output 结构缺陷已在批次 2 修复，修复留痕于 trace 的 finish 步骤 output_normalization 字段。

### 3.1 批次 1 发现（驱动了两处修复）

1. **case2 行为偏差**：模型把缺失的目标时长当作自由裁量（自拟 50.3s 完成任务）。修复：system prompt 增加硬约束「缺主题/目标时长不得自行假定，须立即 incomplete_missing_info」。批次 2 复验正确请求补充（live 测试固化）。
2. **output 结构缺陷（对抗性审查发现，P1）**：case1/4/5 的 output.json 中 risks 是整句字符串（协议要求数组）、case5 的 output.status 是口语表述——trace 真实但交付字段未经审计。修复：`normalize_finish_output`（src/model.py）做确定性结构规整，修复清单经 trace 的 finish 步骤 `output_normalization` 字段留痕，7 个单测固化。
3. 另修复 60s 读超时中断长决策问题（`LLM_TIMEOUT` 可配 + 瞬时重试）。

### 3.2 批次 2 结果（当前 examples_real/，已规整）

| 用例 | 预期 | 真实模型实际（证据） | 判定 |
| --- | --- | --- | --- |
| case1 | 首稿超时 → 修正 → 通过；输出含 P01 引用 | **completed**，8 步 / 7 次调用：检索 → 读取 P02/P05/P01 → 首稿估算 **39.0s**（6 分镜）超时 → **「精简旁白冗余表述」后重新估算 29.0s** → 规则检查通过；output 引用 P01–P05，risks 为数组、status 为枚举 | ✅ 与参考样例同构 |
| case2 缺关键信息 | 请求补充，不猜默认值 | **incomplete_missing_info**，0 次工具调用即请求补充（批次 1 修复后行为稳定） | ✅ |
| case3 无素材 | 改写/多次检索 → 请求补充 | **incomplete_no_material**：检索词自行泛化「量子计算机 维护」→「量子计算机」，仍无结果后明确请求补充 | ✅ |
| case4 工具失败 | 明确处理，不伪造时长 | **completed（降级策略）**：estimate_duration 连续失败 **4 次**后，模型按「工具公开口径（4 字/秒、1.5s 下限）」自行估算，改用 check_rules 交叉验证后交付。与 Mock 的「重试一次后明确放弃」（examples/case4）构成两条都合理的策略对照 | ✅（策略差异如实记录） |
| case5 步数上限 6 | 达到上限显式未完成 | **completed**：真实模型更省步数（5 次调用完成），未触达上限。上限强制执行的证据为 Mock 产物 examples/case5（tool_calls_used=6 → step_limit）与单测 | ✅（上限逻辑由 Mock 路径证明） |

**非确定性披露**：真实模型 case1 共运行 3 次——批次 1 首稿 39.0s→29.5s；批次 2 重跑 A 首稿一次过 27.0s（无修正链路）；批次 2 重跑 B 首稿 39.0s→29.0s。交付样本选取含完整修正链路的重跑 B；三次均为真实运行，首稿长度随模型生成波动，复跑结果可能不同（live 测试只做结构/协议断言）。

**trace 自证**：批次 2 起 trace 的 `model` 字段记录实际模型 ID（`real:doubao-seed-2.1-lite`）。

## 四、两条执行路径的运行记录（模型因工具结果不同而动作不同）

- **路径 A（case1）**：检索命中 5 条 → 逐条读取 → 首稿估算 38.1s → 检查不通过 → **压缩修正** → 28.0s → 通过 → 完成。工具调用 10 次。
- **路径 B（case3）**：检索 0 命中 → **改写查询词**再检索 → 仍 0 命中 → 请求补充素材。工具调用 2 次。
- 另有路径 C（case4，工具失败 → 重试 → 明确失败）、路径 D（case2，缺信息 → 0 次工具调用直接请求补充）。
- 每步记录含：步骤号、决策依据（decision_basis）、工具参数（tool_args）、返回摘要（observation_summary）与完整观察（observation）；对比 `examples/case1_normal_30s_compress/trace.json` 与 `examples/case3_no_material/trace.json` 步 1–2 即可看到同一检索动作在不同结果下的分叉。

## 五、反幻觉验证（关键缺陷项）

- 正例：case1 输出中「3 分钟」可追溯至 P05 原文「使用一键成片模板的平均成片时间 3 分钟（内部实测，2026-09）」（S3.material_ids=[P05]）。
- 反例（测试固化）：`test_check_rules_r3_unsupported_effect_claim_is_violation` —— 旁白写「效率提升 80%」即使引用 P01 也判 R3 违规，因为素材库无任何该效果数据。
- case1 首稿正是利用这一点演示修正：首稿 S4 旁白含「效率翻倍」，被 R3 拦截后模型在压缩稿中移除该表述（trace 步 8→9）。

## 六、开发过程中出现过的问题（均已修复）

1. Mock 模型向 estimate_duration 传参双重嵌套（`{"scenes": {"scenes": ...}}`）→ 修为直接传 `{scenes: [...]}`；
2. R3 正则单位组误写成非捕获组导致 IndexError → 改为捕获组；
3. 状态机把「刚检查完的稿」误判为「下一稿」，case1 首稿检查失败后直接误报 unsatisfiable → 改为按「规则检查次数」推导当前稿；
4. CLI 对 dict 形态的故障注入配置解析错误（只注入 1 次失败）→ 区分 dict/list 两种形态。
5. 真实模型首跑的 output 结构缺陷（risks 为整句字符串、case5 的 output.status 为口语表述）未经审计即落盘，由对抗性审查发现 → 新增 `normalize_finish_output` 结构规整 + 修复留痕（trace `output_normalization`）+ 7 个单测，examples_real/ 已用修复后代码重跑（详见 3.1/3.2）。

## 七、已知限制

1. **真实模型仅实测一个端点**：`doubao-seed-2.1-lite`（火山方舟 OpenAI 兼容接口，2026-10-30）已跑通全部 5 个用例并落盘 examples_real/；其他模型/端点未验证，模型行为差异（如 case2 首跑自拟时长）说明换模型需要复测。
2. **case4 真实模型的降级自估没有独立工具校验总时长**：模型按公开口径自报 estimated_total_sec，check_rules 的 R1 只能校验其自报值与目标区间的一致性、R6 只交叉校验语速，总时长数值本身无第二来源。Mock 路径的 case4（明确放弃）没有该风险。两条策略已在文档中如实对照。
3. **R3 反幻觉为正则/关键词级**：中文数字（如「三分钟」）不在覆盖范围内；语义级改写（如把「成片快」说成「省时间」）不能全部拦截。这是确定性实现与语义理解的边界，已在 docs/02 说明取舍。真实模型生成的脚本同样受 R3 约束（case1 真实输出通过检查）。
4. **检索为关键词打分**：无同义词扩展（改写由模型侧完成），Mock 改写词表固定（MOCK_QUERY_SYNONYMS）；真实模型 case3 自行改写了多组关键词。
5. **真实模型路径的运行非逐字节确定**：examples_real/ 为 2026-10-30 单次运行快照；live 测试只做协议/结构/行为断言，不锁定具体文案。
6. Python 3.14 + 仅标准库 + pytest，未在其他版本验证。
