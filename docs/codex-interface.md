# Codex 主界面与交接协议

主 Codex 负责需求、任务拆分、冻结 handoff、发布、状态解释及纠偏。Pi + Bonsai 实施、执行项目命令与测试、生成本地 Git 提交；独立 Codex 检查源码与原始证据、复跑必需检查并验收。
Codinator 只提供交接规范、进程调度、状态和预算管理，不实施项目、不调用 Git、不执行测试、不自行生成验收意见。

## 服务与发布

安装后以实际路径运行 `codinator doctor`，只检查依赖和沙箱。`codinator service` 输出 user unit；主 Codex在授权内安装／重启，先确认没有旧执行者。
所有管理命令必须指向同一 `--state-dir`。关闭主界面不等于暂停后台任务。

主 Codex 准备普通仓库任务分支，提交基线、handoff 和固定忽略规则，确认干净状态，再发布 [manifest v2](../examples/task.json)。当前不支持 linked worktree。
控制器保存原始 handoff 副本供 Pi／Codex读取；其 SHA 记录是交接声明，真实 Git 核验由执行者和审查者承担。

```bash
codinator --state-dir /absolute/state submit /absolute/task.json
codinator --state-dir /absolute/state status TASK_ID
```

主会话不与自动执行者同时修改工作目录，也不修改数据库推进状态。

## 文档模板与格式协议

新 v2 manifest 可显式设置 `handoff_protocol: 1`。未设置的旧契约保持原行为；v1
不支持此字段。使用新代码启动控制器后才能发布该协议，不能让未升级的旧服务接收新任务。

```bash
codinator template handoff
codinator template summary
codinator template help
codinator template review
codinator template guidance
```

模板命令只输出文档，不打开状态数据库或调用模型。五类模板包含固定二级 Markdown
栏目和角色提示词；正文语言不限，允许追加栏目。“无”“未执行”“None”满足非空规则。
控制器仅校验栏目、字段类型、身份、回复关联和证据完整性。栏目齐全但内容含糊仍可
通过格式校验；内容是否清楚、真实、充分和可执行，由 Codex／Pi 判断并负责。

发布时留存原始 manifest、handoff、协议摘要和五类模板的精确字节及哈希。实际派发
保存所用模板身份和完整提示词。更新安装的模板不会改变历史契约。
绑定提交工具自动填写 message 的版本、task／round／attempt、类型、作者、接收方、
message_id、reply_to、contract_digest 和 submission_digest；正文合格与否仍由代理核验。

## 实施交付与一次自动修复

Pi 核对任务分支和起点，先提交再测试。使用 attempt 的只读 delivery contract，通过绑定命令提交 summary、status 和 evidence。
`evidence.json` 固定任务／轮次／attempt，包含分支、起点／候选 SHA，以及每项检查的精确 argv、对应 SHA、状态、退出码、原始工具证据引用。
控制器校验格式、身份和完整性，所有实际 Git／测试声明由独立 Codex核实。

正常 Pi 进程及 RPC 结束后，缺失或格式不合法交付可接受一次修复，最多五分钟并共享剩余预算。
修复的源码和 Git 只读，只从已有 Pi 工具日志补齐交接；不能执行 Git、测试、修改源文件或编造结果。没有足够原始证据则 blocked。
原始交付保留；最终选定目录及三文件哈希记录在 `delivery-selection.json`。
明确 blocked、身份冲突、不确定进程终态或不可信路径不自动修复。

独立 Codex 读取原始 handoff、逐提交 diff、Pi 工具日志和 evidence，核实范围、精确 SHA、干净状态及提交在检查之前，并复跑全部必需检查。
其只读 verdict 才能验收，`needs_changes` 在剩余预算内传回 Pi；控制器不自行判断业务正确性。

协议 1 的 summary 使用 summary 模板；技术求助提交 `--status needs_guidance`，正文
使用 help 模板，仍通过 `--summary` 传入，并附正常 evidence 包。尚未执行的检查如实
标记 `not_run`／null。Pi 必须留下干净且已提交的候选；否则使用 `blocked` 停止。

