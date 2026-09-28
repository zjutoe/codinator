# Codinator

让 Codex 制订任务、Pi + Bonsai 实施、Codex 独立验收，并自动安排返工。
你发布一份明确的任务契约，程序负责派发、留证、冻结、验收和通知。

首版面向 Linux、单用户、串行任务；Python 3.11+，Python 运行时无第三方依赖。
需要已安装并完成认证的 `pi`、`codex`、`git`、`bwrap`。

2026-09-28 统一项目名称为 **Codinator**：Python 包、发行包和命令均为 `codinator`，
环境变量使用 `CODINATOR_` 前缀，默认状态目录为 `~/.local/state/codinator`。
升级现有安装前停止旧控制器和服务，卸载原发行包，再按下文安装；不要让两个版本同时操作同一工作区。
已有任务继续通过 `--state-dir` 指向原状态目录，不搬迁或重写原 manifest、会话和证据。
服务 unit 须重新生成，环境变量及外部调用脚本须同步改名；安装本身不启用服务或自动派发任务。

## Pi 主界面：自动实施与审查

在交互终端启动一份明确的任务 manifest：

```bash
cd ~/src/llm/codinator
env http_proxy=http://127.0.0.1:8888 https_proxy=http://127.0.0.1:8888 \
    .venv/bin/codinator pi /absolute/path/to/task.json
```

终端中显示原生 Pi CLI 和 Bonsai 的实时输出。任务实施 → 自检 → 提交 Markdown 总结 →
后台检查和独立 Codex 审查 → 自动返工 → 再提交，直到接受，正常轮次无需用户确认。
Pi 固定使用 `bonsai/bonsai2-27b/xhigh` 并直连本机；Codex 固定使用 `gpt-6-astra/xhigh`
并继承上面为控制器设置的 proxy。不会改全局代理设置。

Pi 完成后调用扩展提供的 `codex_submit_review` 工具，程序负责生成 `submission.md`。
Codex 的正式意见保存在 `review.md` 并显示在 Pi 中；中间模型输出保存在仓外原始记录中。
这些文件位于 `~/.local/state/codinator/interactive/tasks/TASK_ID/attempt-NNNN/`，
每轮使用新目录，历史保留。Pi 原生会话位于同一 state 根下的 `private/TASK_ID/`。

常用命令直接输入 Pi：

- `/codex-status`：显示阶段、轮次和证据目录。
- `/codex-pause`：停止自动循环并中断当前实施或审查。
- `/codex-resume`：明确恢复。冻结提交已完成检查时，只恢复 Codex 审查。

按 Esc 中止当前模型回合、输入新消息介入、切换会话或退出 Pi，都停止自动续轮。
退出后重新打开使用：

```bash
env http_proxy=http://127.0.0.1:8888 https_proxy=http://127.0.0.1:8888 \
    .venv/bin/codinator pi TASK_ID --resume
```

可省略 `--resume` 先查看现场，再在 Pi 中决定恢复。不会因重启而重放不确定的旧提示。
默认最多四轮；`needs_changes` 在剩余轮次内自动交回 Pi，即使问题编号与上一轮相同。
稳定编号用于追踪同一问题，不能据此判断修复没有进展。轮数耗尽、额度/连接故障、范围或冻结快照变化等情况会停止并说明原因。
正常返工不需逐轮确认，但程序不承诺无限重试或任何任务一定被接受。验收后也不自动 commit/push/merge。

旧版因 `Two consecutive reviews retain the same issue set` 停在 `blocked / feedback` 的交互任务，
更新后退出旧控制器，再用 `codinator pi TASK_ID --resume`（或新会话内 `/codex-resume`）明确恢复。
控制器核对冻结快照、检查证据和最近返工结论，在原 `max_rounds` 内进入下一轮；保留全部旧 attempt，
重新提交时才创建新 attempt，不重跑旧审查。此恢复不解除其他阻塞、不增加预算，也不修改契约。

原生 Pi 的提交工具先返回“请求已排队”，这不是正式接收回执。冻结前的范围校验若明确拒收，
控制器会保存拒收现场，并将具体问题交回 Pi 一次；重复拒收会暂停。超时、断连或接收结果不确定
时仍暂停，不自动重放。暂停原因保存在 SQLite 和 Pi 会话中，退出会话不会覆盖已有原因。

Python 缓存优先写入仓外的私有 Pi 配置目录；`-I` 会忽略环境变量，因此子进程仍应显式使用 `-B`。
提交及显式恢复实施前，控制器可先留证再清理白名单外的新字节码缓存：仅限新建、非链接的
`__pycache__/模块.版本标签[.opt-1或2].pyc`，对应源码存在，标签与 magic 匹配控制器 Python。
已有缓存、其他版本、符号链接、硬链接和任意其他越界文件继续拒收；`.gitignore` 不扩大允许范围。
清理前保存完整快照及内容，实际缓存移动到仓外 `cache-cleanup-*/removed/`，原 attempt 不改写。
移动要求工作树和证据目录在同一文件系统；归档失败、并发变化或跨盘移动失败时保留现场并停止，
不会回退到删除文件。冻结的检查／审查阶段不执行缓存清理。详见 [验证记录](validation/submission-recovery-20260928/README.md)。

