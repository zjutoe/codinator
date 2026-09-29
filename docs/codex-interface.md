# Codex 主界面与后台执行

日常从原生 Codex 界面管理任务，调用 Codinator CLI 即可；不需要嵌入 Codex TUI、接入 App Server 或增加 MCP 服务。Codex 主会话负责交接、明确发布、读取状态和解释异常；后台 `serve` 持有进程与持久状态，Pi + Bonsai 实施，独立 Codex 进程审查冻结快照。后台自动返工不依赖主会话继续对话或接收通知。

原生 Pi 是可选的直接交互入口，适合调试实施过程。它运行自己的实施会话，不是后台任务的只读观察窗口。新任务在发布前选定模式；已发布任务继续使用原来的状态目录、证据和恢复入口。本版不提供中途切换、接管、搬迁或同时开启两个实施者的功能。

## 一次性准备独立服务

先安装 Codinator 并运行 `codinator doctor`。该检查只验证本地依赖和沙箱，不证明模型连通。用实际安装路径替换下面的 `codinator`；需要代理时在生成 unit 的命令上设置代理环境。Pi 仍直连本机 Bonsai。

```bash
mkdir -p ~/.config/systemd/user
codinator service > ~/.config/systemd/user/codinator.service
systemctl --user daemon-reload
systemctl --user enable --now codinator.service
systemctl --user is-active codinator.service
```

`service` 只输出 unit，不自动安装、启用或重启。已有 unit 时先核对内容和正在执行的任务，不能覆盖运行中的配置后立即重启。自定义状态目录时统一使用 `codinator --state-dir /absolute/state ...`，查询、发布、恢复和服务必须指向同一个根目录。

独立 user service 启用后，关闭 Codex 窗口不会停止任务。机器关机、服务退出会中断执行；下次控制器启动保存中断状态，等待检查后显式恢复，不重放不确定的旧提示。注销系统后继续运行取决于用户服务与 linger 设置。不要用 Codex 工具中的长期 `run` 或临时 shell 后台进程代替独立服务，再声称它不依赖前台生命周期。

## 在 Codex 中操作

交接、独立工作树、模型、允许路径、检查和预算全部确定后，才发布一个任务：

```bash
codinator submit /absolute/path/to/task.json
codinator status TASK_ID
```

服务自动处理已发布任务，正常 `needs_changes` 在剩余轮次内自动交回 Pi。不要额外启动 `run`；不要让主会话直接改任务工作树或数据库。主会话可以查看证据、解释反馈，并按已有授权决定显式恢复；改变范围、预算或研究语义仍需遵循任务契约与用户授权。独立审查进程固定使用原模型和原验收条件，主会话不能替它宣布通过。

需要精确查看过程时，先从 `status` 的 `attempt_evidence` 找到当轮目录，再按需读取 `delivery/summary.md`、`pi/stdout.jsonl`、`checks/*/result.json`、`codex/result.json`。JSONL 是原始事件，不能将任意模型文字当成控制器结论。原始认证、配置和会话不应复制到聊天或版本库。

状态查询是只读操作，不创建数据库、不迁移旧 schema、不恢复任务，也不启动 Pi。JSON 保留原有字段，并提供：

| 字段 | 含义 |
| --- | --- |
| `state` / `phase` / `reason` | 控制器记录的当前状态、阶段及原因 |
| `round` / `attempt` | 当前实施轮次与最近分配的 attempt；两者不一定相同 |
| `control` | 尚待执行中的进程响应的暂停或取消请求 |
| `mode` / `workspace` | 所查询的模式与工作树 |
| `evidence` / `attempt_evidence` | 任务与最近 attempt 的证据位置；未开始时后者为空 |
| `latest_review` | 最近落盘的 outcome、其 attempt、摘要和 Markdown 路径；包括控制器检查返工，可能属于历史轮次 |
| `process_alive` | 已记录的检查／agent 子进程 PID 和启动标识是否仍匹配；不代表服务健康或 Pi TUI 存活 |
| `next_action` | 提示查看、继续监控或检查后恢复；不是自动执行或扩大授权 |