控制器确认 Pi 协议正常结束、进程退出后，调度单独的只读 Codex 指导进程。
指导者自行核验候选 SHA、分支、干净状态与原始范围，输出绑定原求助的 `continue`
或 `blocked`；它不能修改源代码、改变契约、增加权限／预算或接受任务。
`continue` 启动新的 Pi attempt，从代理声明并由指导者核实的候选继续，附原 handoff
和关联指导。最终交付仍由另一独立 Codex 验收。技术／外部阻塞分类完全依赖代理
提交的控制字段，控制器不分析正文决定路线。

每次 Pi 派发（含指导后的继续及中断后的普通恢复）消耗一个实施轮次；指导和 review
属于对应 attempt，共享总截止和进程上限。相同提交幂等；冲突、过期或跨任务回复拒绝。
格式错误应先由原代理根据提交工具的具体错误当场纠正。独立退出后的交付修复保留
一次五分钟兜底，仍只整理已有证据。

## 暂停、恢复与纠偏

```bash
codinator pause TASK_ID
codinator cancel TASK_ID
codinator resume TASK_ID
codinator resume TASK_ID --review-only
```

暂停／取消由控制器停止所持进程并记录状态。中断时不提交代码、不回滚、不清理缓存、不采纳未知提交。
主 Codex 检查现场、分支和 SHA，按既有授权处理后再恢复；契约或起点需要改变时发布明确后继任务，不能改旧 handoff。
普通恢复创建新 attempt；review-only 固定完整原始交付和进程证据，不重新实施、不增加实施轮次。新的独立 Codex须重新核验 Git 与检查结果。
已有未协调 verdict、缺失或篡改证据拒绝重试；不得通过审查重试绕过已产生的结论。

软检查示例：`checkpoint_seconds=1800`、`attempt_seconds=5400`。每30分钟通过 RPC steer 请求结构化进展，固定五分钟回应宽限。
无有效回应或 `needs_guidance=true` 在无活动工具的安全边界 blocked，仍受90分钟硬上限；有效正常报告继续原 attempt。
控制器不自行分析研究问题或生成纠偏提示。主 Codex查明阻塞、给反例或新的 handoff，再安排后续执行。

上述软检查停止规则适用于未启用格式协议的契约。协议 1 的 progress 另含 `summary`
字符串，按 summary 模板填写；每个有效回复保留为带哈希的阶段小结。
`needs_guidance=true` 后请求 Pi 在最多五分钟且不越过硬截止的宽限内提交最终 help 包
并正常停止；缺失或不可信交付不能触发自动指导。接近硬上限时只发一次收尾请求，
提前量为本次实际有效进程超时的一半与 300 秒中的较小者；不延长任何截止时间。

`status` 的 `current_handler`、`pending_reply_to` 和 `deadline` 表示当前处理方、待回复
关联和任务边界；`summaries` 区分最终交付、最近阶段小结与最终小结缺失。
进程记录保留 `process_exit_code` 与 `protocol_completed`，不能把请求已接收或进程退出
当作任务验收。指导中断、缺失／无效结果或进程终态不确定均停止保存现场，显式恢复
前由主 Codex 核对 Git；不自动重放未知执行。旧 v1 记录保持只读。

## 预算

实施、Pi 测试、独立复测／验收和返工共享任务总墙钟，暂停也计时。
`--attempt-seconds` 覆盖后续单模型进程上限，`--extra-seconds` 只在已有授权时延长总截止；若已过期，从恢复时计算新增时间。
固定 `deadline_utc` 不能被这些选项越过；省略选项不重置预算或轮次。原契约、旧预算和 attempt 证据不改写。
每项检查的命令和超时交给实际执行的 Pi／Codex；控制器管理模型进程硬上限，不另开测试进程。

## v1 历史及升级

