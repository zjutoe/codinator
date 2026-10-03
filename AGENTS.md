# Codinator working agreements

- Codex is the only user interface. Task commands manage the background service; Pi implementation and independent Codex review are controller-owned stages.

- Preserve task contracts and immutable attempt evidence. Never replay an uncertain prompt.
- Only the controller writes workflow state. Only an independent Codex verdict can accept work.
- New tasks use a dedicated Git branch in a normal checkout; worktrees are optional. Publish a clean, committed baseline with its full SHA. Run one writer per Git repository, including across state directories. Preserve unrelated work; never reset, stash or force-switch it away.
- Version 2 publication authorizes task-local checkpoint commits by the controller after Pi stops, before checks/review, and when preserving interrupted in-scope work. These commits do not mean acceptance or authorize merging/pushing. The worker cannot stage, commit or write Git metadata. Freeze the candidate SHA and clean workspace during checks/review; rework stays on the task branch with existing budgets and rounds.
- Use Git for source history and recovery; no new source snapshot/blob store for version 2. Fixed tracked Git ignore rules govern caches. Preserve old version 1 contracts, snapshots, blobs and budget history; only existing version 1 tasks retain their original execution/integration behavior. New publication requires version 2.
- Merge/push requires separate explicit authorization. Version 2 ends on its task branch after independent acceptance; it does not use the legacy integration manifest. Existing version 1 integration verifies the exact accepted tree and promotes the same commit by fast-forward. Never expand a submitted allowlist or silently switch models.
- No unsandboxed fallback. Inspect real subprocess exit, protocol completion, delivery and snapshot identity.
- Changes to lifecycle, persistence or isolation require meaningful fault-injection tests and non-author review.
- Mock/fake-agent tests are not real model connectivity evidence. Keep their results clearly separated.
- Store credentials and raw task runtime outside this Git repository; never print authentication contents.
- Keep the implementation small and standard-library based. Do not introduce distributed queues or UI frameworks without an actual need.
