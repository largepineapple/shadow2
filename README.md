# 内容任务规划 Agent

帮助运营人员在预算与截止时间约束下完成内容活动方案。Python 3.11+ CLI、原生 Agent Loop、DeepSeek/OpenAI 兼容工具调用、SQLite 状态持久化。数据、工具费用和耗时均为合成值，仅保存本地方案，不执行发布或实际采购。

**验证结果：29项自动化测试通过；mock与live验收各11/11通过。live套件中8个实际模型执行路径，另3个为缺输入或缓存的无模型路径。**

评审入口：[验收报告](docs/03-evaluation.md)、[费用表](data/tool_costs.json)、[示例输入](examples/inputs/)、[自动生成 Trace](examples/traces/)。

## 安装与运行

```powershell
python -m venv .venv
.venv/Scripts/Activate.ps1
python -m pip install -r requirements.txt
python -m src.main --mode mock --input examples/inputs/replan.json --db campaign.sqlite --trace examples/traces/local_mock.json --output examples/outputs/local_mock.json
python -m pytest -q
python -m examples.run_cases --mode mock
```

mock 不需要密钥，仅做离线流程验证。进程读取环境变量，不自动加载 .env。真实模型：

```powershell
$env:OPENAI_BASE_URL='https://api.deepseek.com'
$env:OPENAI_MODEL='deepseek-flash'
# 在本机安全设置 OPENAI_API_KEY，不放进代码或 Git
python -m src.main --mode live --input examples/inputs/replan.json --db live.sqlite --trace examples/traces/local_live.json
python -m examples.run_cases --mode live --prefix deepseek
```

默认 endpoint/model 即上述 DeepSeek 值；也可通过环境变量使用其他 OpenAI 兼容服务。客户端对 DeepSeek 显式关闭 thinking 模式以支持 required 工具选择，每轮只接受一个工具调用。HTTP 超时 45 秒，无效响应/模型错误最多两次。默认最多 12 轮、10 次工具调用，超限返回 incomplete 并保留成功步骤。仅 completed 退出码为 0，其余业务结果退出码为 1。

## 核心业务与费用

输入 request_id、theme、budget、deadline。缺预算/截止时间返回 needs_information，不调用模型或收费工具；无效数据拒绝。budget 是非负整数模拟单位；deadline 必须带时区 offset。

| 工具/策略 | 模拟费用 | 模拟耗时 |
|---|---:|---:|
| 估算 | 0 | 0 秒 |
| 素材检索 | 2 | 60 秒 |
| premium 脚本 | 10 | 600 秒 |
| basic 脚本 | 6 | 180 秒 |
| 保存方案 | 0 | 0 秒 |

模型先评估高配方案；预算 10 时观察到估算 12 超限，自行选择 basic、重新估算 8，再检索、生成、保存。预算 7 时最低方案仍缺 1，返回 budget_exceeded。截止时间也可能触发更快方案或 deadline_exceeded。

estimate.estimated_total = 已发生费用 + 当前选定方案尚未成功步骤的费用；cost_total 只合计实际成功执行的工具费用。估算不是扣费。合成故障在执行前注入，失败费用与耗时为 0；这是明确的演示假设。真实模型 API 账单不属于这张模拟费用表，没有将模型调用费冒充为模拟费用。

素材检索读取合成目录并按主题标签筛选，找不到时使用通用素材。generate_script 是本地合成模板工具，根据主题与已检索素材生成 hook/scenes/call_to_action；**不是额外的真实文案生成模型**。真实 LLM 负责动作选择与重规划，工具输出如实标记 generator=synthetic_template。

## 状态、依赖与恢复

工具顺序由模型决定，服务端强制依赖：有效估算 → 素材检索成功 → 选定策略脚本成功 → 保存。每个收费动作及保存前重新复核总预算与截止时间，模型无法修改输入约束。返回 task_plan（依赖与步骤状态）、steps（真实结果、错误、次数、费用）、cost_total、estimate（预算/时间缺口）、plan_status/plan_id/saved_plan 或未完成原因。

步骤状态 pending/succeeded/failed；顶层 completed/needs_information/budget_exceeded/deadline_exceeded/incomplete/save_failed/request_conflict。completed 只表示本地方案保存。published 始终 false。

SQLite BEGIN IMMEDIATE 内完成成功步骤缓存检查、约束检查、结果与账本落库；步骤唯一键为 request_id + 逻辑步骤，方案 request_id 唯一。同逻辑请求重复执行返回同 plan_id、plan_status=existing，不增加费用。同 request_id 不同规范化输入或不同 mock/live 模式返回 REQUEST_CONFLICT，避免把 mock 结果当成 live 成果。成功的旧策略结果也会保留；task_plan 只显示当前选定方案，steps 包含历史策略。重试成功时 attempts 增加，早前失败逐次保留在 Trace 中。

故障注入和续跑（使用同一个 DB 和输入）：

```powershell
python -m src.main --mode mock --input examples/inputs/resume.json --db resume.sqlite --inject-failure generate_script --failure-mode once --trace examples/traces/resume_first.json
python -m src.main --mode mock --input examples/inputs/resume.json --db resume.sqlite --trace examples/traces/resume_second.json
```

第一次保留检索成功结果及费用 2，脚本失败不写方案；第二次复用检索，只续跑失败/待处理步骤，总费用 12，不会变成 14。once 的注入记录持久化，同 DB 同请求同工具只注入一次；always 每次都失败。live 模型可在同一轮任务内选择有限重试、替代或停止，不保证与 mock 相同的动作序列。

## 时间与证据边界

默认按当前 UTC 墙钟 + 已完成步骤模拟耗时 + 剩余模拟耗时计算预计完成时间，不实际 sleep。--now 可显式指定固定模拟钟锚点。examples.run_cases 为可复现性使用 2026-10-28T12:00:00Z；Trace started_at/finished_at 仍是真实运行时刻，业务截止时间检验是模拟的，不是已执行真实制作的证明。

runner 使用临时数据库，正常与重复场景共享 DB，失败与续跑场景共享 DB；输出、Trace 和汇总自动生成。缺输入场景不调用模型；已保存方案重复场景为应用缓存命中，Trace 标记 cache_hit，不能当作一次真实模型决策。真实模型验证结果和失败情况以 docs/03-evaluation.md 为准。

## 已知限制

单进程本地演示，无生产身份鉴权、外部素材系统、真实发布/采购。业务时间和费用是模拟值；候选策略只有 premium/basic；脚本为合成模板；单个请求在工具异常后可能因模型选择不同而产生不同轨迹。跨进程恢复依靠 SQLite，无法对真实远程计费 API 提供分布式 exactly-once。个人贡献、投入时间与主观反思保留给候选人本人填写。