交互模式不会把用户阅读/讨论时间算作任务总墙钟，manifest 的 `max_seconds` 在此模式不适用；
`attempt_seconds` 限制单次 Codex 审查（默认 7200 秒），各检查保留自己的超时。
Pi 模型执行受其客户端设置和主动中断控制。新入口与旧 `run/serve` 使用独立状态，
共享工作目录互斥锁；不要把正在旧流程执行的同一任务直接迁移进来。
详见 [Pi 主界面协议](docs/pi-interface.md)。

## 快速开始

```bash
cd ~/src/llm/codinator
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps -e .
.venv/bin/codinator doctor
```

复制 [examples/task.json](examples/task.json)，将 workspace、handoff、允许路径和检查命令改为真实值。
可用 [示例交接文档](examples/demo-handoff.md) 在空的独立仓库里先试跑。
workspace 必须是独立 Git 仓库／worktree 的根目录；可以有未提交改动，发布时会整体留存。
不要把正在人工编辑的工作树直接投入自动运行。

```bash
.venv/bin/codinator submit /absolute/path/to/task.json
.venv/bin/codinator run demo-add
.venv/bin/codinator status demo-add
```

`submit` 是明确派发授权：程序不会扫描项目里所有 `ready` 文档自行开工。
`run` 在当前终端执行完整闭环；`serve` 在后台串行处理已发布任务。
后台服务已启用时，`submit` 后直接用 `status` 查看结果，不再并行执行 `run`。

**前台运行的代理：** `run`／`serve` 继承当前终端环境；它们不会读取 systemd unit 保存的代理变量。
本机 Codex 需要代理时，前台命令也必须带上代理。以下为本机 `127.0.0.1:8888` 的示例，
其他机器替换为自己的地址；仅影响这一条命令，Pi 子进程仍移除代理并直连 Bonsai：

```bash
env http_proxy=http://127.0.0.1:8888 https_proxy=http://127.0.0.1:8888 \
    HTTP_PROXY=http://127.0.0.1:8888 HTTPS_PROXY=http://127.0.0.1:8888 \
    NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
    .venv/bin/codinator run demo-add
```

遇到 `Process exceeded wall-clock budget`，先查看最新 attempt 中 `pi/result.json`、
`checks/*/result.json`、`codex/result.json` 来定位阶段。若 Codex 日志反复出现连接失败，
先恢复代理／网络；延长墙钟预算不会修复连接。恢复使用新 attempt，旧证据保留。

实施固定为 `bonsai / bonsai2-27b / xhigh`，验收固定为 `gpt-6-astra / xhigh`。
实际 Pi 客户端身份通过 RPC 校验并记录，不把模型自述当作证据。
Pi + Bonsai 直连本机服务，子进程会移除代理变量并设置 `NO_PROXY=*`；
Codex 审计与通知保留启动控制器时的代理环境。不会修改你的全局代理或 Pi 配置。
控制器只串行调度自己的任务，不管理其他 Pi 客户端。Bonsai 只有一个推理槽时，
并行使用手工会话可能产生排队和上下文缓存竞争；联调宜安排在服务空闲时。

## 工作流

```text
ready → implementing → checking → reviewing → accepted
             ↑                       │
             └──── needs_changes ────┘
```

必需检查失败会附带原始结果进入返工；检查通过后才启动 Codex 审查。
Codex 根据原契约、真实代码、测试质量、快照和执行记录决定通过、返工或阻塞。
默认最多四轮实施（首轮加三次返工），总墙钟四小时；单个Pi或Codex进程默认上限7200秒（两小时）。
两个阶段、检查和返工共享总墙钟；单次上限不是保证可用时长。连续两轮保留相同问题集合时暂停。
传输失败、输出截断、缺少交付、越界改动和不可信验收结果进入 `blocked`，不会误报通过。
Pi 自身可按其重试策略处理瞬时服务错误；调度器不盲目重发可能已经执行过的 prompt。

```bash
.venv/bin/codinator pause demo-add
.venv/bin/codinator resume demo-add
.venv/bin/codinator run demo-add
# 墙钟预算含暂停时间；需要延长时显式授权：
.venv/bin/codinator resume demo-add --extra-seconds 3600
.venv/bin/codinator cancel demo-add
```

普通 `resume` 会创建新 attempt，从 Pi 开始重新走完整流程；旧记录不覆盖。

旧任务保留发布时的显式预算，不随新版默认值变化。对已阻塞／暂停任务，可明确覆盖之后每个Pi／Codex进程的上限：

```bash
.venv/bin/codinator resume TASK_ID --attempt-seconds 7200 --extra-seconds 14400
```

