# Pi 模型选择验证（2026-10-09）

受测源码提交：`eeed0b1d29990bcff27bb9714c8b9d7f7fddfdae`。
任务分支：`task-pi-model-selection`。
后续提交仅修正文档和保存本记录，不改变受测源码或测试。

## 配置与兼容性

原生任务通过 `pi={provider,model,thinking}` 选择 Pi 中已配置的模型。
省略字段仍按 `bonsai/bonsai2-27b/xhigh` 执行和核验，不修改旧 manifest、证据或预算。
实施、交付修复和仅重试验收绑定同一冻结身份。
指导与独立 Codex 仍为 `gpt-6-astra/xhigh`。
端点和认证仍由 Pi 配置；控制器没有新增直接模型 HTTP 接口。

## 检查与独立审查

检查命令：`.venv/bin/python -B -m unittest discover -s tests`。
完整回归结果：退出码0；418项检查全部通过，耗时522.244秒，无跳过。
新增的六项身份和生命周期检查通过。

非作者独立审查通过上述受测提交。
独立执行了18项目标和旧默认路径检查，以及指导续执行、错误身份在发送任务前拒绝、原生 thinking 枚举三个补充探针。
记录：`/home/mye/data/codinator-probes/independent-pi-selection-review-eeed0b1.txt`。
模拟 agent 证据不代表真实模型调用。

## 真实 Pi 工具探针

实际身份：`strata/qwen3.8-flash-next-iq3_s/high`。
通过现有 `_pi_worker` 和 bubblewrap 文件边界执行，硬上限120秒。
Pi 完成一次 bash 工具调用，创建 `probe.txt`，内容为 `42`。
过程成功退出，原始事件和结果保存在：
`/home/mye/data/codinator-probes/strata-1791526326034122874/`。
宿主网络权限由工具升级批准；未使用无文件沙箱回退。

原生 Pi 将该 strata 模型的 `xhigh` 降为 `high`。
控制器按契约拒绝了不匹配身份，未发送任务；记录保存在：
`/home/mye/data/codinator-probes/strata-1791526078534272500/`。
正确 `high` 身份在受限宿主网络下的连接失败也保留在：
`/home/mye/data/codinator-probes/strata-1791526224754865452/`。
示例据此使用 `high`，没有弱化身份校验。

该探针证明原生 Pi 握手、模型请求和一次工具执行连通。
它不是真实 Codinator 全流程研究验收，也不证明服务端权重或 strata 研究质量。
本次没有启动或恢复 AlphaLab 研究任务。
