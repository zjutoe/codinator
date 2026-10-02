# Codinator

## 摘要

Codinator 的设计初衷是实现 **Codex 与 Pi 的协作，即强模型与弱模型的协作**。
Codex 使用强模型负责需求分析、任务规划与独立验收，Pi 调用较弱模型（当前为 Bonsai）
负责具体代码实施，并根据审查反馈修正。控制器将实施、检查、验收和返工串联为可持续运行、
可追溯的流程，减少人工派发任务、跟踪进度和反复转交修改意见的负担。

以 **Codex 为唯一交互界面**：在 Codex 主会话中讨论需求、发布任务、查询状态和处理异常；
独立后台服务负责 Pi + Bonsai 实施、必需检查、Codex 独立验收与自动返工。
关闭主界面不会暂停后台任务，只有独立验收结论可以接受工作。

项目提供任务暂停与显式恢复、修改范围和执行预算控制、沙箱隔离及逐轮证据留存；
经明确授权，还可在验收通过后提交代码并快进合并。

## 运行环境

面向 Linux、单用户、串行任务。需要 Python 3.11+，以及已安装并完成认证的
`pi`、`codex`、`git`、`bwrap`；Python 运行时无第三方依赖。
Pi 通过 RPC 执行实施任务，Codex 通过独立审查进程验收，均由控制器调度。

## 安装与服务

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps -e .
.venv/bin/codinator doctor

mkdir -p ~/.config/systemd/user
.venv/bin/codinator service > ~/.config/systemd/user/codinator.service
systemctl --user daemon-reload
systemctl --user enable --now codinator.service
systemctl --user is-active codinator.service
```

`doctor` 只检查本地依赖和沙箱，不调用模型。`service` 只输出 unit；生成时保存 PATH 和代理环境。
已有安装应先核对运行任务与配置，在服务空闲时更新和重启。
需要代理时，在生成 unit 的命令上设置，例如：

```bash
env http_proxy=http://127.0.0.1:8888 https_proxy=http://127.0.0.1:8888 \
    .venv/bin/codinator service > ~/.config/systemd/user/codinator.service
```

地址按本机环境调整。Pi + Bonsai 子进程移除代理并直连本机服务；Codex 审查与通知保留服务代理。
全局代理和客户端配置不会被修改。实施固定为 `bonsai / bonsai2-27b / xhigh`，
验收固定为 `gpt-6-astra / xhigh`。Pi 实际客户端身份由 RPC 校验和记录，不以模型自述作证。

## 在 Codex 中管理任务

复制 [examples/task.json](examples/task.json)，将工作树、交接文档、允许路径和检查命令改为真实值。
可用 [示例交接文档](examples/demo-handoff.md) 在空的独立仓库中准备任务。
workspace 必须是独立 Git 仓库或 worktree 的根目录；发布时保存包括未提交改动在内的基线。

```bash
.venv/bin/codinator submit /absolute/path/to/task.json
.venv/bin/codinator status TASK_ID
.venv/bin/codinator pause TASK_ID
.venv/bin/codinator resume TASK_ID
.venv/bin/codinator cancel TASK_ID
```

`submit` 是明确派发授权，服务仅处理已发布任务。`status` 只读，显示状态、原因、轮次、
attempt、证据位置和带 attempt 标识的最近审查；它不会恢复任务或启动模型。
`serve` 是供独立用户服务使用的控制器入口。日常由主 Codex 会话调用上述管理命令。
操作与故障处理见 [Codex 主界面协议](docs/codex-interface.md)。

## 执行与恢复

```text
ready → implementing → checking → reviewing → accepted
             ↑                       │
             └──── needs_changes ────┘
