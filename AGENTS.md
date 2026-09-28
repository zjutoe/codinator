# Codinator working agreements

- Preserve task contracts and immutable attempt evidence. Never replay an uncertain prompt.
- Only the controller writes workflow state. Only an independent Codex verdict can accept work.
- Never auto commit/push/merge or expand a submitted allowlist. Do not silently switch models.
- No unsandboxed fallback. Inspect real subprocess exit, protocol completion, delivery and snapshot identity.
- Changes to lifecycle, persistence or isolation require meaningful fault-injection tests and non-author review.
- Mock/fake-agent tests are not real model connectivity evidence. Keep their results clearly separated.
- Store credentials and raw task runtime outside this Git repository; never print authentication contents.
- Keep the implementation small and standard-library based. Do not introduce distributed queues or UI frameworks without an actual need.
