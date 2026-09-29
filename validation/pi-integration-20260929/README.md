# 验收后的 Pi 提交与合并

新后台任务通过显式 `integration` manifest 授权该阶段。无此字段的历史任务和原生 Pi 不改变。
独立审查接受后，Pi 在私有 clone 创建带正文的单一提交并快进合并；控制器在只读沙箱中
验证完整 tree、父提交、协议与证据，再从 bundle 将同一提交快进到真实目标。不 push。

- [非作者审查](review.json)：accepted；关闭不可信 Git 配置的宿主执行、取消竞态和 intake 未绑定三个发现，包含审阅文件 SHA-256。
- `full-r2.stderr`：139 项 Python 测试通过。新增真实 Git／假模型故障测试覆盖候选错误、暂停／取消、超时、证据修改、HEAD／暂存冲突、实施返工、检查点崩溃与合并后恢复。
- `extension-r2.stdout`：主机上 14 项原生 Pi 扩展回归通过。
- `full-r1.*` 保留首次失败：旧 CLI mock 未提供新状态字段，以及工具沙箱拒绝两个 Unix socket 测试。修正 mock 后在主机重新验证，不覆盖旧输出。
- [真实模型摘要](live-summary.json)、[结果](live-result.json)：最终代码的真实 Pi 实施 → 独立 Codex 接受 → Pi 提交／合并成功；指定模型、原工作树冻结和主仓库未提交规划保留均核验。私有认证／完整会话在仓外。
- [部署记录](deployment.json)：精确文件 hash、测试结果、服务状态及回滚备份位置；源码尚未 commit/push。

假模型测试不当作真实模型证据；Pi 身份为 RPC 客户端元数据，不证明后端权重。
真实探针是独立的临时 Git 仓库，不修改 KMesh。T0020 的实际状态另由其控制器记录。
