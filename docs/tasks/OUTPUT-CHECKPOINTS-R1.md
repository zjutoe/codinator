# Codinator：只读观察软检查

## Goal

为 Pi 任务增加 checkpoint_mode="observe"。每隔 checkpoint_seconds，控制器只读采集既有日志、工具事件和声明产物。该模式不要求 Pi 定时报告，不发 checkpoint steer，也不因报告缺失或日志静默停止。硬上限、取消、最终交付和非作者独立验收保持。

首个可验证行为及已知失败按 Required inputs 中当前续执行摘要执行。原目标：fake Pi 有一个跨软检查时刻的活动工具，没有进度报告。观察记录按时产生，未发 steer、未触发旧五分钟停止；原进程继续并正常完成。先实现此行为并提交、执行 focused 检查，再补齐相同目标的读取边界和状态展示。

## Scope and constraints

Pi + Bonsai 实施，独立 Codex 验收。工作目录 /home/mye/src/llm/codinator，分支 observer-soft-checkpoints。完整发布基线和起点由 manifest / 当前 delivery-contract.json 提供。实施前由标准 Git 核对分支、SHA、clean、refs，核对冻结 handoff。只允许任务分支本地提交，不合并、不推送、不改服务、配置、认证或旧任务状态。

允许修改：src/codinator/observations.py（新增）、src/codinator/config.py、src/codinator/agents.py、src/codinator/status.py、tests/test_observations.py（新增）、README.md、README_CN.md、docs/codex-interface.md、AGENTS.md、examples/observation-task.json（新增）。不重构旧 checkpoints.py，不扩大到外部宿主 Codex 或自动指导模型。需要其他路径则提交具体阻塞求助。

本阶段独立于 MF-001：stage CODINATOR-OBSERVATION-CHECKPOINTS，57600 秒、最多 4 次实施，单 Pi/Codex 进程硬限 5400 秒。检查、审查、返工共用阶段额度。新模式尚未部署，本次改造包不启用旧的强制 Pi 报告软检查；主 Codex 观察原始输出，30 分钟核对一次，不能虚构新模式已用于本包。不要以缺少旧检查点请求为由停工或调用报告工具。

源码先提交后检查。每次源码改变后新提交、复测受影响检查。最终候选必须干净，完整回归与独立验收绑定准确 SHA。只用标准库，不增加依赖。Codinator 不运行项目 Git/测试，不制造检查 verdict，不接受日志关键词为成功或自动认为没有日志就是阻塞。新观察机制本身只做元数据和受限读取。

## Required inputs

当前续执行摘要：第一轮原始证据 /home/mye/.local/state/codidator/tasks/CODINATOR-OUTPUT-CHECKPOINTS/attempt-0001。进程已硬停；主 Codex 保存 Pi 未提交源码到 ffc7f370d80524eb763e81dbbc4da9ad3feb3715，该提交未验收。共 43 项 focused 检查，原始输出为 42 通过、1 个 fake-RPC 超时；未跑完整回归。文档和示例尚未完成。剩余预算读取同一 stage，已用 1 轮，本次是阶段第 2 轮，deadline 不变。独立 reviewer 必须核对本次与前轮自 92401e0d9d6e42cc9fe4363ddf8223a84d506933 起的全部累计源码改动，不只看当前基线之后的差异。

首个修正已确定：tests/test_observations.py 的 FAKE_AGENT 嵌套字符串把事件分隔符写成字面量反斜杠+n，导致 run_process 永远没有 JSONL 完整行。可把 FAKE_AGENT 改为 raw triple-quoted 字符串，生成代码使用普通 "\n"。对生成源码的 emit 输出验证确实以 LF 结尾，再提交并运行 focused。禁止继续猜进程定时问题或延长 fake 超时。当前 process.py 无需修改。

