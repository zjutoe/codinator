# Handoff: format-only handoff protocol and automatic Codex guidance

## Goal

Implement work packages 1–3 of `docs/handoff-review-refactor-plan.md`, and prepare the
documented interface needed for the bounded real-agent pilot in work package 4.
The user authorized execution of that plan on 2026-10-03. Codinator transports and
validates protocol data. It cannot judge whether document content is meaningful,
truthful, sufficient, actionable, or compliant with natural-language requirements.
Pi owns implementation, project commands, tests, and task-local commits.
Independent Codex owns exact-candidate verification and acceptance.

## Scope and constraints

- Modify only `src/codinator/`, `tests/`, `examples/`, `README.md`, `README_CN.md`,
  `docs/codex-interface.md`, `docs/git-checkpoints.md`, and `pyproject.toml`.
- This handoff, the approved plan, `AGENTS.md`, `.gitignore`, and other paths are frozen.
  Preserve unrelated work, published contracts, immutable attempt evidence, and v1 history.
- Keep standard-library implementation, fixed Bonsai/Pi and Codex models, and actual
  bubblewrap isolation. No unsandboxed fallback, automatic merge/push, source snapshots,
  controller Git commands, controller project tests, or semantic content validators.
- Retain current v2 Git/check requirements. General task support, removing fixed
  development rules, and changing model configuration are later work.
- The existing controller used to implement this change runs from an immutable ordinary
  checkout of the publication baseline, outside this working directory. Do not alter it.
- Work is authorized for a shared maximum of four hours, including implementation,
  rework, review, guidance, and the later pilot. The manifest records a fixed absolute
  deadline. Each model process has a 90-minute cap. This implementation task has at
  most four implementation rounds. No retry, new task ID, or pilot resets this allowance.
- Do not start detached/background processes, alter Git configuration/hooks/ignore
  rules, rewrite history, change branch, merge, or push. Commit allowed changes before
  checks and rerun every required check against the final exact candidate SHA.

## Required inputs

- Approved plan: `docs/handoff-review-refactor-plan.md` (frozen in publication baseline).
- Current contracts: `AGENTS.md`, `docs/codex-interface.md`, `docs/git-checkpoints.md`.
- Implementation: `config.py`, `delivery.py`, `agents.py`, `engine.py`, `checkpoints.py`,
  `process.py`, `store.py`, `status.py`, `cli.py`, and `sandbox.py` under `src/codinator/`.
- Existing tests use fake agents and isolated Git fixtures. Inspect fixture/import
  behavior before execution. Fake-agent results are not real-model connectivity evidence.

## Deliverables

1. Five shipped Markdown templates, containing role prompts, for handoff, summary,
   help request, review, and guidance. Include templates in an installed package and
   expose a small CLI command to print a selected template without dispatching a model.
2. An explicitly versioned, opt-in format protocol for new v2 tasks, bound submission
   tools, immutable message/template/prompt evidence, and readable status reporting.
3. Automatic Pi → Codex guidance → fresh Pi attempt routing within the frozen task,
   with non-author final review, shared budgets, bounded rounds, and single-writer locks.
4. Stage/final summary distinction, bounded closeout steering, explicit missing-final
   summary reporting after interruption, and conservative recovery.
5. Meaningful regression/fault-injection tests and updated operating examples/docs.
   Document exact live-pilot publication and evidence collection steps; do not claim
   the external pilot has run. Main Codex performs its preparation after code acceptance.

## Protocol decisions

These decisions fix the external contract before implementation. Keep internal layout
small; reuse existing bound delivery tools and immutable attempt directories.

### Version and format

- Add optional integer `handoff_protocol: 1` to v2 manifests. Absence preserves the
  published v2 contract. Reject unknown versions and use with v1. Never rewrite old
  manifests or retrofit requirements into existing tasks.
- Use fixed level-two Markdown headings to express required sections. Body language
  and additional sections are unrestricted. Do not count headings inside fenced code.
  Nonempty section bodies are the only body-level requirement; literal `无`, `未执行`,
  or `None` satisfy it. Do not infer missing semantic information from wording or length.
- Handoff headings: `Goal`, `Scope and constraints`, `Required inputs`, `Deliverables`,
  `Acceptance criteria`, `Handoff rules`.
- Summary headings: `Status`, `Completed`, `Incomplete`, `Attempts and results`,
  `Artifacts and evidence`, `Deviations and unknowns`, `Next step`.
- A help request includes all summary headings plus `Blocker`, `Question for Codex`.
- Review headings: `Subject`, `Verdict`, `Basis and verification`, `Rework`, `Stop reason`.
- Guidance headings: `Question`, `Advice`, `Basis`, `Unknowns`, `Next boundary`.
- Preserve strict JSON control fields separately from body text. Bind immutable message
  identity, kind, task/round/attempt, original contract digest, reply-to request/delivery,
  and declared candidate identity. The bound tools/controller supply identities and
  destinations; agents supply declared results and document bodies. Persist the exact
  templates and prompts delivered to each agent. No separate generic workflow platform.
- Missing headings/fields and invalid types are format errors with specific locations.
  Duplicate/stale/conflicting identities and tampered evidence fail explicitly. An
  idempotent identical submission must not launch a second consumer or overwrite evidence.
- Text such as “everything done” or “please improve” under every required heading passes
  format validation. Agents, not the controller, must detect its inadequate meaning.

### Guidance and counters

- New-protocol Pi dispositions are `awaiting_review`, `needs_guidance`, and `blocked`.
  `blocked` stops for main Codex/external intervention; `needs_guidance` requests automatic
  read-only Codex guidance. Do not classify prose to choose these branches.
- Guidance results are `continue` and `blocked`. Keep reviewer results `accepted`,
  `needs_changes`, and `blocked`. Validate structural result/body fields and bindings,
  without deciding whether their contents justify the declared result.
