# Codex 主界面与后台执行

日常从原生 Codex 界面管理任务，调用 Codinator CLI 即可；不需要嵌入 Codex TUI、接入 App Server 或增加 MCP 服务。Codex 主会话负责交接、明确发布、读取状态和解释异常；后台 `serve` 持有进程与持久状态，Pi + Bonsai 实施，独立 Codex 进程审查固定候选提交（旧 v1 为冻结快照）。后台自动返工不依赖主会话继续对话或接收通知。

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

独立 user service 启用后，关闭 Codex 窗口不会停止任务。机器关机、服务退出会中断执行；下次控制器启动保存中断状态，等待检查后显式恢复，不重放不确定的旧提示。注销系统后继续运行取决于用户服务与 linger 设置。后台任务应由独立用户服务持有。

## 在 Codex 中操作

交接、任务分支与基线 SHA、模型、允许路径、检查和预算全部确定后，才发布一个 v2 任务。
普通仓库的独立分支即可，不要求 worktree；工作目录须干净，契约和忽略规则提前提交。
见 [Git 分支与源码检查点](git-checkpoints.md)。已发布 v1 任务保留原协议，不接受新的 v1 发布：

```bash
codinator submit /absolute/path/to/task.json
codinator status TASK_ID
```

服务自动处理已发布任务，正常 `needs_changes` 在剩余轮次内自动交回 Pi。主会话通过管理命令操作任务，不直接改执行中的工作树或数据库。主会话可以查看证据、解释反馈，并按已有授权决定显式恢复；改变范围、预算或研究语义仍需遵循任务契约与用户授权。独立审查进程固定使用原模型和原验收条件，主会话不能替它宣布通过。

需要精确查看过程时，先从 `status` 的 `attempt_evidence` 找到当轮目录，再按 `delivery-selection.json` 找到选定的 `summary.md`，并按需读取当轮的 `pi/stdout.jsonl`、`checks/*/result.json`、`codex/result.json`。发生交付修复时，被检查的汇总位于 `delivery-repair/delivery/summary.md`；旧协议没有选择收据的历史 attempt 使用 `delivery/summary.md`。JSONL 是原始事件，不能将任意模型文字当成控制器结论。原始认证、配置和会话不应复制到聊天或版本库。

状态查询是只读操作，不创建数据库、不迁移旧 schema、不恢复任务，也不启动 Pi。JSON 提供以下字段：

| 字段 | 含义 |
| --- | --- |
| `state` / `phase` / `reason` | 控制器记录的当前状态、阶段及原因 |
| `round` / `attempt` | 当前实施轮次与最近分配的 attempt；两者不一定相同 |
| `control` | 尚待执行中的进程响应的暂停或取消请求 |
| `workspace` | 任务仓库工作目录 |
| `git` | v2 的任务分支、发布基线及最新检查点 SHA |
| `evidence` / `attempt_evidence` | 任务与最近 attempt 的证据位置；未开始时后者为空 |
| `latest_review` | 最近落盘的 outcome、其 attempt、摘要和 Markdown 路径；包括控制器检查返工，可能属于历史轮次 |
| `process_alive` | 已记录的检查／agent 子进程 PID 和启动标识是否仍匹配；不代表服务健康 |
| `review_accepted` / `integration` | 显式授权集成任务的独立审查接受标识、集成 attempt、候选提交及最终 merged_commit；旧任务无此字段 |
| `next_action` | 提示查看、继续监控或检查后恢复；不是自动执行或扩大授权 |

例如当前 `implementing / round 3 / attempt 3`、`latest_review.attempt=2` 表示正在按第二次审查修改，不是第三次审查已返回。即使磁盘中有 `accepted` outcome，控制器仍为 `blocked`，也必须核对中断现场，不能根据历史文件改写状态。损坏或链接的 outcome 会使查询明确报错。

`status` 不判断 systemd 健康；任务一直 `ready` 时检查 `systemctl --user is-active codinator.service` 与 `journalctl --user -u codinator.service -n 50 --no-pager`。未创建任务库或状态目录错误时，查询报错且不会静默新建一个空库。服务启动后尚未提交任务，查询返回空数组。

## 实施交付与一次自动修复