例如当前 `implementing / round 3 / attempt 3`、`latest_review.attempt=2` 表示正在按第二次审查修改，不是第三次审查已返回。即使磁盘中有 `accepted` outcome，控制器仍为 `blocked`，也必须核对中断现场，不能根据历史文件改写状态。损坏或链接的 outcome 会使查询明确报错。

`status` 不判断 systemd 健康；任务一直 `ready` 时检查 `systemctl --user is-active codinator.service` 与 `journalctl --user -u codinator.service -n 50 --no-pager`。未创建任务库、状态目录错误或选错模式时，查询报错且不会静默新建一个空库。服务启动后尚未提交任务，查询返回空数组。

## 暂停与恢复

```bash
codinator pause TASK_ID
codinator status TASK_ID
codinator resume TASK_ID
```

暂停是明确操作；阅读日志、询问进度、退出主界面不等于暂停。运行中的进程停止需要时间，以控制器最终状态为准。恢复前阅读 `reason` 和证据，核对工作树与进程；普通恢复安排新实施 attempt，旧证据保留，不是向旧进程重发提示。

已完整交付、检查通过，仅独立审查因基础设施中断时，可使用 `codinator resume TASK_ID --review-only`；控制器验证旧提交及检查证据后只运行新审查。存在未协调的 verdict、证据损坏、快照变化或未完成实施会拒绝此操作。额度耗尽时不要把反复 resume 当成修复。

后台总时间仍包括暂停时间。确需额外预算且已获授权时，使用 `--extra-seconds N` 或 `--attempt-seconds N` 明确记录，不改旧 manifest。主会话无需逐工具或逐轮调用模型监控；按用户查询读取状态，或使用明确配置的 `notify_thread` 接收终态通知即可。通知失败不改变结果，也不阻止后台返工。

## Python 缓存与故障证据

后台 Pi 与原生 Pi 都将默认 Python 缓存目录放在仓外。`python -I` 会忽略环境变量，子进程仍需显式 `-B`。后台 Pi 成功结束协议并完整交付后，先保存 `implementation.json` 和内容 blobs，再复用严格的新字节码识别与归档逻辑，最后冻结 `submission.json` 供检查和审查。

只处理新增、非链接、匹配控制器 Python tag/magic 且对应源码存在的缓存；有其他越界时全部拒绝。原文件先留证再移入唯一 `cache-cleanup-*/removed/`，跨文件系统或中途失败不回退到删除。失败记录及旧 attempt 保留。检查／审查期间不清理缓存，冻结快照变化会阻塞。

后台的中断恢复仍要求匹配已记录快照；不会自动采用暂停期间的外部改动，也不会对未完成协议的旧实施进行猜测清理。此处与原生 Pi 的显式实施恢复不同。归档失败、协议中断且产生越界或冻结文件变化时，应检查现场后处理，不承诺自动恢复所有错误。

## 查看原生 Pi 的现有任务

```bash
codinator status TASK_ID --mode pi
```

从同一状态根目录下的 `interactive/` 只读查询，不打开 Pi、不冻结或恢复任务。需要继续实施时仍使用原入口 `codinator pi TASK_ID --resume`；原生 Pi 中输入新消息、退出或中止仍有原来的暂停语义。不能把 `interactive/` 当作后台 `serve` 的状态目录，不能把同一工作树重新发布到另一模式来绕过冻结或旧证据。

实现依据：独立审查沿用官方 [Codex 非交互执行](https://learn.chatgpt.com/docs/non-interactive-mode) 的 `exec --json` 与结构化结果；本方案不需要官方 [App Server](https://learn.chatgpt.com/docs/app-server) 提供的深度界面集成。