仍须完成以下范围内修正及对应证据：
1. baseline 的 next_due 必须是 start+interval。每条 record 包含 task_id/round/attempt，status 核对契约及每条记录绑定，保存 contract 与 latest 分开；不能把循环中的 record 变量误用于 interval/sources。异常绑定或已写证据损坏应明确暴露，不静默当没有记录。
2. 每次采样的输出片段用周期序号或内容身份保存不可覆盖版本；记录引用必须与该次实际采样字节和范围一致。旧实现 outputs/<固定文件名> 第一次写后拒绝更新，却继续引用新读取元数据，是错误证据。记录 inode/dev、范围及实际读取长度，识别替换/截断和写入中状态。
3. 安全读取从 base 目录文件描述符逐级打开，每一级使用 O_DIRECTORY/O_NOFOLLOW，末级用 O_NOFOLLOW/O_NONBLOCK，并以打开后的 fstat 验证普通文件再读取。只靠 resolve+lstat 存在父路径竞争；根内父目录符号链接同样不允许。missing、无权限、特种文件、非文本、部分 JSON 保留未知，不中断 Pi。控制器状态写入失败则明确失败；不能 except Exception: pass 隐藏持久化失败或伪装已采集。
4. RPC 原始日志只保存有界元数据/偏移引用和工具事件计数，不分析模型内容或复制对话。声明日志保存受限原始片段。旧日志通过 baseline 区分；全记录序列化 <=1,000,000 字节。观察器不产生项目 verdict，不调用 Git/测试/子进程。
5. 补真实本地 fake 进程的 cancel、hard-timeout 和无最终交付不会 accepted 的验证。核对新 worker prompt 无定时报告义务；旧模式回归保留。完成中英文文档、AGENTS.md 和示例，移除“任何观察错误都不能中断”的过强声明，区分产物不可读取与控制器证据写入失败。

第一次检查就保存全量 stdout/stderr；按 subprocess 实际 returncode 判定，不能使用 tail/grep 管道退出码。当前候选文件未变化时不为补日志反复重跑全部 focused。两次同一反例失败必须报告需指导，不再连续几十次试相同假设。最终 summary 和 evidence packet 仅用原契约字段；blocked 可以只交七节简短小结。下一条操作是修正 JSONL fixture、提交并 focused 检查。


已核实基线源码 92401e0d9d6e42cc9fe4363ddf8223a84d506933。实际发布 SHA 会包含本 handoff，优先用当前 contract 的起点。

最小代码入口：agents.py 的 PiProtocol.start/observe/tick/event/finish 与 worker 的 checkpoint 分支；process.py 的 run_process 已在活动工具期间调用 protocol.observe，并独立执行取消和硬超时；config.py 的 load_manifest；status.py 的 report。目前 Checkpoints.tick 发 steer，observe/enforce 因五分钟未收到报告而锁定停止。保留所有旧路径与冻结契约行为。

tests/test_checkpoints.py 是本地 fake-RPC 定时/停止测试。tests/test_engine.py 使用 FakeSandbox 和 fake_agent，设置空认证目录；tests/test_sandbox.py 有本地 bubblewrap 隔离核验。没有真实模型调用必要。检查前仅阅读相关 fixtures；不得调用真实 Pi/Codex 作为产品测试、使用认证信息或读取模型历史。

确认无活动/排队任务后，主 Codex 已创建专属分支。部署由主 Codex 在实施进程结束且独立验收后处理。Pi 不重启当前后台控制器。

## Deliverables

最小实现、针对性故障注入测试、中英文使用说明和合法示例。更新本项目 AGENTS.md，区分观察模式与旧报告模式。原有报告模式和历史证据不追溯修改。最终 summary 引用真实 SHA、完整检查输出和具体限制。

新 manifest 接口：checkpoint_mode 仅支持显式 "observe"。它要求 version=2、handoff_protocol=1 和合法 checkpoint_seconds。未指定 checkpoint_mode 时保留既有行为。checkpoint_outputs 是最多 16 个不同的 workspace 相对文件路径，可省略或为空（仍观察代理原始日志和工具事件）。拒绝绝对路径、..、通配符、保护目录、目录路径和重复项。该字段只能用于 observe。observe 与 checkpoint_format/first_checkpoint/counterexamples 旧格式字段互斥；observe 不支持 implementation="external"，明确拒绝而非假称外部宿主可自动监测。拒绝未知模式，不修改旧 manifest。

