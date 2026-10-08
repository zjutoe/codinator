# Codinator

Codinator 为主 Codex、Pi + Bonsai 和独立 Codex 提供任务交接协议与后台通信渠道。
主 Codex 负责需求、拆解、handoff 和纠偏；Pi 负责实施、Git 提交与测试；独立 Codex 负责验证和验收。
**Codinator 不实施业务任务，不运行项目测试，不调用 Git。** 它派发模型进程，传递原始要求和反馈，保存交接证据，控制状态、预算和串行执行。

本轮实现依据 [交接协议与任务推进改造计划](docs/handoff-review-refactor-plan.md)。新 v2 任务可显式启用 `handoff_protocol: 1`；旧契约保留原行为。格式通过不表示内容合格，文档内容由作者及接收的 Codex／Pi 负责核验。

其他项目接入时可参考[项目 AGENTS.md 调度约定](docs/project-agents.md)。该文档从 AlphaLab 提取可复制的通用约束，并区分项目授权、旧契约与当前控制器能力。

## 环境与安装

Linux、单用户、串行任务；Python 3.11+，已认证的 `pi`、`codex`，以及 `git`、`bwrap`。Python 运行时无第三方依赖。

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps -e .
.venv/bin/codinator doctor
mkdir -p ~/.config/systemd/user
.venv/bin/codinator service > ~/.config/systemd/user/codinator.service
systemctl --user daemon-reload
systemctl --user enable --now codinator.service
```

`doctor` 只验证依赖和沙箱，不调用模型。`service` 只输出 unit，不代为安装或重启。
已有服务仅在确认旧任务停止后升级；不得在活动实施期间换版本。
生成 unit 时保留 PATH 和代理环境；Pi 子进程去掉代理并直连 Bonsai，Codex 审查与通知保留代理。
实施固定为 `bonsai / bonsai2-27b / xhigh`，独立验收为 `gpt-6-astra / xhigh`，不静默更换模型。
Pi 的客户端身份由 RPC 核验，不能证明远端服务实际加载的权重。

指导与审查要求 Codex 支持命名权限配置：继承 `:read-only`，仅授予 `/tmp` 写入以支持
fixture 和日志；外层 bubblewrap 保持源码、Git 与原始证据只读，不使用无沙箱回退。

## 发布与管理

主 Codex 准备普通 Git checkout 的独立任务分支，提交 handoff、固定忽略规则与基线，并确认干净状态。
新发布只接受 [manifest v2](examples/task.json)，填写完整分支和基线 SHA、允许路径、检查、预算及交付要求。
当前不支持 linked worktree。协议详见 [Git 分支与交接](docs/git-checkpoints.md)。

```bash
codinator --state-dir /absolute/state submit /absolute/task.json
codinator --state-dir /absolute/state status TASK_ID
codinator --state-dir /absolute/state pause TASK_ID
codinator --state-dir /absolute/state resume TASK_ID
codinator --state-dir /absolute/state cancel TASK_ID
```

`submit` 授权后台派发；`status` 只读，不启动模型或恢复任务。
`serve` 是独立用户服务入口，前台不另启竞争执行者。主 Codex 不编辑自动任务正在写入的 checkout，也不直接改数据库。
控制器保存发布时的只读 handoff 副本，Pi 与审查者均读取此副本。
发布记录中的 SHA 是发布者声明；真实 Git 核验由 Pi／Codex 执行。

五类模板自带固定 Markdown 栏目和供对应代理遵循的提示词：

```bash
codinator template handoff
codinator template summary
codinator template help
codinator template review
codinator template guidance
```

模板命令不打开状态库、不调用模型。启用新协议时，控制器只检查必要栏目、非空正文、字段类型、身份、回复关联与引用哈希；栏目齐全但正文含糊，或明确写“无”“未执行”，仍可通过格式校验。目标是否清楚、陈述是否真实、证据是否充分、任务是否合格由代理判断。
发布时冻结原 handoff 和五类模板，每次派发留存实际提示词；升级模板不改写历史任务。示例见 [manifest](examples/handoff-task.json) 与 [handoff](examples/protocol-handoff.md)。

## 实施与验收

```text
ready → implementing (Pi: Git + implementation + tests) → reviewing (Codex) → accepted
                     ↑                                      │
                     └──────── needs_changes ───────────────┘
