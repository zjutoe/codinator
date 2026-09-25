# Codidator

让 Codex 制订任务、Pi + Bonsai 实施、Codex 独立验收，并自动安排返工。
你发布一份明确的任务契约，程序负责派发、留证、冻结、验收和通知。

首版面向 Linux、单用户、串行任务；Python 3.11+，Python 运行时无第三方依赖。
需要已安装并完成认证的 `pi`、`codex`、`git`、`bwrap`。

## 快速开始

```bash
cd ~/src/llm/codidator
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/codidator doctor
```

复制 [examples/task.json](examples/task.json)，将 workspace、handoff、允许路径和检查命令改为真实值。
可用 [示例交接文档](examples/demo-handoff.md) 在空的独立仓库里先试跑。
workspace 必须是独立 Git 仓库／worktree 的根目录；可以有未提交改动，发布时会整体留存。
不要把正在人工编辑的工作树直接投入自动运行。

```bash
.venv/bin/codidator submit /absolute/path/to/task.json
.venv/bin/codidator run demo-add
.venv/bin/codidator status demo-add
```

`submit` 是明确派发授权：程序不会扫描项目里所有 `ready` 文档自行开工。
`run` 在当前终端执行完整闭环；`serve` 在后台串行处理已发布任务。
后台服务已启用时，`submit` 后直接用 `status` 查看结果，不再并行执行 `run`。
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
默认最多四轮实施（首轮加三次返工），总墙钟四小时；连续两轮保留相同问题集合时暂停。
传输失败、输出截断、缺少交付、越界改动和不可信验收结果进入 `blocked`，不会误报通过。
Pi 自身可按其重试策略处理瞬时服务错误；调度器不盲目重发可能已经执行过的 prompt。

```bash
.venv/bin/codidator pause demo-add
.venv/bin/codidator resume demo-add
.venv/bin/codidator run demo-add
# 墙钟预算含暂停时间；需要延长时显式授权：
.venv/bin/codidator resume demo-add --extra-seconds 3600
.venv/bin/codidator cancel demo-add
```

恢复会创建新 attempt，旧记录不重用。控制器崩溃后先 `recover` 并检查状态，再显式 `resume`。
若工作树偏离已记录快照，应人工检查并恢复，或发布新任务；不会自动 reset、stash 或回滚。
任务通过后停在 accepted，不自动 commit、push、合并或开始下一项研究设计。

## 任务契约

`handoff` 是只读契约，不允许出现在 `allowed_paths` 内。执行者写独立的本轮汇总，
SQLite 是状态的唯一来源；本版不自动改写项目 README／交接文档中的状态。
接入已有 KMesh 任务前，应由 Codex 修订旧的“Pi 修改状态和报告路径”约定及范围检查器；
不能让 Pi 绕过旧冻结清单。Codidator 本身不修改 KMesh。

- `allowed_paths`：精确文件，或以 `/` 结尾的目录；不支持通配符，不允许 `.git`／`.codex`／`.agents`。
- `checks[].argv`：参数数组，直接启动进程，不经过隐式 shell。检查在只读工作树中运行；临时产物写 `/tmp`。
- `excludes`：不纳入内容快照的环境／缓存路径；不能覆盖 tracked 文件，也不能与允许修改范围相交。
- `max_rounds`、`max_seconds`、`attempt_seconds`：发布前确定的执行边界。
- `notify_thread`：可选，用户明确指定的 Codex 会话 ID。未设置时保留本地通知，不向外发送。

新任务需要单独 worktree 时先人工／Codex 创建，明确环境与基线，再发布。
Codidator 不自动创建工作树或搬迁虚拟环境，避免隐式丢失未提交文件及破坏绝对路径契约。

## 证据与权限

默认状态目录 `~/.local/state/codidator`；可用 `--state-dir` 或 `CODIDATOR_STATE_DIR` 指定。
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
.venv/bin/codidator service > ~/.config/systemd/user/codidator.service
systemctl --user daemon-reload
systemctl --user enable --now codidator.service
systemctl --user status codidator.service
journalctl --user -u codidator.service
```

服务只执行明确发布到默认状态目录的任务。不同状态目录需生成对应 service 参数。
生成的 unit 保存当时的 PATH 和代理环境，确保后台 Codex 仍经代理；Pi 的直连规则不变。
注销后继续运行取决于本机 user service／linger 设置；程序不修改系统级策略。

配置 `notify_thread` 后，以 `codex queue` 排队发送通过／阻塞通知。SQLite 终态与通知入队
在同一事务中完成。发送失败不会回滚验收；`codidator notify` 可重试。
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
