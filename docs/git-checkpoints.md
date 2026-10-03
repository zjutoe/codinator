# Git 分支与交接协议

源码版本由 Pi／Codex 使用标准 Git 管理。Codinator 不调用 Git，不生成源码快照或提交，也不运行项目测试；它只传递 handoff、交付与验收结果，管理进程、状态、预算和单写入者锁。

## 发布

主 Codex 在普通仓库中创建并检出任务分支，提交 handoff、固定 `.gitignore` 和实施基线，确认工作目录干净。新 manifest 使用 `version: 2`，填写完整 `git.base_commit` 和 `git.branch`。当前只支持实际 `.git/` 目录的普通 checkout，不支持 linked worktree；控制器按这个目录的真实路径加锁，不自行实现 Git 仓库发现。

```json
{
  "version": 2,
  "git": {
    "branch": "mf-001-S02-WP02",
    "base_commit": "8c05779165ecb0030643e8d09c1fdfd2ecf43e52"
  }
}
```

这是版本字段示例，完整格式见 [task.json](../examples/task.json)。Git 不允许 `mf-001` 与 `mf-001/S02/WP02` 同时作为分支名；可从 `mf-001` 创建 `mf-001-S02-WP02`。继承关系取决于创建时的提交。

`submit` 保存原始 handoff 副本和发布者声明的 Git 身份，不代表已经核验真实 Git 状态。Pi 与独立 Codex 均读取状态目录中的只读副本；二者分别核对其与基线中受 Git 管理的原契约一致。发布前准备和核实属于主 Codex 的职责。

## Pi 实施、提交与自测

Pi 先用标准 Git 确认分支、精确起点 SHA 和干净状态。偏离时报告 blocked，不 reset、stash、强制切换或自动采纳未知改动。实施只修改允许路径，并在测试前生成本地提交。再次修改时重新提交、重新验证，最终交付的所有必需检查绑定最终 SHA。

Pi 自己执行 `checks[].argv`，遵守各项超时，保存实际命令、输出和退出码。临时输出写 `/tmp`，缓存按发布前固定的 `.gitignore` 处理。控制器不归档、删除缓存，也不改变忽略规则。

除 summary 和 completion 外，Pi 通过绑定工具提交 `evidence.json`，包含当前任务／轮次／attempt、分支、起点和候选 SHA，以及每项检查的名称、精确 argv、对应 SHA、状态、退出码和原始证据引用。没有执行的检查标为 `not_run`、退出码为 null；不能编造通过结果。blocked 允许没有候选包，中断现场不由控制器代为提交。

Pi 获得本 checkout 的 Git 元数据写权限，仅用于任务分支本地提交。禁止修改 Git 配置、hooks、其他分支、忽略规则和历史；禁止合并或推送。Git 命令显式禁用 hooks、fsmonitor 和提交签名。此权限变化是实际边界，不能再声称实施者的 `.git` 只读。可写挂载中的符号链接、硬链接文件和不可遍历目录会在启动前拒绝；主 Codex须准备无此别名风险的 Git 元数据，控制器不自动复制或拆分共享对象。

## 独立 Codex 核验

控制器只校验交接格式、身份、SHA 格式、检查引用和证据完整性。Pi 的检查记录仍是待验证声明；控制器不会将其转换为自己的“检查通过”结论。

独立 Codex 在只读源码及 Git 上自行核验实际分支、HEAD、干净状态、祖先关系、每个新增提交的修改范围、冻结契约和原始 Pi 工具事件，并亲自复跑全部必需检查。测试前后均核对候选身份；相同 tree、不同 SHA 不能代替原候选。审查者最终给出绑定候选 SHA 的 `accepted`、`needs_changes` 或 `blocked`；只有该结论能验收或提出实质返工。

`before.json`、`implementation.json`、`submission.json` 只保存 branch/commit 的交接声明。`status.git.verification` 区分发布／实施者声明和独立 Codex 验收。没有控制器 Git journal、源码 blobs、自动检查阶段或自动 integration。

## 中断与恢复

控制器确认进程停止并保留证据，不创建中断提交，不重放结果不确定的提示。主 Codex 检查现场并通过标准 Git 处理后，再明确恢复；范围或起点需改变时准备新的 handoff，保留旧契约与预算账目。

普通 `resume` 新建实施 attempt。仅审查中断时，`resume --review-only` 验证原始 Pi 协议、选定交付及其哈希，复用候选声明且不增加实施轮次。Git 是否仍匹配由新的独立 Codex 检查，必需检查也由其执行。已有未协调 verdict、缺失或篡改交付均拒绝重试，不能绕过已产生的结论。

同一 checkout 的控制器锁不阻止同 UID 用户绕过控制器直接改文件，必须遵守单写入者约定。

启用 `handoff_protocol: 1` 时，技术求助也提交正常 Git／检查 evidence 包，绑定干净且
已提交的部分候选；未执行检查标记 `not_run`／null。独立只读指导 Codex 核验真实 Git
状态后才可声明 `continue`，新的 Pi attempt 消耗新轮次并从该候选继续。指导不代替
最终独立验收。控制器仍只校验声明、格式和绑定，不自行发现或采纳 HEAD。
若指导进程中断或结果不可信，保留现场并停止，不自动重放；主 Codex核对后才能显式恢复。

## 旧任务与预算

v1 状态、快照、blobs、检查／集成证据仅供历史查询；新版拒绝执行与恢复，后台不会派发 v1。升级前先用旧服务停下所有活动任务。不能修改旧数据库或 manifest 把 v1 冒充 v2。

后继任务由主 Codex核对旧成果、已用时间与轮次，冻结实际剩余预算及适用的旧绝对截止时间；新 task ID 不意味着新额度。验收后代码留在任务分支，合并和推送须另获明确授权，由 Pi／Codex 执行。

实施沙箱将既有 Git HEAD、配置、hooks 和 packed-refs 重新挂载只读；普通提交仍可写 index、objects 和任务 ref。其他 ref 的不变性由 Pi 的起始 `git show-ref` 原始工具记录与独立 Codex 核对，不宣称所有 Git 元数据均有操作系统级写保护。