```

必需检查失败或独立审查要求修改时，控制器在剩余轮次内自动安排 Pi 返工。
默认最多四轮实施，总墙钟四小时，单次 Pi 或 Codex 进程上限两小时；
实施、检查、审查与返工共享总时间，暂停时间也计入总墙钟。相同 issue ID 不提前终止返工。
连接故障、进程输出截断、越界改动和不可信验收结果会阻塞任务；不重放不确定提示。

实施交付采用只读的 attempt 契约与绑定提交工具，Pi 只提供汇总和交付状态。
Pi 确认正常结束且范围检查通过后，缺失或格式错误的交付可在剩余预算内接受一次、最多五分钟的
专项修复，工作树及原证据只读。控制器固定选用的交付后再执行检查与独立审查。
排除条件与证据规则见 [实施交付与一次自动修复](docs/codex-interface.md#实施交付与一次自动修复)。

可选 `checkpoint_seconds` 通过 Pi RPC `steer` 按间隔请求实施进展。例如设置
`checkpoint_seconds: 1800`、`attempt_seconds: 5400`，就是每 30 分钟软检查、单进程 90 分钟硬上限。
请求在下一个安全工具／轮次边界送达，不强制打断长工具，不重启会话、不重发实施 prompt，也不延长预算。
`status.latest_checkpoint` 分别显示最近请求、RPC 确认、最近结构化进展和未回应请求；进展包含完成事项、
实际执行的检查、阻塞、下一步及 `needs_guidance`，属于实施者声明，不能触发验收。
每次请求必须在固定五分钟回应宽限内提交合法、绑定当前 attempt 的报告；完成事项和检查数组可以为空，
但阻塞必须如实报告。超过宽限仍无合法报告，或报告 `needs_guidance=true`，控制器会在没有活动工具的
下一个安全边界阻断本 attempt。活动工具可以先完成，但仍受原硬上限约束；已记录的违约不能被迟到报告
撤销。及时提交 `needs_guidance=false` 的正常任务继续工作并进入下一软检查点，不会一律在第 35 分钟终止。
主 Codex 指导冻结后的 blocked 任务；控制器不自动重派或启动 planner 模型。

agent 正常结束时仍会核对未回应请求。只有最后一个检查点、尚在回应宽限内且正式交付合法时，才允许
以正式交付替代，并在 `resolutions/` 明确记录 `source=final_delivery`，不冒充已经收到进展报告。
旧试点已经 accepted 的证据不追溯改判。`status` 区分缺失报告、控制器 resolution、已锁定的 `violation`
和活动工具结束后实际执行的 `stop` 记录。

普通 `resume` 创建新的实施 attempt，旧证据保留。Pi 已完整交付、检查通过，
仅 Codex 审查因额度、连接或进程中断而未完成时，可以显式只恢复审查：

```bash
codinator resume TASK_ID --review-only
# 确需调整预算时显式指定：
codinator resume TASK_ID --review-only --attempt-seconds 7200 --extra-seconds 3600
```

仅审查恢复会核对控制器选用的交付、检查证据与冻结快照，创建新审查 attempt，不增加实施轮次。
若交付经过修复，还会验证两个 Pi 进程的证据；已通过检查的旧协议历史交付仍受支持。
缺失或损坏证据、工作树变化、检查失败、未完成实施，以及已有但尚未接收的 verdict，都会拒绝该操作。
若新审查要求返工，服务继续安排下一轮实施与检查。

`--attempt-seconds` 覆盖之后单个模型进程的上限；`--extra-seconds` 延长总截止时间，
若已过期则从恢复时计算新增时间。省略参数保留原预算；检查自己的超时不变。
变更原子记录到 SQLite 事件，新 attempt 的 `budget.json` 保存生效预算，原 manifest 和旧证据不改写。

服务启动时自动检查中断现场；手工执行 `recover` 也只做恢复核对，不派发任务。
检查原因与证据后才能显式 `resume`。工作树偏离已记录快照时，需要人工核对并恢复，或发布新任务；
控制器不会自动 reset、stash 或回滚。延长超时不能修复代理、额度或连接问题。

manifest 可通过 `integration` 显式授权验收后的 Pi 提交和快进合并。
控制器核对接受快照与完整提交树，推广同一个提交，合并完成后才进入 `accepted`；不自动 push。
未配置时，任务在验收接受后结束。授权和恢复规则见 [验收后集成](docs/codex-interface.md#验收后由-pi-提交并合并)。

## 升级边界

本版移除原生 Pi 终端界面、前台 `run` 命令及单独的 Codex 探针；状态查询不再提供 `--mode`，
结果也不再包含 `mode` 字段。所有操作统一使用后台任务的状态根目录。
旧 `interactive/` 目录、任务契约和 attempt 证据保留在仓外，不自动迁移或重新派发。
升级前结束旧界面会话并检查遗留任务，勿将其目录直接交给后台服务，或重复发布仍在执行的工作树。

## 任务契约

`handoff` 是只读契约，不允许出现在 `allowed_paths` 内。执行者写独立的本轮汇总，
SQLite 是状态的唯一来源；本版不自动改写项目 README／交接文档中的状态。

- `allowed_paths`：精确文件，或以 `/` 结尾的目录；不支持通配符，不允许 `.git`／`.codex`／`.agents`。
- `checks[].argv`：参数数组，直接启动进程，不经过隐式 shell。检查在只读工作树中运行；临时产物写 `/tmp`。
- `excludes`：不纳入内容快照的环境／缓存路径；不能覆盖 tracked 文件，也不能与允许修改范围相交。
- `max_rounds`、`max_seconds`、`attempt_seconds`：发布前确定的执行边界。
- `checkpoint_seconds`：可选正整数，须小于 `attempt_seconds`；仅实施阶段启用软进展请求，省略时沿用原行为。
- `deadline_utc`：可选绝对截止，如 `2026-10-02T06:00:09Z` 或等价的 `+00:00` 时间。首次派发取该截止与 `now + max_seconds` 较早者；已过期不启动 agent。排队延迟及显式恢复增加预算均不能推迟这个冻结上限。
- `notify_thread`：可选，用户明确指定的 Codex 会话 ID。未设置时保留本地通知，不向外发送。

新任务需要单独 worktree 时先人工／Codex 创建，明确环境与基线，再发布。
Codinator 不自动创建工作树或搬迁虚拟环境，避免隐式丢失未提交文件及破坏绝对路径契约。

## 证据与权限

默认状态目录 `~/.local/state/codinator`；可用 `--state-dir` 或 `CODINATOR_STATE_DIR` 指定。
必须在任务工作树之外，不应提交到 Git。目录权限为 0700。

```text
state.sqlite                          状态、轮次、通知 outbox
blobs/<sha256>                        去重后的不可覆盖文件内容
tasks/<task>/
  manifest.json / intake.json          发布时的契约与完整快照
  handoff.md                          发布时的原契约文本
  attempt-0001/
    implementation.json              后台 Pi 确认正常结束后的原始现场（归档缓存之前）
    before.json / submission.json     文件内容、类型、模式、Git HEAD/index
    diff.json                         本轮改动范围与快照摘要
    pi-runtime.json                   RPC 报告的实际客户端模型身份
    pi/                               原始 RPC、stderr、进程与退出记录
    delivery-contract.json            只读的任务／轮次／attempt 与目标目录绑定
    submit-delivery.py                 绑定的汇总／状态提交命令
    delivery/summary.md                Pi 的改动、偏差、未运行项
    delivery/completion.json           本轮提交标识
    delivery-error.json                原交付无效时的逐字段诊断
    delivery-repair/                   可选的一次修复：绑定工具、契约、Pi 证据及 delivery/
    delivery-selection.json            控制器选定的交付目录及两个文件身份
    checkpoint-contract.json           可选的只读进展身份与目标目录
    submit-checkpoint.py                绑定的进展专用提交命令
    checkpoints/requests/              控制器发出的编号 steer 请求
    checkpoints/acks/                  RPC 确认；不代表收到进展
    checkpoints/reports/               实施者提交的编号进展声明
    checkpoints/policy.json            新 attempt 固定五分钟回应宽限
    checkpoints/resolutions/           控制器冻结的报告或及时正式交付替代记录
    checkpoints/violation.json         已锁定的超时／指导要求（若有）
    checkpoints/stop.json              活动工具结束后的控制器阻断记录
    checks/<name>/                    调度器独立执行的命令、退出码、原始输出
    codex/                            独立审计的原始事件与退出记录
    review-delivery/verdict.json       绑定本轮摘要的结构化验收结果
    outcome.json / review.md           最终判定／返工要求
