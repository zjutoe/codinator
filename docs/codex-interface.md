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