这给本任务设置7200秒单次上限，并延长总截止时间14400秒；若旧截止时间已过，则从恢复时起给四小时。
两个阶段及检查仍共享总时间，检查自己的超时上限不变。只想改单次上限且总时间仍够时可省略 `--extra-seconds`。
该覆盖值可与 `--review-only` 合用；省略 `--attempt-seconds` 保留此前覆盖，不能绕过仅审计恢复的资格检查。
预算变更随恢复状态原子记入SQLite事件，新attempt的 `budget.json` 保存生效来源、原发布值和总截止时间；
原manifest、工作树交接和旧attempt原件不改写。`status` 显示当前生效的单次上限。
升级后先重启空闲后台服务再恢复任务，避免旧服务仍按发布值运行。

若 Pi 已完整交付、所有必需检查通过，仅 Codex 因额度／连接／进程中断而未完成审计，可显式只恢复审计：

```bash
.venv/bin/codinator resume TASK_ID --review-only --extra-seconds 3600
.venv/bin/codinator status TASK_ID
```

这里额外授权3600秒墙钟预算；仍有足够余额时可省略 `--extra-seconds`。后台服务启用时恢复命令即入队，
不要同时再执行 `run`；前台模式则按上面的代理示例运行 `run TASK_ID`。

`--review-only` 进入独立的 `review_ready` 状态：核对未变的提交、Pi 成功协议／交付、原检查命令及输出哈希，
只创建新的 Codex 审计 attempt。不会启动 Pi 或重跑已通过的检查，实施轮次不增加。新 `review-source.json`
引用原实施证据，原始日志保留；运行前后都核对冻结提交与证据。再次遇到基础设施中断仍暂停，须显式恢复。
若新审计要求返工，则自动进入下一实施轮，重新运行 Pi 与必需检查。

缺少／损坏证据、工作区改变、检查失败、未完成的 Pi，均不能只恢复审计。已有 verdict 或 outcome 的任务
需要先检查结论，不能用此选项绕过返工／阻塞意见；这是保守限制，也包括 verdict 已落盘而控制器尚未接收的崩溃。
旧数据库自动迁移，符合条件的旧任务可恢复；部署此版本后先重启空闲后台服务。
旧服务不会识别 `review_ready`，因此不会误将它交给 Pi，但也不会处理该队列，需重启加载新代码。

控制器崩溃后先 `recover` 并检查状态，再显式选择普通或仅审计 `resume`。
若工作树偏离已记录快照，应人工检查并恢复，或发布新任务；不会自动 reset、stash 或回滚。
任务通过后停在 accepted，不自动 commit、push、合并或开始下一项研究设计。

## 任务契约

`handoff` 是只读契约，不允许出现在 `allowed_paths` 内。执行者写独立的本轮汇总，
SQLite 是状态的唯一来源；本版不自动改写项目 README／交接文档中的状态。
接入已有 KMesh 任务前，应由 Codex 修订旧的“Pi 修改状态和报告路径”约定及范围检查器；
不能让 Pi 绕过旧冻结清单。Codinator 本身不修改 KMesh。

- `allowed_paths`：精确文件，或以 `/` 结尾的目录；不支持通配符，不允许 `.git`／`.codex`／`.agents`。
- `checks[].argv`：参数数组，直接启动进程，不经过隐式 shell。检查在只读工作树中运行；临时产物写 `/tmp`。
- `excludes`：不纳入内容快照的环境／缓存路径；不能覆盖 tracked 文件，也不能与允许修改范围相交。
- `max_rounds`、`max_seconds`、`attempt_seconds`：发布前确定的执行边界。
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
    before.json / submission.json     文件内容、类型、模式、Git HEAD/index
    diff.json                         本轮改动范围与快照摘要
    pi-runtime.json                   RPC 报告的实际客户端模型身份
    pi/                               原始 RPC、stderr、进程与退出记录
    delivery/summary.md                Pi 的改动、偏差、未运行项
    delivery/completion.json           本轮提交标识
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
Codex 也有外层进程隔离，工作树只读，CLI 内层显式 `--sandbox read-only`、`-a never`。
两种 agent 都启用父进程退出清理与 PID namespace；没有不受限运行的自动降级。
这属于单用户工程隔离，不是对恶意同 UID 主机进程的安全防线。

## 后台服务与通知

```bash
mkdir -p ~/.config/systemd/user
.venv/bin/codinator service > ~/.config/systemd/user/codinator.service
systemctl --user daemon-reload
systemctl --user enable --now codinator.service
systemctl --user status codinator.service
journalctl --user -u codinator.service
```

服务只执行明确发布到默认状态目录的任务。不同状态目录需生成对应 service 参数。
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
本机首次安装的验证结果与待续事项见 [验证记录](validation/README.md)。
项目的后续运行中，完整 state 目录可包含认证和原始工具输出，分享前只选所需脱敏证据。

接口依据：[Pi RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)、
[Codex 非交互执行](https://learn.chatgpt.com/docs/non-interactive-mode)。当前适配 Pi 0.86.1、Codex 0.154.0；升级后应复跑协议与真实联调。
