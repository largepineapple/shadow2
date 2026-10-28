# 验收报告

本地实现采用 Python CLI、原生模型工具循环、SQLite 状态与费用账本。实际测试输出见 [pytest 结果](../examples/test_results.txt)。本轮 `python -m pytest -q`：35 passed in 0.53s；初始交付为29项。

## 必须验收与扩展场景

`python -m examples.run_cases --mode mock` 实际退出码0，11/11场景通过；`python -m examples.run_cases --mode live --prefix deepseek` 实际退出码0，11/11场景通过。每个场景输出的费用均与数据库账本核对，保存数量由直接 SQL 查询核实，不是模型自报。

[离线机器汇总](../examples/mock_results.json) / [真实验收机器汇总](../examples/deepseek_results.json)。下表为初始提交dbb174e的历史真实验收结果，业务时钟显式固定为2026-10-28T12:00:00Z，Trace时间戳为实际运行时间；本轮未重新调用DeepSeek：

| 场景 | 状态 | 模拟费用合计 | DB方案数 | 秒 | 证据 |
|---|---|---:|---:|---:|---|
| normal | completed | 12 | 1 | 28.261 | [输出](../examples/outputs/deepseek_normal.json) / [Trace](../examples/traces/deepseek_normal.json) |
| replan | completed | 8 | 1 | 35.545 | [输出](../examples/outputs/deepseek_replan.json) / [Trace](../examples/traces/deepseek_replan.json) |
| missing_budget | needs_information | 0 | 0 | 0.228 | [输出](../examples/outputs/deepseek_missing_budget.json) / [Trace](../examples/traces/deepseek_missing_budget.json) |
| missing_deadline | needs_information | 0 | 0 | 0.144 | [输出](../examples/outputs/deepseek_missing_deadline.json) / [Trace](../examples/traces/deepseek_missing_deadline.json) |
| tool_failure | incomplete | 2 | 0 | 39.719 | [输出](../examples/outputs/deepseek_tool_failure.json) / [Trace](../examples/traces/deepseek_tool_failure.json) |
| over_budget | budget_exceeded | 0 | 0 | 17.291 | [输出](../examples/outputs/deepseek_over_budget.json) / [Trace](../examples/traces/deepseek_over_budget.json) |
| deadline | deadline_exceeded | 0 | 0 | 17.382 | [输出](../examples/outputs/deepseek_deadline.json) / [Trace](../examples/traces/deepseek_deadline.json) |
| duplicate | completed | 12 | 1 | 0.33 | [输出](../examples/outputs/deepseek_duplicate.json) / [Trace](../examples/traces/deepseek_duplicate.json) |
| save_failure | save_failed | 12 | 0 | 34.065 | [输出](../examples/outputs/deepseek_save_failure.json) / [Trace](../examples/traces/deepseek_save_failure.json) |
| resume_failed | incomplete | 2 | 0 | 40.371 | [输出](../examples/outputs/deepseek_resume_failed.json) / [Trace](../examples/traces/deepseek_resume_failed.json) |
| resume_completed | completed | 8 | 1 | 17.744 | [输出](../examples/outputs/deepseek_resume_completed.json) / [Trace](../examples/traces/deepseek_resume_completed.json) |

真实验收总墙钟耗时 231.095 秒。固定模拟时钟用于验证合成制作耗时约束，不代表真实素材制作已经耗时相应秒数。

## 真实动态决策与恢复

共8个场景有真实 deepseek-flash 选动作→工具→观察→后续决策的循环。其余3个场景不应冒称真实模型路径：missing_budget/missing_deadline 在输入校验时返回；duplicate 命中已保存方案缓存，Trace动作是明确标记的应用 cache_hit。

正常场景 premium 估算12、可行，模型下一步检索素材；replan 场景同样估算12但预算10，下一步模型自行再次估算 basic，观察到8可行后执行，最后 cost_total=8。程序没有硬编码替模型选择替代策略，服务端只约束合法性。

工具持续故障时，模型实际选择重试 premium，再估算 basic 并尝试，仍失败后停止。检索成功结果及费用2保留；失败步骤为failed，未生成方案。resume_failed 用同一DB制造这个部分成功状态；移除注入后 resume_completed 的真实动作只有 generate_script→save_plan→finish，检索attempts仍为1，费用总计8。mock 的续跑路径保留premium，最终12，同样只计费一次；不要求两种策略轨迹完全一致。