v1 的状态、预算、快照、blobs、缓存归档、review 和 integration 证据仅供历史查询。新版公开执行／恢复入口拒绝 v1，serve 跳过，不能修改旧任务伪装成 v2。
升级前确认所有旧模型进程停止；新版 recovery 不对仍活动的 v1 猜测执行或处理源码。
后继任务由主 Codex按实际已用时间与轮次核定剩余预算及适用的原绝对截止，不能采用新任务默认值冲销历史。

源码版本由标准 Git管理，细节见 [Git 分支与交接协议](git-checkpoints.md)。验收后保留任务分支，合并／推送由 Pi／Codex在另有明确授权时执行，控制器没有自动 integration 阶段。

## 有界真实代理试点

模拟／故障测试验证协议行为，不能证明真实模型连接或完整求助闭环已运行。试点使用
独立普通 checkout、独立状态目录和 [示例 handoff](../examples/protocol-handoff.md)，
不以本项目正在修改的工作目录作试点产物。主 Codex 准备并提交 handoff、忽略规则及
基线，填写 [示例 manifest](../examples/handoff-task.json) 的实际分支、完整 SHA、路径、
预算和固定截止，再用已升级代码 `submit`／`serve`。

示例要求先提交一次明确的技术求助：Pi 从干净基线提交 help 与未执行检查声明，
Codex 回答后，新 Pi attempt 实现加法函数，独立 Codex 复测并验收。真实试点至少需要
两个实施轮次。整个试点连同指导、审查、返工共享已核定的剩余预算，不通过新 task ID
或默认值重置本轮已用时间和额度。不得修改旧任务、伪造小结或切换模型以制造成功。

试点前记录未启用新协议时的求助停止行为和人工处理基线；试点后保存请求、指导、
下一 Pi prompt 的回复关联、实际模型进程证据及最终独立 verdict。记录人工介入次数
及原因、格式错误耗时和轮次、重复阻塞，以及停止后有哪些证据足以继续处理。
没有真实证据时明确标记“未运行”，不预设改善幅度；试点结果与代码验收分别报告。

## Passive output observations (recommended for new Pi tasks)

New v2 Pi tasks with `handoff_protocol: 1` can use `checkpoint_mode: "observe"`
and `checkpoint_seconds: 1800`. See [the example](examples/observation-task.json).
Keep the process hard limit at `attempt_seconds: 5400` and publish the shared stage
budget as usual. Omission preserves the frozen report-based behavior. Observation
does not support `implementation: "external"`, or the legacy `checkpoint_format`,
`first_checkpoint`, and `counterexamples` fields.

`checkpoint_outputs` is optional: at most 16 distinct workspace-relative file paths.
It can name existing project logs or progress files specified by the handoff. No
directory paths, wildcards, dot components, protected paths or symlink traversal
are allowed. The controller does not discover files or create progress producers.
The handoff defines what output should exist and what behavior it evidences.

At startup and each interval the controller records immutable attempt-bound
observations, including UTC/elapsed time, source byte ranges, size changes and tool
start/end counts with bounded active IDs. Busy tools do not delay sampling. RPC logs
are referenced by metadata only; model dialogue is not analyzed or copied. Declared
files are sampled at most 32 KiB each. Each sample has its own immutable fragment;
sample hashes identify those bytes, not the whole source file. Replaced/truncated
files, missing/unreadable sources, partial JSON, non-text and concurrent writes are
explicitly unknown. Old output is distinguished by the startup baseline.

Pi has no periodic report obligation. The observer sends no checkpoint steering
request and imposes no five-minute response timeout. Silence, missing artifacts or
partial output do not stop Pi. Cancellation, hard limits, final summary/evidence
delivery and independent acceptance remain required. Controller evidence-write
failure is an explicit error, not a successful observation. `status.observation`
exposes the count, latest record and evidence path; corrupt or wrongly bound evidence
fails explicitly. These records are unverified observations, never project verdicts.
Main Codex assesses actual behavioral evidence and uses native `pause` when guidance
is needed. After confirming tools stopped, it preserves Git state and publishes a
linked corrective handoff under the remaining budget. No guidance model is launched
by the observer, and historical task contracts are not migrated.