每次实施派发都由控制器生成只读的 `delivery-contract.json` 和绑定该契约的 `submit-delivery.py`。
契约固定任务编号、实施轮次、attempt 和绝对交付目录。Pi 收尾时重新读取契约，在 `/tmp` 写好
汇总，再执行提示中的绑定命令，仅传 `--summary` 和 `--status awaiting_review|blocked`。
任务身份和目录没有可覆盖的命令行参数；不手写 `completion.json`，也不在工作树新建 `delivery/`。
契约位置与绑定命令同时通过 Pi 的 `--append-system-prompt` 保留在系统提示中，
不依赖会话压缩摘要记住路径；上下文压缩后仍应读取契约恢复这些约束。

提交工具与控制器共用校验器：汇总须为非空 UTF-8，两个文件须为普通文件、无链接祖先且各不超过
1,000,000 字节；completion 只允许 `task_id`、`round`、`attempt`、`status` 四个字段。
轮次和 attempt 必须为整数，布尔值不能替代；重复 JSON 键、额外字段和身份错误均会明确拒绝。
工具先原子发布汇总，再发布 completion。相同提交可重复执行；已有内容冲突或无效时拒绝覆盖。
仅汇总已发布而 completion 尚未发布的中断，可用相同汇总重试完成。JSON 回执中的 `submitted`
只表示提交成功，之后仍需控制器检查与独立审查，不能据此宣称任务已接受。

若 Pi 进程及 RPC 已确认正常结束，缺文件、空汇总或 JSON 格式／身份错误会记录到
`delivery-error.json`，逐项给出错误路径、预期值、实际值及是否允许修复。控制器先保存原始现场、
核对修改范围并固定候选提交（旧 v1 为冻结快照），才允许在当前 attempt 中启动**最多一次**仅修复交付的 Pi 进程。
此时状态为 `checking`，阶段为 `delivery-repair`；时间上限为 300 秒、单进程上限和总剩余预算
三者的最小值，不增加实施轮次，也不延长总截止时间。

修复进程的工作树和原 attempt 证据只读，交付输出只写新的 `delivery-repair/delivery/`；
临时文件仍使用 `/tmp`，运行时配置与会话使用独立私有目录。它只能据原证据整理汇总并提交，
不能修改源代码、重跑审计或检查、安装依赖或改动原交付。控制器再次核对候选提交和新交付，
修复成功后继续正常必需检查与独立审查。原坏交付始终保留，不自动从工作树其他目录搜索或迁移文件。

明确报告 `blocked`（包括能识别为 blocked 的错误格式）、链接、非普通文件、超大或超深 JSON、越界修改、
进程失败和 RPC 输出截断都不进入自动修复。修复再次失败或报告 blocked 时，任务仍阻塞；
不能通过反复修复把研究缺陷改写为已完成，也不重放执行结果不确定的提示。

控制器用不可覆盖的 `delivery-selection.json` 记录最终选用的原交付或修复交付，保存两个文件的
内容哈希、大小和模式。检查、审查和仅审查恢复均使用该选择，文件身份变化会拒绝继续。
新协议缺少选择收据会明确报错；仅旧协议中已通过检查的历史证据可沿用原交付目录。
仅审查恢复还会核对原 Pi 和修复 Pi 的成功退出、RPC 完成及模型身份，不重新运行交付修复。
模拟测试与真实模型联调证据应分别记录，不能用本地测试推断上下文压缩后的真实模型行为已验证。

## 暂停与恢复

```bash
codinator pause TASK_ID
codinator status TASK_ID
codinator resume TASK_ID
```

暂停是明确操作；阅读日志、询问进度、退出主界面不等于暂停。运行中的进程停止需要时间，以控制器最终状态为准。恢复前阅读 `reason` 和证据，核对工作树与进程；普通恢复安排新实施 attempt，旧证据保留，不是向旧进程重发提示。

已完整交付（包括上述一次修复后选定的交付）、检查通过，仅独立审查因基础设施中断时，可使用 `codinator resume TASK_ID --review-only`；控制器验证旧提交及检查证据后只运行新审查。存在未协调的 verdict、证据损坏、提交或工作目录变化（旧 v1 为快照变化）、未完成实施会拒绝此操作。额度耗尽时不要把反复 resume 当成修复。

