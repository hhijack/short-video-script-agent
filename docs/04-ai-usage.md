# 04-ai-usage.md — AI 辅助内容、个人修改及验证

## AI 辅助范围（如实说明）

本项目的**代码与文档由 AI agent（ZCode，GLM 模型）端到端生成**，包括：

- 全部 `src/` 代码（ReAct 循环、Mock/真实模型入口、4 个工具、CLI）；
- `data/` 合成素材库与品牌规则、`tests/` 全部测试、`examples/` 由脚本真实运行生成；
- `docs/01/02/03` 与 `README.md`。

AI 在开发中报告的调试过程（4 个 bug 的定位与修复、2 个测试用例自身错误）均有对应测试覆盖（详见 docs/03-evaluation.md 第六节）。

## 复核清单（建议作者提交前亲自确认）

> 以下为 AI 建议的最小复核集合，如有修改请在本文件记录。

### A. 须亲自 review 的文件（按重要性排序）

- [ ] `src/agent.py` —— 确认主循环内**没有**业务分支（ReAct 要求的「模型决定下一步」是否成立）
- [ ] `src/model.py` 的 `MockModel.decide` —— 确认每一步是否真的由上一步观察结果决定（尤其 case1 步 8→9 的压缩决策、case3 的改写决策）
- [ ] `src/tools.py` 的 `tool_check_rules` —— 确认 R3 反幻觉逻辑（数字表述/效果关键词必须能在引用素材原文找到）
- [ ] `src/duration.py` —— 确认估算口径（4 字/秒、1.5s 下限）是否符合你对口播语速的常识
- [ ] `docs/02-solution.md` —— 取舍表述是否与你的真实判断一致，改成你自己的话

### B. 须亲自跑过的命令（本机 Python 3.10+ 即可）

```bash
python3 -m pytest tests/ -q                                # 预期：40 passed, 2 skipped（无 key 时 live 测试自动跳过）
python3 -m src.cli run-all --out /tmp/examples_check       # 预期：5 组 Mock 产物，case1 completed
python3 -m src.cli run --topic "映记 新手 出片" --duration 30 --model mock   # 交互式看单次输出
```

- [ ] 以上命令已亲自执行，结果与 docs/03-evaluation.md 所述一致

配置自己的模型 key（写入 gitignored 的 `.env`，参考 `.env.example`）后，建议再亲自验证真实模型路径：

```bash
python3 -m src.cli run-all --out /tmp/examples_real_check --model real
set -a; source .env; set +a; python3 -m pytest tests/test_real_model_live.py -v
```

- [ ]（可选，需 key）真实模型路径已亲自运行，examples_real/ 的产物可复现出同量级的决策行为

> 说明：开发过程中（2026-10-30）曾使用一个仅用于测试的模型 key 完成真实模型验证，key 只写入 gitignored 的 `.env`，未进入任何提交；`examples_real/` 产物已扫描确认不含密钥。

### C. 作者个人贡献

> 本节由 AI 依据协作过程中的真实记录起草；各条目均有会话记录可查证，作者审定后随本仓库提交。

**1. 任务定义与方向把控。** 提出完整的项目要求框架（工作区与交付目录结构、执行顺序、8 项硬性要求、禁止事项与时间盒），框定了实现边界与优先级；过程中做出多项关键决策：接入真实模型并定下密钥使用边界（仅本地 gitignored 的 `.env`、不入仓库）、git 作者身份与仓库信息去标识化要求、仓库公开属性与中性命名、推送与交卷节奏。

**2. 质量审查。** 发起并主持一轮对抗性质量审查，对交付说明逐条独立取证（复跑测试与复现命令、比对全部运行产物、扫描密钥与 git 全历史），产出分级问题清单——P1 真实模型 output 的结构缺陷（risks 为整句字符串、status 为口语表述）、P2 文档数据失真、P3 过程日志时间戳漂移、P4 trace 未记录模型名。P1 的修复（`normalize_finish_output` 结构规整、修复留痕、examples_real/ 全量重跑）与全部文档更正均由该审查直接驱动。审查取证环节同样使用了 AI 工具（与编码部分同属 AI 辅助范围），问题清单的采信与处置要求由作者确定。

**3. 验证与合规把关。** 核验公开仓库不含密钥与任何非项目内容；负责发布渠道的信息填写与最终提交。

## 诚信声明

- 本仓库 `examples/`（Mock）与 `examples_real/`（真实模型 doubao-seed-2.1-lite）下的所有 output.json / trace.json 均为 `src.cli` 真实运行落盘，未手工编辑。Mock 产物可用 `run-all` 重新生成比对（除 trace 时间戳外逐字节一致）；真实模型产物为批次 2 运行快照（经 finish.output 结构规整后重跑；其中 case1 从 3 次真实运行中选取含完整修正链路者，非确定性披露见 docs/03 3.2）。真实模型首跑的结构缺陷已在当前版本修复（见 docs/03-evaluation.md 3.1）。
- 真实模型验证所用 key 仅写入 gitignored 的 `.env`，仓库及其 git 历史中不含任何密钥（已做模式扫描）。
- 未伪造任何「真实模型运行」记录：真实模型的一次行为偏差（case2 自拟时长）与修复过程在 docs/03 如实记录，未掩饰。
- 仓库中不含任何真实密钥（.env 已 gitignore，仅提交 .env.example 空模板）。
