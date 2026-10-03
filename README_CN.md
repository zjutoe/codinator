# Codinator

Codinator 为主 Codex、Pi + Bonsai 和独立 Codex 提供任务交接协议与后台通信渠道。
主 Codex 负责需求、拆解、handoff 和纠偏；Pi 负责实施、Git 提交与测试；独立 Codex 负责验证和验收。
**Codinator 不实施业务任务，不运行项目测试，不调用 Git。** 它派发模型进程，传递原始要求和反馈，保存交接证据，控制状态、预算和串行执行。

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

## 预算与软检查

默认最多四轮、总墙钟四小时、每个 Pi／Codex 进程两小时；实际以已发布任务授权为准。
Pi 的实施／测试、独立 Codex 的复测／验收与返工共享总预算，暂停也计入墙钟。
`checks[].timeout_seconds` 由执行该检查的 Pi／Codex 遵守；控制器实施整个模型进程的硬上限。

`checkpoint_seconds: 1800` 与 `attempt_seconds: 5400` 表示每30分钟软检查、每个模型进程90分钟硬上限。
软检查通过 Pi RPC steer 请求已完成事项、实际检查、阻塞、下一步及 `needs_guidance`，不重启会话。
五分钟内无合法报告或 `needs_guidance=true` 时，控制器在无活动工具的安全边界阻断；长工具仍受硬上限约束。
正常报告可继续工作；已锁定违规不能被迟到报告撤销。最后一个尚在宽限内的请求可由合法正式交付替代，留下明确 resolution。
主 Codex 据证据纠偏，控制器不自动生成指导或启动 planner。

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

实施沙箱将既有 Git HEAD、配置、hooks 和 packed-refs 重新挂载只读；普通提交仍可写 index、objects 和任务 ref。其他 ref 的不变性由 Pi 的起始 `git show-ref` 原始工具记录与独立 Codex 核对，不宣称所有 Git 元数据均有操作系统级写保护。