新观察目录在 attempt 状态目录中，Pi 不可写。记录 task/round/attempt、序号、UTC/elapsed、采集范围和原始来源。启动记录基线；随后每个周期采集一次，活动工具期间也采集，不需 safe boundary 回应。每条历史记录只写一次。status 显示 mode、观察次数、最近观察及证据路径。记录明确是 observation/unverified claim，不是进度成功、测试通过或 accepted。

内建来源只使用本 attempt 的 pi/stdout.jsonl、pi/stderr.txt 和已收到的工具开始/结束事件。可记录字节增量/偏移及事件计数和活动工具；不要从模型推理内容推断进度或复制整段对话。约定输出可保存有界日志片段，单文件最多读取 32 KiB，最多 16 项；记录截断和采集字节范围。序列化单条记录须不超过 1,000,000 字节，可采用更小的固定片段上限。不全读大文件、不为哈希扫整文件、不递归扫描目录。若对采样字节哈希，明确不是全文件哈希。

输出文件及父目录不允许符号链接逃逸。避免跟随符号链接、读取 FIFO/设备或打开特种文件导致阻塞。文件不存在、写入中/被替换、无权限、非文本或 JSON 不完整，记录为未知/不可读取或截断，不因此中断 Pi。读取后仍不能证明 producer 内容真实。旧产物通过启动基线和增量区分；截断/替换日志显式记录。输出异常不会调用项目命令修复。状态目录写入失败不能伪装成功。

## Acceptance criteria

- OC-01：合法新配置和非法配置验证；旧配置、已有 compact/checkpoint/help/closeout 语义保持。没有新模式时不自动迁移旧任务。
- OC-02：活动 fake 工具跨周期、无 Pi 报告时按时采集且不中断；无 checkpoint steer/报告工具/五分钟报告超时。固定硬上限和 cancel 仍能终止进程；观察错误不延长预算。最终交付仍必需，观察不能自动进入 accepted/guidance。
- OC-03：missing、静默、部分 JSON、超大日志、截断/替换、symlink 文件和父目录、FIFO、非文本均有有界实证。日志活跃不被判为成功；没有变化不被判为失败。观察器不调用 Git、测试或任何子进程。
- OC-04：记录 attempt 绑定和不可覆盖的历史；status 只读且显示新模式。原始日志引用、偏移、采样范围和未知项清楚。新 Pi prompt 不再要求定时报告，最终 summary/evidence 义务保留。文档、示例与实际支持一致。
- OC-05：先提交后检查；focused 与完整本地回归在同一最终干净 SHA 上退出 0。独立 Codex 按原始需求核对候选、Git 范围、原始证据并复跑两项检查，重点验证生命周期、读取边界、取消和旧模式兼容。mock 只证明本地协议，不冒充真实模型连通。

## Handoff rules

立即从 prompt 原样取得 Bound submission command，先执行 --help 验证入口。随后按首个行为实施，提交并 focused 检查。不要重新调查控制器角色或阅读全部历史文档。扩读前说明具体未知和读取后的动作。

用 contract.checks 中的完整 argv 运行检查，第一次就保存完整 stdout/stderr、实际退出码。不要依靠 tail 管道退出码，不为补日志重复已通过的全套回归。最终提交后执行一次全套。检查失败如实记录并修正范围内错误，连续两次同假设失败须求助。

最终由绑定工具提交 awaiting_review 七节 summary 和 evidence packet。packet 仅 version/task_id/round/attempt/git/checks；git 仅 branch/base_commit/commit；checks 每项仅 name/argv/commit/status/exit_code/evidence。从当前 contract 原样取绑定和 argv，填写实际结果，不追加 candidate_commit/all_checks_passed 等字段。先用最简文本证据引用，勿编造复杂字符串。

阻塞时在可恢复现场交七节 blocked summary 并停止；blocked 可省 evidence packet，未执行检查记 not_run/null。不得研究绑定工具内部实现、手写 accepted、改数据库或更换模型。独立 review 决定验收。
