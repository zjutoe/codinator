# 验证方法与历史记录

当前由 Codex 主界面管理后台服务。控制器负责派发、协议检查、预算和证据传递；
Pi／Codex 执行 Git 和项目检查，独立 Codex 验收。源码版本由 Git commit SHA 固定。使用方法见 [项目说明](../README.md) 和 [主界面协议](../docs/codex-interface.md)。

## 当前验证方法

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
python3 -B validation/native_sandbox.py
```

单元与流程测试使用明确标识的假 agent，不调用真实模型。覆盖自动返工、范围与证据校验、
仅审查恢复、暂停／取消竞态、进程故障、服务重启及旧 attempt 保留。
`native_sandbox.py` 只验证真实 bubblewrap 的文件权限边界，不调用模型。
沙箱不可用时应明确报告失败，不改成无沙箱运行。

`live_smoke.py` 使用 v2 契约，在新的独立普通 checkout 中准备分支、已提交的只读
handoff 和测试 oracle。默认只准备夹具，不调用模型。显式 `--run` 才调用真实
Pi + Bonsai 和独立 Codex；总预算 40 分钟、最多两轮、单次 20 分钟，观察间隔 10 分钟。
Pi 提交后自行运行检查，再通过绑定交付工具提交证据。独立 Codex 复测并验收。

```bash
python3 -B validation/live_smoke.py --output-parent /path/to/writable-parent
python3 -B validation/live_smoke.py --run --output-parent /path/to/writable-parent
```

每次命令创建新的夹具，不修改已有项目。准备夹具或假 agent 通过都不能证明模型连通性。
`integration_smoke.py` 已移除：其旧版快照和自动合并接口不再存在。历史验收记录仍保留。
认证、私有配置、完整会话与任务运行数据均保存在仓外，分享时只导出所需脱敏摘要。

## 历史材料的边界

本目录中的日期子目录、JSON 摘要及 stdout/stderr 是历史记录，包含已移除界面的测试名称和路径。
它们不代表当前接口、安装状态、服务健康或本次模型连通性；当前版本无需运行旧 Pi 扩展测试。
这些记录中的源码哈希只对应当时版本，不重新计算它们来宣称新版本通过验收。

- [后台服务与只读查询](codex-interface-20260929/README.md)
- [授权集成及故障恢复](pi-integration-20260929/README.md)
- [服务部署与真实后台调用](service-deployment-20260929/README.md)
- [返工与旧任务恢复](rework-20260928/README.md)
- [提交故障与缓存归档](submission-recovery-20260928/README.md)

部分历史导出为文字规范化后的展示件。2026-09-28 的更名说明及原始／展示哈希见
[name-migration.json](name-migration.json)；2026-09-30 的项目专属措辞清理见
[documentation-cleanup.json](documentation-cleanup.json)。后者仅调整三个导出的说明文字，
结论、attempt、计时及受测源码哈希不变。未经改动的历史原件保留在各记录指定的 Git 提交中：

```bash
git show COMMIT:validation/PATH
```

旧任务的仓外数据库、manifest 和 attempt 原件未搬迁或改写。原生 Pi 任务不自动转为后台任务，
也不能通过重用状态目录或重复发布来绕过旧冻结证据。