后台总时间仍包括暂停时间。确需额外预算且已获授权时，使用 `--extra-seconds N` 或 `--attempt-seconds N` 明确记录，不改旧 manifest。主会话无需逐工具或逐轮调用模型监控；按用户查询读取状态，或使用明确配置的 `notify_thread` 接收终态通知即可。通知失败不改变结果，也不阻止后台返工。

## 验收后由 Pi 提交并合并

本节仅适用于升级前已发布的 v1 integration 契约。v2 在检查前已有本地 Git 提交，
验收后保留在任务分支，后续合并须单独授权；v2 不接受此 legacy integration 配置。

用户授权后，在新任务 manifest 中增加以下字段；没有此字段的旧任务行为保持不变。
这是发布契约的一部分，不能给已冻结任务静默补授权。`target_workspace` 必须是同一
仓库的另一工作树，发布时源和目标都处于 `base_commit`，目标检出 `target_branch`。

```json
"integration": {
  "target_workspace": "/absolute/path/to/main-repository",
  "target_branch": "master",
  "base_commit": "完整的基线 commit hash",
  "planning_paths": ["AGENTS.md", "docs/", "reports/"]
}
```

`planning_paths` 仅声明发布前已有的未提交规划文件，不授予实施写权限，也不进入产品提交；
不能与 `allowed_paths` 重叠。发布时索引须无暂存更改，其他未提交基线必须属于此清单。
不支持 Git filters、include/worktree config 覆盖或改变内容的 checkout 转换。

流程为 `reviewing → integration_ready → integrating → accepted`。独立审查接受后先固定其
证据与提交快照，再建立无共享对象的私有 clone，将被接受的文件差异按原字节和执行位放入。
新的 Pi + `bonsai2-27b/xhigh` 进程负责检查 diff、暂存确切文件、创建一个带说明正文的提交，
切换到目标分支并 `merge --ff-only`。Pi 不能写原工作树、原 Git 或控制器证据。
控制器在沙箱中核验单一父提交、完整 tree、干净状态、交付和完成协议，导出 bundle，
再用固定 Git 命令把**同一个提交**快进到真实目标。没有 push、自动冲突解决、rebase 或强制更新。

只合入从发布基线到接受快照的产品变更。目标中无冲突的规划改动保留；目标 HEAD 移动、
暂存更改或与待合并路径重叠的未提交内容都会阻塞，不修改原主分支来强行完成。
原任务工作树仍保持验收时 HEAD/index/文件快照，最终 commit 单独记录为 `integration.merged_commit`。

`integration-NNNN/` 留存 Pi 原始协议、交付、私有 Git 副本、候选 bundle 与推广意图／结果。
集成中断不会抹去 `review_accepted`；检查原因后普通 `resume` 只恢复集成，不重跑实施或验收。
未完成的 Pi 协议不能被推定成功；显式恢复使用全新集成 attempt。已有完整候选时恢复只继续
核验与推广；合并后控制器中断则用落盘意图核对既成提交，不重复提交或合并。
集成受原任务总截止时间与单进程预算约束，暂停、取消与最终合并串行处理。

## Python 缓存与故障证据

v2 使用发布前固定的 `.gitignore`，缓存不进入源码检查点，不因创建缓存失配，也不自动清理。
改变忽略规则、未忽略的越界修改仍会阻塞。以下归档流程只描述既有 v1 任务：

Pi 实施进程将默认 Python 缓存目录放在仓外。`python -I` 会忽略环境变量，子进程仍需显式 `-B`。后台 Pi 成功结束进程及 RPC 协议后，先保存 `implementation.json` 和内容 blobs，再严格识别并归档新增字节码，最后冻结 `submission.json`。需要的交付修复及后续检查、审查均使用该冻结快照。

只处理新增、非链接、匹配控制器 Python tag/magic 且对应源码存在的缓存；有其他越界时全部拒绝。原文件先留证再移入唯一 `cache-cleanup-*/removed/`，跨文件系统或中途失败不回退到删除。失败记录及旧 attempt 保留。检查／审查期间不清理缓存，冻结快照变化会阻塞。

后台的中断恢复仍要求匹配已记录快照；不会自动采用暂停期间的外部改动，也不会对未完成协议的旧实施进行猜测清理。归档失败、协议中断且产生越界或冻结文件变化时，应检查现场后处理，不承诺自动恢复所有错误。