private/                              运行时配置、会话与认证副本；不是公开证据
notifications/                        通知的每次发送尝试
```

开发期命令保存在 Pi 原始工具事件中；调度器必需检查另有精确退出码和输出摘要。
任意开发命令的文本输出不会被冒充为结构化测试计数。
快照覆盖 tracked、untracked、普通 ignored 文件、目录、删除项、模式与符号链接；
明确排除的环境／缓存内容不作完整性承诺。符号链接不跟随读取。

Pi 在 bubblewrap 中运行：主机文件默认只读，Git 元数据与现存非授权文件只读；
允许文件所需父目录可写以支持原子替换。新建越界条目会被事后范围检查拒绝，
这不等于所有非法新文件都在创建前被阻断。超范围时保留现场并暂停，不自动删除。
后台 Pi 进程与 RPC 确认正常结束后会先留存原始现场，再归档严格识别的新 Python 缓存；有其他越界时不清理，
冻结检查／审查不清理。未完成协议与中断恢复仍要求检查原始证据和快照，详见主界面协议。
Codex 也有外层进程隔离，工作树只读，CLI 内层显式 `--sandbox read-only`、`-a never`。
两种 agent 都启用父进程退出清理与 PID namespace；没有不受限运行的自动降级。
这属于单用户工程隔离，不是对恶意同 UID 主机进程的安全防线。

## 后台服务与通知

服务只执行明确发布到所配置状态目录的任务。不同状态目录需生成对应 service 参数。
生成的 unit 保存当时的 PATH 和代理环境，确保后台 Codex 仍经代理；Pi 的直连规则不变。
注销后继续运行取决于本机 user service／linger 设置；程序不修改系统级策略。

配置 `notify_thread` 后，以 `codex queue` 排队发送通过／阻塞通知。SQLite 终态与通知入队
在同一事务中完成。发送失败不会回滚验收；`codinator notify` 可重试。
发送采用至少一次语义，消息含稳定事件 ID；极端崩溃时可能重复，接收方应按 ID 去重。
CLI 排队成功不代表用户已读；自动审计不依赖通知通道或当前聊天窗口。

## 验证与维护

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用明确标识的假 agent 子进程，不调用模型；覆盖返工、恢复、截断、错误退出、
并发修改、范围越界、通知重试、身份校验、证据发布及后台子进程清理。
真实模型／沙箱联调证据另列于 `validation/`，不能用模拟测试代替。
验证方法与历史证据说明见 [验证记录](validation/README.md)。
项目的后续运行中，完整 state 目录可包含认证和原始工具输出，分享前只选所需脱敏证据。