```

Pi 先核对分支、起点和干净状态，修改后提交，再运行约定检查。再次修改必须重新提交并验证。
Pi 交付 summary、completion 和绑定当前任务／attempt 的 `evidence.json`：包含分支、起点／候选 SHA，以及每项检查的精确命令、对应 SHA、状态、退出码和原始工具证据引用。
没有运行的检查显式记为 `not_run`，阻塞时允许无候选包。交付工具只校验格式，不替 Pi 执行测试或 Git。

独立 Codex 核实真实 HEAD、分支、干净状态、祖先关系、逐提交修改范围、冻结契约和 Pi 工具日志，并亲自复跑全部必需检查。
其 verdict 绑定精确 SHA；控制器只依据该 verdict 验收或传递返工意见，不能把 Pi 自报通过当作独立证据。
`status.git.verification` 显示当前身份是待验证声明还是已获独立验收。

Pi 正常结束后，缺失或格式错误的交付可接受一次不超过五分钟、共享原预算的修复。
修复只允许整理已有原始证据，源码和 Git 只读，不能补做测试、提交或编造事实；原始交付保留。
无正常进程／协议终态、实质身份冲突和明确 blocked 不会被修复成成功。

新协议允许 Pi 用 `needs_guidance` 和求助模板提交具体问题，同时提供干净、已提交的部分工作候选与证据包；未做的检查记为 `not_run`／null。确认 Pi 进程结束后，控制器派发源码及 Git 只读的指导 Codex，后者核实真实 Git 现场，再显式返回 `continue` 或 `blocked`。
`continue` 把关联指导交给新的 Pi attempt；最终交付仍交由另一独立 Codex 验收。`blocked` 停止并交回主 Codex。流程只读取控制字段，不根据正文推断阻塞类型或指导有效性。指导结果与原始进程记录在发布前重新核对完整性；缺失、篡改或退出不确定时停止并保留现场。
每次 Pi 实施派发消耗一轮，指导及审查共享原截止时间，不另计实施轮次；恢复后的新实施也计入轮次，review-only 保留原实施轮次。

## 预算与软检查

默认最多四轮、总墙钟四小时、每个 Pi／Codex 进程两小时；实际以已发布任务授权为准。
Pi 的实施／测试、独立 Codex 的复测／验收与返工共享总预算，暂停也计入墙钟。
`checks[].timeout_seconds` 由执行该检查的 Pi／Codex 遵守；控制器实施整个模型进程的硬上限。

`checkpoint_seconds: 1800` 与 `attempt_seconds: 5400` 表示每30分钟软检查、每个模型进程90分钟硬上限。
软检查通过 Pi RPC steer 请求已完成事项、实际检查、阻塞、下一步及 `needs_guidance`，不重启会话。
旧契约中，五分钟内无合法报告或 `needs_guidance=true` 时，控制器在无活动工具的安全边界阻断；长工具仍受硬上限约束。
正常报告可继续工作；已锁定违规不能被迟到报告撤销。最后一个尚在宽限内的请求可由合法正式交付替代，留下明确 resolution。
主 Codex 据证据纠偏，控制器不生成指导内容或诊断。

新协议的检查点报告附带 summary 栏目，作为阶段小结留存并绑定哈希。`needs_guidance=true` 请求 Pi 在原五分钟宽限与实际硬上限内提交最终求助包并正常停止；缺少最终求助包不会继续派发。
临近硬上限时，控制器提前 `min(300 秒, 实际进程时限的一半)` 请求收尾，不延长截止时间，也不替代理写结论。`status` 分别报告当前处理方、待回复对象、最终小结、最近阶段小结和最终小结缺失；无检查点的硬中断也保留缺失标记。实际进程退出码、协议完成与代理自报结果分别记录。

## 中断与恢复

中断时控制器停止进程、保留现场，不创建 Git 提交，不采纳未知 HEAD，不重放不确定提示。
主 Codex 核对并处理 Git 现场后明确 `resume`；新实施 attempt 保留原历史和额度。
完整交付后的独立审查中断，可用：

```bash
codinator resume TASK_ID --review-only
# 仅已有明确预算授权时：
codinator resume TASK_ID --review-only --attempt-seconds 7200 --extra-seconds 3600
```

review-only 固定原始交付和进程证据，不增加实施轮次；新的 Codex 必须重新核验 Git 并执行检查。
已有未协调 verdict、证据缺失或篡改会拒绝重试。省略预算参数不会重置时间或轮次。
`deadline_utc` 是不可被恢复延期越过的绝对上限；详细行为见 [主界面协议](docs/codex-interface.md)。

## 契约与隔离

- `allowed_paths` 是精确文件或以 `/` 结尾的目录；禁止通配符和 handoff、`.git`、`.codex`、`.agents`。
- `checks[].argv` 是交给 Pi／Codex 的精确参数数组，控制器不会直接启动它。
- `git.branch`、`git.base_commit` 指定任务分支和完整基线 SHA。
- `.gitignore` 在发布前固定；缓存不纳入提交，也不由控制器归档或删除。
- `max_rounds`、`max_seconds`、`attempt_seconds` 控制轮次、总墙钟、单模型进程时限。
- `deadline_utc` 可设置冻结的 UTC 绝对截止；`notify_thread` 仅在明确授权通知指定会话时配置。
- v2 不接受旧 `excludes` 或 `integration`，验收不授权合并、推送或自动集成。

Pi 在 bubblewrap 中只能写授权源码、交付和本 checkout 的 Git 元数据。Git 写权限仅用于任务分支提交；禁止改变配置、hooks、分支或历史，Git 命令须禁用 hooks、fsmonitor 和签名。
审查与交付修复阶段源码和 Git 均只读，临时输出写 `/tmp`。父目录为原子替换而开放时，新建越界文件可能产生，须由独立审查核验范围；控制器不删除现场。
控制器按普通 `.git/` 路径锁定同一 checkout 的发布／执行／恢复。它不阻止同 UID 用户绕过控制器改文件，必须遵守单写入者约定；没有无沙箱自动回退。可写挂载在启动前拒绝符号链接、多重硬链接文件和无法遍历的目录，包括 Git 元数据；主 Codex 须准备可核验的普通仓库，控制器不自动复制或拆分共享 Git 对象。

## 历史与证据

v1 只读保留状态、快照、blobs、预算与原验收／集成记录；新版不再执行或恢复，serve 直接跳过。
主 Codex 基于实际剩余时间、轮次和原截止时间准备明确的 v2 后继任务；新 ID 不产生额外授权。
源码历史只由 Git 管理；控制器不再保存源码 blobs、Git 操作意图或代跑检查日志。

状态根默认 `~/.local/state/codinator`，可通过 `--state-dir` 或 `CODINATOR_STATE_DIR` 指定，必须在任务仓外。
主要交接记录为 `manifest.json`、发布时 `handoff.md`、每个 attempt 的 `before.json`／`submission.json`、`pi/`／`codex/` 原始进程记录、三文件 delivery、选定交付的哈希和精确 SHA verdict。
`private/` 含运行配置和认证副本，不是公开证据；分享时只选择必要脱敏内容。

## 服务通知与验证

设置 `notify_thread` 后，通过 `codex queue` 排队发送终态通知。状态和 outbox 同事务提交；失败可 `codinator notify` 重试，不回滚验收。
通知是至少一次语义，含稳定事件 ID；排队成功不代表已读。未配置外部会话时保留本地记录。

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

测试使用隔离假 agent 和本地 Git 仓库，不能代表真实模型连通或真实研究执行；历史真实验证见 [validation](validation/README.md)，不自动证明新协议已做模型实跑。
有界真实试点的准备及指标见 [主界面协议](docs/codex-interface.md)：保留可比较基线，记录求助回传、人工介入原因、格式错误时间／轮次、重复阻塞及停止证据。当前未将模拟闭环标为真实 Pi／模型验证。
源码更新不会升级正在运行的服务；确认旧代理停止后，以新代码重启服务，再发布启用新协议的任务。

实施沙箱将既有 Git HEAD、配置、hooks 和 packed-refs 重新挂载只读；普通提交仍可写 index、objects 和任务 ref。其他 ref 的不变性由 Pi 的起始 `git show-ref` 原始工具记录与独立 Codex 核对，不宣称所有 Git 元数据均有操作系统级写保护。

## S03 后续 harness 改进

新任务可显式启用 `checkpoint_format="compact"`，并声明 `first_checkpoint` 和
`counterexamples`：软报告只填写完成、检查、阻塞、下一步、指导需求与候选 SHA，
完整 summary 留给交付/求助/停止。主 Codex 根据真实证据判进展，控制器只校验格式。
`status` 区分 RPC 收到、代理事件流中观察到请求、报告有效及各自延迟，不声称模型已理解。
30分钟软检查、5分钟回应、90分钟硬限不变，旧任务协议不改。

阶段内各包使用相同 `stage={id,max_seconds,max_rounds}`，共享首次执行起点、固定截止和
实施轮次；失败和外部接管也计数，检查与验收共享时间。权限与依赖预检在发布/启动前完成。
`status.stage` 显示整个阶段成员与剩余额度；阶段按同一状态目录记账，不追溯迁移旧任务。

已授权 Codex 接管可发布 `implementation="external"` 的冻结后继：`begin-external` 记录
起点、作者、额度和仓库占用，主 Codex 实施/提交/测试并通过绑定工具交付，
`finish-external` 只排队独立验收。只有非作者真实验收可 accepted；返工等待原作者，
控制器不调用 Git/测试、不伪造 Pi 进程证据。跨状态目录的控制器也受接管占用约束。
宿主 Codex 负责自身90分钟硬停/30分钟检查，控制器不能杀宿主；超时交付拒收且保留占用。
确认工具全部停止后，可用 `stop-external TASK --summary FILE` 保存完整阻塞小结并释放占用，
即使原交付已存在也不覆盖。执行命令与完整约束见英文 README 的 S03 harness improvements。

本次合成/fake-agent验证不能宣称真实模型连通或 S04 已完成；旧 S03 证据及状态保留原样。

## 只读观察软检查（新 Pi 任务推荐）

新 v2 Pi 任务使用 `handoff_protocol: 1`、`checkpoint_mode: "observe"` 和
`checkpoint_seconds: 1800`。示例见 [observation-task.json](examples/observation-task.json)。
单进程硬限仍设 `attempt_seconds: 5400`。同阶段共享原预算。
省略新模式时保留已发布的报告行为。observe 不支持 external 宿主实施，
也不能与 `checkpoint_format`、`first_checkpoint`、`counterexamples` 同用。

`checkpoint_outputs` 可省略或为空，最多声明 16 个不同的项目相对文件路径。
可声明 handoff 约定的日志和进度文件，不接受目录、通配符、点路径、保护路径和符号链接。
控制器不搜索文件、不运行项目命令、不生成项目进度。handoff 说明输出和所需行为证据。

控制器在启动及每个周期保存不可覆盖、绑定 task/round/attempt 的记录。
记录 UTC、elapsed、来源范围、大小增量、工具开始/结束计数及有界活动工具 ID。
活动工具期间也采集。RPC 日志只引用元数据，不分析或复制模型对话。
声明文件每项最多采样 32 KiB，每次片段单独保存。哈希只绑定采样字节，不绑定整文件。
启动基线区分旧产物。缺失、不可读、非文本、部分 JSON、并发写入、替换和截断明确记录未知。

Pi 无需定时报告。观察器不发送 checkpoint steer，也没有五分钟报告超时。
静默或缺少产物不会自动停工。硬限、取消、最终 summary/evidence 和非作者独立验收保持。
控制器证据写入失败必须明确失败。status 显示观察数、最近记录和证据路径；
证据损坏或绑定错误明确报错。观察记录不证明进度、测试通过或 accepted。
主 Codex 按实际行为证据判断纠偏，需要时使用原生 pause。
核验工具已停并保存 Git 现场后，在剩余预算内发布新的指导 handoff。
观察器不启动指导模型，不追溯迁移旧任务。
