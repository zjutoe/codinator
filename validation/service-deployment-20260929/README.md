# 用户级服务部署与真实后台核验

> 历史验证记录，仅对应当时版本；当前操作以 [项目说明](../../README.md) 为准。

2026-09-29：codinator.service 已 enabled / active / running，由 systemd 用户管理器持有。
真实任务 deploy-live-20260929T092520Z 为 accepted / round 1 / attempt 2。

- Pi RPC：bonsai/bonsai2-27b/xhigh；真实实施、delivery 和 agent_settled 已核对。
- 控制器只读检查退出 0；独立 Codex gpt-6-astra/xhigh 完成 turn.completed，accepted 绑定冻结快照。
- 前台查询退出后，宿主观察确认审查继续运行；随后由同一服务 PID 完成任务。
- 首轮默认配置只有 bonsai-local，在 RPC 提示发送前失败。8 个旧证据文件哈希未变。
- 服务独立 Pi 配置为 ~/.config/codinator/pi，unit 显式设置 PI_CODING_AGENT_DIR；
  全局 Pi 配置及旧任务未改，显式 resume 创建 attempt 2。重生成 unit 时须保留该设置及既有代理。
- Codex 沙箱看不到宿主 PID；沙箱内 process_alive=false 不能单独证明服务退出，
  本次使用宿主 systemctl 和进程关系核验。

[完整摘要](summary.json)。控制器原件：/home/mye/.local/state/codinator/tasks/deploy-live-20260929T092520Z/attempt-0002

部署命令、unit 的修正前后版本、隔离工作区副本保存在 /home/mye/.local/state/codinator/deployments/deploy-live-20260929T092520Z。
认证和私有会话没有导出到源码仓库。自动 needs_changes 返工仍以此前故障测试为依据，
本次没有强制真实 review 返工。T0019 仍 accepted，T0020 未发布；无产品代码修改或 commit/push。
