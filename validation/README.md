# 验证记录（更新至2026-09-26）

## 本地测试与审查

- 最终代码：`tests-r5.stderr`，38 项测试通过，45.315 秒，无跳过。
  测试使用明确标识的假 agent 子进程；不把它们当作真实模型联调。
- 较早轮次输出保留在 `tests-r2`～`tests-r4`；服务代理专项输出为 `service-proxy.stderr`。
- `tested-files.json` 记录第 5 轮受测 Python 源码与测试的 SHA-256。
- 提交前仅删除 7 个文件末尾的多余空行，Python 文件 AST 均未变。原测试记录和哈希
  不回写；前后哈希见 [commit-hygiene.json](commit-hygiene.json)。
- 独立非作者审查覆盖进程生命周期、状态与通知事务、快照、沙箱及协议终态。
  发现的问题已修正并复审；审查者独立执行了针对性测试及真实 bubblewrap 探针。
  审查调用原件在本次 Codex 会话中，本目录不伪造单独审查日志。
- `native_sandbox.py` 已实际运行通过：允许路径可写、冻结文件/Git/证据只读、
  `/tmp` 证据重挂可读、可写硬链接在启动前被拒。原始输出见本次会话工具记录。

## 真实模型联调

真实运行目录在 `/home/mye/data/`，包含私有认证副本和原始工具输出，不进入代码仓库。

1. `codidator-live-2t64yxcz`：Pi 继承代理而连接失败，控制器正确标记 blocked。
   随后按用户指示改为 Pi 直连本机，Codex 保留代理。
2. `codidator-live-2h3hzypf`：Pi 已成功读取契约并调用工具，随后达到 420 秒阶段预算。
   控制器停止进程并标记 blocked。同期服务唯一槽位处理约 18～20 万 token 请求；
   排队/共享服务竞争是推断，不能仅据此认定其他请求的来源。
3. `codidator-review-live-dmhasr5p`：独立 Codex 示例真实返回 accepted，进行了源码检查
   和 1,000 次隔离调用。该示例由控制器构造，不是 Pi 交付，也不是完整闭环。
   原适配器误把重连中的 error 诊断当最终失败；修复后要求 exit 0、turn.completed、
   无 turn.failed 及有效且摘要匹配的结论。原始失败不改写，回归在最终 38 项中。
   脱敏摘要见 [codex-live-summary.json](codex-live-summary.json)。
4. `codidator-live-_fn0my19`：阶段预算 1,200 秒的完整闭环试跑。用户确认另有 Pi 长任务
   使用同一 Bonsai，并要求等待它结束。本轮按用户指示暂停，实际状态为 paused，
   原因为 `Pause/cancel requested`，未完成实现/验收。暂停后进程已收尾，记录保留。
   实施模型继续使用 `bonsai2-27b`。脱敏记录见 [live-pause-summary.json](live-pause-summary.json)。

2026-09-26追加验证（原失败／暂停记录均保留）：

5. 同一示例的 `attempt-0002`：Pi 32.42秒正常完成，控制器2项测试通过；Codex没有完成审计，
   日志反复出现网络不可达／请求超时，约1202秒后被1200秒阶段预算终止。该次启动未录制代理环境，
   因此不能断言一定缺少代理，也不能把网络失败解释成模型推理太慢。
6. `attempt-0003`：显式为控制器提供本机HTTP/HTTPS代理环境，并按已有命令恢复为新attempt。
   Pi仍直连Bonsai，RPC确认 `bonsai / bonsai2-27b / xhigh`；Pi用时33.06秒，控制器2项测试通过；
   Codex使用原配置 `gpt-6-astra / xhigh`，用时281.78秒，进程exit0、`turn.completed`、
   摘要匹配的无遗留问题accepted结论齐全。审计包含独立复跑、144个整数边界和48个非法类型探针。
   SQLite最终为 `accepted / done / attempt=3`，无活动PID。单阶段1200秒上限未变，未修改程序源码。

**该微型任务的完整真实闭环现已通过一次。** 本次仍有可恢复重连诊断；不将一次通过推广为稳定性或
KMesh接入已验证。原始材料在上述state目录；可分享的摘要见
[live-proxy-recovery-summary.json](live-proxy-recovery-summary.json)。JSONL完成事件的说明见
[OpenAI Docs非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)。

前台 `run` 不读取后台unit保存的代理环境。需要代理时使用README的显式环境示例；
后台默认队列和本示例的独立state目录仍分开。本示例已accepted，无需再resume/run。

没有向真实会话发送测试通知。通知重试、稳定事件 ID、验收/通知原子事务由假 agent
故障注入测试覆盖；`codex queue` 成功也仅代表入队，不代表用户已读。

## 本机安装

代码安装于 `/home/mye/src/llm/codidator`，虚拟环境内 editable 安装，无运行时第三方依赖。
`codidator doctor` 通过。用户级 `codidator.service` 已启用，默认队列为空。
服务保存本机 Codex 代理，Pi 直连规则在派生进程单独生效。未修改全局模型/代理配置。

KMesh 工作树未被本项目实施或联调修改。本次2026-09-26排障仅更新操作说明与脱敏证据，未commit/push。