- A help submission binds partial work to a declared Git candidate with the normal
  evidence format; unperformed checks remain `not_run`/null. Pi must leave a clean
  committed source or report `blocked`. Do not have the controller discover/adopt HEAD.
- Launch guidance only after confirmed successful Pi protocol completion and process
  cleanup with no active tool. The guide is a separate Codex process using the existing
  review model/reasoning level, with source and Git read-only. It verifies the declared
  branch/candidate/clean state itself before recommending automatic continuation.
- Store guidance as a new immutable record linked to the original request and contract.
  It cannot amend requirements, grant permissions, extend budget, or accept implementation.
  A fresh independent reviewer later checks the original requirements and final candidate.
- A `continue` result schedules a fresh Pi process from the candidate asserted in the
  help packet, with the original handoff and linked guidance supplied as feedback.
  Every Pi dispatch, including continuation after guidance, consumes an implementation
  round and a new attempt. Guidance/review belong to the requesting attempt and do not
  create another implementation round. All phases consume the same wall-clock deadline
  and retain the process cap. Refuse further Pi dispatch when max_rounds is exhausted.
- Dispatching, waiting, blocked, and terminal status must expose the current handler,
  pending input/reply relation, applicable deadline, and next action. Only independent
  Codex `accepted` can mark the task accepted. Message receipt is never acceptance.
- A crash, missing/invalid guide result, unconfirmed process exit, or ambiguous dispatch
  stops and preserves the scene. Recovery never blindly replays guidance or Pi prompts.
  Main Codex reconciles Git before explicit resume; review-only keeps original evidence.

### Checkpoints and closeout

- Keep periodic RPC progress in the same Pi session. Snapshot each resolved checkpoint
  as a stage summary with its identity; keep it distinguishable from final delivery.
  For new-protocol reports, retain the required summary sections as well as existing
  structured progress controls. Do not reinterpret `completed` claims as verified facts.
- When the agent explicitly requests guidance at a checkpoint, steer it to submit the
  bound final help packet and stop. Allow at most the existing five-minute response grace
  within the original hard limit. If it cannot deliver cleanly, stop as blocked and retain
  missing-final-summary evidence; do not auto-continue a forced/uncertain interruption.
- For RPC Pi processes, request closeout once when remaining process time reaches
  `min(300 seconds, half the effective process timeout)`, where the effective timeout
  is the smaller of the process cap and remaining task budget at dispatch. Use the
  existing event loop and bounded
  RPC acknowledgements, without postponing cancellation or the original hard deadline.
- On every stopped attempt distinguish final summary present, latest stage summary,
  and final summary missing. Keep raw exit/protocol evidence and do not fabricate an
  agent conclusion. This reporting must also work for attempts without checkpoints.

## Acceptance criteria

- A1: All five templates ship, contain the prescribed headings and role prompts, and
  can be retrieved without model calls or workflow-state mutation. Actual dispatch
  records preserve the template version/content and agent prompt.
- A2: New-protocol submissions reject missing sections/invalid identities with precise
  format errors, accept structurally complete vague text and honest empty-item markers,
  and remain immutable/idempotent. Existing v2 behavior and read-only v1 history survive.
- A3: A valid help delivery reaches separate Codex guidance and returns to a fresh Pi
  attempt on `continue`, then independent review. User/external blockers stop explicitly.
  No semantic routing, contract changes, increased budgets, overlapping writer, or
  guide-produced acceptance is possible.
- A4: Fault injection covers duplicate and late replies, wrong task/attempt/contract,
  tampered/absent guidance, interruption in each new phase, failed protocol completion,
  process-exit uncertainty, pause/cancel, max-round/total-budget limits, and multiple
  controllers/state roots attempting the same checkout.
- A5: Normal completion/help includes final summary; checkpoints preserve stage summaries;
  closeout is bounded and does not extend deadlines; hard interruption preserves raw
  evidence and explicitly reports missing final summary. New timer behavior has focused
  deterministic fault tests, rather than waiting for real 30/90-minute limits.
- A6: The controller launches only agents and validates protocol/evidence integrity.
  Pi/Codex own real Git operations and project checks. Review and guidance are read-only,
  fixed model identities are checked, and bubblewrap protections remain effective.
- A7: Existing and new tests pass against the final exact SHA, and native bubblewrap
  validation passes. The independent reviewer checks fixture quality and actual raw
  tool/check evidence. Mock tests are described only as mock tests.
- A8: Documentation/examples describe the implemented API, lifecycle, budgets, error
  correction, explicit recovery, and a bounded real-agent pilot. No unimplemented state
  or unperformed real-model validation is advertised as working. Preserve the plan's
  pilot metrics: comparison baseline, Codex-to-Pi reply delivery, manual intervention
  count/reasons, format-error time/rounds, repeated blockers, and recoverable stop evidence.

## Handoff rules

Read the immutable delivery contract and use its bound submission command. The existing
bootstrap controller uses the original v2 format, so this implementation task itself
does not set `handoff_protocol`. Its summary should still use the new summary headings
for clarity; final Git/check evidence must satisfy the original attempt-bound contract.
Do not change the frozen plan or this handoff while implementing.

If an instruction is unclear or repeated attempts fail, submit honest `blocked` using
the bootstrap bound tool, explain the concrete question and actual attempts, then stop.
Main Codex supplies corrective guidance within the original authorization. Never replay
an uncertain operation or fabricate a final SHA/check result for the sake of submission.
After delivery, independent Codex verifies actual branch, exact HEAD, clean state,
ancestry, every commit's scope, frozen handoff/ignore rules, original tool evidence,
and all required checks. Only its verdict accepts this implementation.
