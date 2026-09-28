# 2026-09-28：连续相同问题的返工与旧任务恢复

修复前，控制器要求 reviewer 使用稳定 issue ID，却在两轮 ID 集合相同后停止返工。T0019 第二轮已有部分修复，仍触发该规则；交互恢复入口又未处理这一 `blocked / feedback` 状态。

本次交互和后台循环均移除 ID 集合停止条件，保留原 `max_rounds`。交互入口允许显式恢复旧版这一特定阻塞：校验快照、检查证据和正式意见后进入下一轮，不重放旧审查、不改旧 attempt、不增加预算。其他阻塞继续拒绝，暂停后恢复不重复计轮。

验证结果：

- 两个新增回归在旧产品上重现原故障：第二轮提前 blocked、legacy resume 被拒；原始输出在本次 Codex 工具记录中。
- 修复后定向 24 项通过。首次沙箱运行只有 Unix socket bind 被环境拒绝；按本机权限重跑后全通过。
- Python 完整回归 **90 项通过，118.464 秒，退出 0**，原始输出见 [full.stderr](full.stderr)／[full.stdout](full.stdout)。
- Pi 扩展 **9 项通过**，包括显式恢复后只派发一次返工并携带原 review；直接运行 `node tests/pi_extension.test.mjs` 核对了九个子测试。
- 按 AGENTS 要求由非作者代理独立审查，结论通过、无遗留发现；额外核验检查失败来源的旧阻塞，以及 review-only retry 后沿用原检查证据的旧阻塞，2/2 通过。
- 用真实 T0019 SQLite／attempt 的私有副本演练恢复：`blocked / round 2 / attempt 2` → `needs_changes / round 3 / attempt 2`；69 个历史证据文件不变，状态事件在副本中落盘，真实任务记录未修改。[演练摘要](T0019-resume-rehearsal.json)

命令、受测改动 SHA-256、独立审查摘要与限制见 [summary.json](summary.json)。这些是本地假 agent／状态演练验证，不是新一轮真实模型联调或 T0019 验收。旧日志、契约、数据库原件和认证材料未纳入本提交目录。

更新后需退出旧原生 Pi／控制器，再通过 `codinator pi TASK_ID --resume` 加载新恢复逻辑。T0019 可沿用 KMesh 的 `reports/T0019/launch-pi.sh --resume`，无需修改已发布 manifest。
