# 2026-09-29：Codex 主界面与后台执行

> 历史验证记录，仅对应当时版本；当前操作以 [项目说明](../../README.md) 为准。

基线：`77b9b732cee381b80ca31580c8fc1e95afeddb13`。

原生 Codex 通过已有 CLI 管理独立后台服务；Pi + Bonsai 实施，独立 Codex 审查，原生 Pi 保留可选。未添加 App Server、MCP 或第二套调度器，未迁移已发布任务。

- `status` 只读打开 SQLite，不初始化或迁移库、不启动模型。增加模式、工作树、证据位置、控制请求、存活进程和带 attempt 标识的最近 outcome；原生 Pi 使用 `--mode pi`。
- 后台完整交付先保存 `implementation.json` 和 blobs，再归档严格识别的新 Python 缓存，最后冻结提交；保留所有失败现场，冻结审查不清理。
- 前台 pause/cancel、分配 attempt 和发布结果通过 SQLite 事务协调，避免晚到结果覆盖已提交的暂停或产生 `accepted + pending pause`。
- 推荐操作、服务配置和严格恢复限制见 [主界面协议](../../docs/codex-interface.md)。服务 unit 已准备并通过 `systemd-analyze --user verify`；未安装或启动本机服务。

验证结果：

- Python 完整 **122/122**，145.053 秒，零失败；[原始输出](full.stderr)、[运行元数据](full-result.json)。主机沙箱外运行的仅为本地测试，使用临时目录及假 agent。
- Pi 扩展 **14/14**，零跳过、失败；[原始输出](pi-extension.stdout)、[运行元数据](pi-extension-result.json)。
- 跨进程测试确认：前台查询进程退出，后台继续完成 `needs_changes → 实施 → accepted`，两轮证据保留；查询不写暂停请求。
- 非作者审查 accepted。初审发现 control 检查与结果发布之间的竞态，已事务化修复，独立并发探针验证两种顺序。另核验只读 WAL 查询及第二次缓存移动故障的证据完整性。
- 在真实 T0019 上仅调用新只读查询：`accepted / round 3 / attempt 3`，最近 review 属于 attempt 3，数据库 SHA256 未变；没有运行或恢复该任务。

所有自动测试与独立故障探针均不调用真实模型，不能作为模型连通性证据。后台归档失败或未完成协议的实施仍需检查现场并满足严格 checkpoint 才能恢复。新入口没有中途 attach／模式迁移能力。

代码 hash、审查摘要及服务校验信息见 [summary.json](summary.json)。