保存故障时模型有限重试保存后停止，返回save_failed；此前检索和脚本成本12及成功结果保留，没有计划ID。预算不足最低方案仍缺1单位；截止时间不足时basic仍差180秒，均未执行收费制作。缺信息场景没有模型或工具调用，也没有扣费。重复执行返回同一plan_id、费用仍12，DB仅一份方案。

## 自动化覆盖

原有29项pytest涵盖：正常/12→8重规划/无可行预算/截止时间、缺预算或截止时间、输入类型校验、工具依赖、服务端预算边界、一次失败续跑、持续工具错误/保存错误、重复幂等、预算/步数限制和部分成功保留、内容与mock/live模式冲突、并发检索不重复收费、模型虚报成功拦截、运行中截止时间重查、无效工具参数，以及HTTP错误密钥脱敏/DeepSeek兼容参数。

源码编译检查通过。输出与Trace逐项一致性检查通过，已保存方案的当前任务计划全部succeeded。不同策略的历史失败步骤仍保留在steps和Trace中，不能据此把失败步骤涂改为成功。密钥逐字节文件扫描通过，临时测试进程已清除密钥并退出。

## 完成定义与限制

- [x] 可复制运行的mock/live CLI；真实LLM驱动工具循环与重规划。
- [x] 五类强制验收有测试和实际示例，补充保存失败、时间不足与预算限制。
- [x] 费用核算、步骤依赖、持久化状态、保留部分成功、防重复和失败步骤续跑。
- [x] 输出/Trace/费用表/合成素材/依赖/环境变量样例/五份文档齐全。
- [x] 失败如实报告，无发布、无实际采购，不提交密钥。
- [ ] 候选人本人补充04-ai-usage.md的真实个人贡献和时间，以及05-retrospective.md的个人理解和反思。

生成脚本是本地合成模板适配器，不是另一个真实内容生成模型；模拟费用不包含真实LLM API账单，未读取账单因此不报告实际金额。仅对这组固定合成场景取得证据，不等价于生产系统可靠性评测。无生产身份鉴权、真实发布或远程计费工具。跨进程SQLite恢复已验证，但不能据此外推远程API exactly-once。

仓库：https://github.com/largepineapple/shadow2。实际提交SHA通过git log或交付消息核实，文档不嵌入自身提交的循环引用。

## 提交前时间与多重失败修复

改动范围：保持Agent.run、模型提示和业务工具schema不变。Tools._quote拆开实时钟与模拟钟的当前时刻计算，然后统一执行“当前时刻+剩余耗时”。实时钟推进后，不会再把已完成步骤的模拟耗时加到未来；--now仍作为模拟起点，通过已完成账本推进模拟当前时刻。输出quote明确增加clock_mode/current_time。

新增blocking_reasons保留当前预算缺口、截止时间缺口及当前计划工具错误，而不改变原有顶层status优先级。测试先执行basic方案并注入保存失败，再推进墙钟、重新估算premium，使保存错误、预算缺口8与时间缺口1100秒同时存在；三个原因均保留，已完成检索和脚本及费用8也保留，期间没有修改用户预算或截止时间。已弃用策略的失败不阻断已保存替代方案，历史记录仍在steps及原始Trace中。输入缺失、请求错误与循环强制终止也提供结构化原因。

实际回归命令 `python -m pytest -q`：35 passed in 0.53s，退出码0；`python -m examples.run_cases --mode mock`：11/11通过，退出码0。新增6项测试覆盖实时钟推进60/120秒、模拟钟起点与账本续跑、恰好截止时允许完成保存、多重阻断原因、旧策略失败不误报为当前阻断。旧有运行中deadline复查测试也切换为明确的可推进实时钟。

历史DeepSeek汇总、输出和Trace原样保留，不增加或改写字段，也不把旧记录声称为本轮重新执行。新增字段及时间修复目前只通过本轮离线回归验证；历史live证据不证明实时钟修复的模型运行效果。固定起点场景的业务预计时间语义保持原有模拟计算方式。

个人贡献、投入时间和主观复盘仍由候选人本人填写：04-ai-usage.md及05-retrospective.md的两处待填写项保留，不能由Codex编造。代码边界修复完成，不代表这两项个人材料已经完成。
