# Codinator

[中文文档](README_CN.md)

Codinator provides the handoff protocol and background transport between main Codex,
Pi + Bonsai, and independent Codex review. Main Codex owns requirements, planning,
handoffs and corrective guidance. Pi implements, runs project commands/tests and makes
local Git commits. Independent Codex verifies the work and decides acceptance.

**The controller does not implement project work, run project tests or invoke Git.**
It dispatches agents, transports immutable requirements and results, preserves process
evidence, manages state and budgets, and serializes writers.

Source versions are Git commits, identified by exact SHAs. Pi/Codex perform Git
operations; Codinator records their declared identities and independent verdicts.
The controller does not create a separate source snapshot store. Document structure,
control fields and evidence integrity are controller responsibilities; whether the
content is true, useful or satisfies the task is the responsibility of its authors
and receiving agents.

## Setup

Linux, one user, serial tasks. Python 3.11+, authenticated `pi` and `codex`, `git`, and
`bwrap` are required. There are no third-party Python runtime dependencies.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps -e .
.venv/bin/codinator doctor
mkdir -p ~/.config/systemd/user
.venv/bin/codinator service > ~/.config/systemd/user/codinator.service
systemctl --user daemon-reload
systemctl --user enable --now codinator.service
```

`doctor` checks dependencies and sandbox availability, without calling models.
`service` prints a unit; it does not install or restart it. Upgrade only after old
agent processes have stopped. Generated units preserve PATH and proxy settings.
Pi removes proxy variables and connects directly; Codex review/notifications retain them.
Implementation is fixed to `bonsai / bonsai2-27b / xhigh`; guidance and independent
review use `gpt-6-astra / xhigh`. These choices are currently fixed in the implementation.
Pi RPC attests client configuration, not the weights loaded by the remote server.

Guidance and review require Codex named permission profiles. Their profile extends
`:read-only` and grants only `/tmp` writes for fixtures and logs; the outer bubblewrap
sandbox keeps source, Git and original evidence read-only. No unsandboxed fallback is used.

For an existing editable installation, restart `codinator.service` after updating
code so the running process loads it. Confirm all agents have stopped before upgrading.
Keep the existing unit's state directory, executable paths and environment; use that
same state directory for publication and status commands. Restarting the service does
not resume paused/blocked tasks or make v1 history executable.

## Publication and execution

Main Codex prepares a normal checkout on a dedicated task branch, commits the handoff,
fixed ignore rules and baseline, and verifies a clean workspace. New publication requires
[manifest v2](examples/task.json), with the full baseline SHA, branch, allowed paths,
required checks and authorized budgets. Linked worktrees are not supported by this protocol.

```bash
codinator --state-dir /absolute/state submit /absolute/task.json
codinator --state-dir /absolute/state status TASK_ID
codinator --state-dir /absolute/state pause TASK_ID
codinator --state-dir /absolute/state resume TASK_ID
codinator --state-dir /absolute/state cancel TASK_ID
```

`submit` authorizes dispatch. `status` is read-only and starts no agent. The background
`serve` process owns execution; do not run competing writers or edit workflow databases.
Implementation, guidance and review agents read the controller's immutable published handoff.
The publication SHA is a publisher assertion, verified against real Git by Pi/Codex.

New v2 tasks can opt into `"handoff_protocol": 1`; existing manifests keep their
published behavior. The document rules below describe this opt-in protocol. Existing
tasks must follow their frozen contracts; new rules do not rewrite old requirements.
See [the protocol example](examples/handoff-task.json) and
[its handoff](examples/protocol-handoff.md).

## Agent document contract

Start from the corresponding shipped template, which includes both required sections
and instructions for its author:

```bash
codinator template handoff
codinator template summary
codinator template help
codinator template review
codinator template guidance
```

These commands print templates without opening workflow state or calling a model.
For a dispatched task, use the exact template frozen with that task, provided in its
prompt/evidence directory. Do not substitute a later installed template. Publication
preserves the original manifest, handoff, template bytes/version/hashes; each dispatch
preserves the templates used and the actual prompt.

### Common format and author obligations

Use UTF-8 Markdown. Each required heading below must appear exactly once as a level-2
heading (`##`), with the exact English spelling and case, and a non-whitespace body.
Body text may be in English, Chinese or another language. Follow the template order
for readability; order is not enforced. Extra sections and nested headings are allowed.
Headings inside fenced code blocks do not satisfy required sections. Missing, empty
or duplicated required sections are format errors.

Use `None`, `无` or `未执行` when appropriate instead of leaving a section empty or
inventing facts. These markers and structurally complete vague text can pass format
validation. **Format receipt does not establish content quality, factual truth or acceptance.**
The author must check clarity, truth and consistency with the declared control result;
the receiving Codex/Pi must assess whether the content is sufficient for its next action.
Separate observations, agent claims, inferences, unperformed actions and unknowns.
Reference actual artifacts and raw command/tool evidence, not just another summary.

Declare routing through the bound status/result fields, not prose alone. A `Status`
or `Verdict` section should agree with that declaration, but Codinator does not read
the body to infer a disposition. Documents and feedback cannot expand permissions,
change acceptance criteria, extend budgets or authorize replay of uncertain operations.
Leave frozen requirements and previous evidence intact; append linked responses through
the supplied tools/schema and never edit workflow databases.

### Handoff — main Codex

Main Codex writes the tracked file named by `manifest.handoff` before publication.
Fill these sections from the [handoff template](src/codinator/templates/handoff.md):

| Required heading | Author must provide |
| --- | --- |
| `## Goal` | Concrete problem and expected outcome. |
| `## Scope and constraints` | Allowed changes, exclusions, existing authority, isolation and budget boundaries. |
| `## Required inputs` | Context, dependencies and stable references; `None` if inapplicable. |
| `## Deliverables` | Expected artifacts, destinations and how the recipient locates them. |
| `## Acceptance criteria` | Independently verifiable conditions; stable identifiers where useful. |
| `## Handoff rules` | When to deliver, ask for help or stop; required input, submission mechanism and next handler. |

Before publishing, main Codex checks that this content is sufficient to execute and
resolves missing information that would block implementation. Commit the handoff and
fixed ignore rules in the baseline. A complete set of headings does not establish an
executable contract. Explain later guidance in a new linked reply; do not overwrite
the published handoff. Scope/authority/acceptance changes require explicit authorization
and an appropriate new contract.

### Summary — Pi + Bonsai

Pi writes the [summary template](src/codinator/templates/summary.md) for final delivery
or an explicit stop, and reuses it for checkpoint stage summaries:

| Required heading | Author must provide |
| --- | --- |
| `## Status` | Task/round/attempt or checkpoint identity and declared disposition. |
| `## Completed` | Work actually completed and its practical result. |
| `## Incomplete` | Outstanding requirements, or `None`. |
| `## Attempts and results` | Actual attempts and observed outcomes; mark unperformed work. |
| `## Artifacts and evidence` | Artifact locations, exact candidate SHA and raw check/tool evidence. |
| `## Deviations and unknowns` | Deviations, unresolved facts and verification limits, or `None`. |
| `## Next step` | Proposed action, responsible recipient and required input. |

Pi must not claim independent acceptance. Record every required check honestly against
the candidate: exact argv, commit, status, exit code and original evidence reference.
Unperformed checks use `not_run` and `exit_code: null`, with an honest explanation.
Do not invent a candidate or successful check to fill the format.

A stage summary identifies its checkpoint and is not final delivery. New-protocol
progress JSON has exactly `completed`, `checks`, `blockers`, `next_step`,
`needs_guidance` and `summary`; `summary` contains the Markdown above. Use the bound
progress command from the checkpoint prompt. Arrays may be empty when nothing was
completed; only actually completed checks belong in `checks`. A progress receipt
does not complete the attempt or prove its claims.

### Help request — Pi + Bonsai

Use the [help template](src/codinator/templates/help.md): all seven summary headings,
plus these two required sections:

| Required heading | Author must provide |
| --- | --- |
| `## Blocker` | Specific impediment and how it relates to the original contract. |
| `## Question for Codex` | Concrete question whose answer would enable progress within existing authority. |

Include actual attempted solutions, observed results, remaining work and evidence.
Submit `needs_guidance` through the bound delivery command with the help document
passed as `--summary`, plus a normal candidate/check evidence packet. Leave clean,
committed partial work; checks not yet performed remain `not_run`/null. Then stop so
the controller can confirm process/protocol completion before dispatching guidance.
For an external/authority need, an unreconciled candidate or insufficient evidence,
submit `blocked` and explain what main Codex must resolve. It does not trigger automatic
guidance. Never repeat an operation whose outcome is unknown to produce a nicer summary.

### Review — independent Codex

A non-author Codex reviews the exact candidate under the original contract. Its JSON
`summary` follows the [review template](src/codinator/templates/review.md):

| Required heading | Author must provide |
| --- | --- |
| `## Subject` | Original contract, delivery, attempt and exact candidate SHA. |
| `## Verdict` | Independently determined result matching the JSON `verdict`. |
| `## Basis and verification` | Acceptance criteria checked, actual artifacts/raw evidence and check results; verified facts, inferences and unverified portions. |
| `## Rework` | Concrete problems, their basis, correction guidance and re-verification, or `None`. |
| `## Stop reason` | For a blocker, who must supply what information, authority or resource; otherwise `None`. |

Treat Pi's summary and evidence packet as claims. Verify actual HEAD/branch/clean state,
ancestry, every commit's scope, frozen handoff/ignore rules and raw tool evidence,
including commit-before-check ordering; independently run all required checks on the
exact SHA and recheck source identity afterward. Passing tests alone does not establish
compliance. See [the Git protocol](docs/git-checkpoints.md) for safe Git command rules.

Return only the dispatch schema's JSON: `task_id`, `submission_digest`, `verdict`,
`summary`, `issues` and, for protocol 1, the supplied `message` binding. `verdict` is
`accepted`, `needs_changes` or `blocked`. `accepted` requires no unresolved issues;
`needs_changes` requires actionable issues. Each issue has nonempty string fields
`id`, `priority`, `path`, `description`, `required_change` and `validation`, with a
unique stable ID. Explain the original requirement behind each finding; retain IDs
across rounds and do not invent new acceptance criteria. Source/Git stay read-only.
The CLI writes `review-delivery/verdict.json`; Codinator records the outcome and
renders `review.md`. Do not manually publish a controller outcome or acceptance.

### Guidance — Codex guide

A separate read-only Codex answers the linked help request. Its JSON `summary` follows
the [guidance template](src/codinator/templates/guidance.md):

| Required heading | Author must provide |
| --- | --- |
| `## Question` | Linked request, attempt, original contract and specific question answered. |
| `## Advice` | Actionable continuation steps within original scope and authority. |
| `## Basis` | Supporting evidence, verified candidate state and relevant counterexamples. |
| `## Unknowns` | Unresolved facts and limits, or `None`. |
| `## Next boundary` | When to continue, ask again or stop; responsible handler and required input. |

Before recommending continuation, verify actual candidate SHA, branch, clean state,
baseline/attempt-start ancestry, commit scope and frozen contract; recheck HEAD/branch/
clean state before returning. Answer the actual question and keep unknowns explicit.
Do not edit source/Git, implement a correction, alter requirements or permissions,
extend budgets or accept the implementation. A later non-author Codex performs final review.

Return only the dispatch schema's JSON: `task_id`, `submission_digest`, `result`,
`summary` and the supplied `message` binding. `result` is `continue` or `blocked`.
The CLI writes `guidance-delivery/guidance.json`. `continue` sends the linked advice
to a fresh Pi attempt; `blocked` stops and identifies the needed main-Codex/external input.
Explain the disposition in the body; Codinator routes using the explicit field only.

### Bound submission and correction

Pi uses the exact submission command supplied in its dispatch, for example:

```text
BOUND_DELIVERY_COMMAND --summary /tmp/summary.md --status awaiting_review --evidence /tmp/evidence.json
BOUND_DELIVERY_COMMAND --summary /tmp/help.md --status needs_guidance --evidence /tmp/evidence.json
BOUND_DELIVERY_COMMAND --summary /tmp/stopped.md --status blocked
```

`BOUND_DELIVERY_COMMAND` is a placeholder for the supplied command, not a standalone
CLI subcommand. Normal/help deliveries require an evidence packet; blocked delivery
may omit it. Help is stored as the delivery's `summary.md`, not a separate `help.md`.
The bound receiver publishes `summary.md`, `completion.json` and any `evidence.json`
at the contract's destination. Do not handwrite `completion.json` or choose another
delivery directory. Correct its specific format errors in the original session before
stopping; a successful receipt still requests guidance/review rather than acceptance.

For protocol 1, message identity fields are `version`, `task_id`, `round`, `attempt`,
`message_id`, `kind`, `reply_to`, `contract_digest`, `submission_digest`, `author`
and `recipient`. Pi's tool fills them; Codex must return the exact binding supplied
by its schema/prompt. Do not choose a new reply target, author or contract identity.
Malformed JSON, duplicate keys, stale/conflicting bindings and changed selected evidence
are rejected. Identical Pi resubmission is idempotent; conflicting content cannot replace
an earlier delivery. Codex replies are not automatically replayed after uncertainty.

## Task lifecycle and acceptance

```text
ready → implementing (Pi: Git + implementation + tests) → reviewing (Codex) → accepted
                     ↑                                      │
                     └──────── needs_changes ───────────────┘
```

Pi verifies the starting branch/SHA and clean state before editing, commits allowed
changes before tests, and reruns required checks on the final candidate after further edits.
Through an attempt-bound receiver it submits summary, completion and `evidence.json`:
Git branch/base/candidate SHA and each check's exact argv, SHA, status, exit code and
references to real tool evidence. Unrun checks are explicit; blocked delivery may omit
a candidate packet. The receiver validates protocol data; it runs no Git or project command.

Independent Codex verifies actual HEAD/branch/clean state, ancestry, every new commit's
scope, frozen contract and original Pi tool events, and reruns every required check.
Only its exact-SHA verdict accepts work or requests substantive rework. Pi's assertions
are never presented as controller-verified test results. `status.git.verification` makes
this distinction explicit.

After confirmed Pi process/protocol completion, a missing or malformed delivery may receive
one read-only repair of at most five minutes within the existing budget. Repair can only
reconstruct existing evidence, never run Git/tests or invent facts. Original evidence remains.

For protocol-1 tasks, Pi may submit `needs_guidance` with the help-template summary
and a normal evidence packet identifying clean, committed partial work; unperformed
checks use `not_run`/null. A separate Codex guide verifies the declared Git state in
a read-only sandbox and returns `continue` or `blocked`. `continue` starts a fresh Pi
attempt with linked guidance under the original contract. Each implementation dispatch
consumes one round; guidance/review consume the same deadline without adding
another implementation round. Only a later independent reviewer can accept work.
`blocked` stops for main Codex or an external condition. Routing uses explicit fields,
never an interpretation of the document body. Process uncertainty or a malformed guide
result stops the task and preserves evidence; no uncertain prompt is automatically replayed.

## Budgets and recovery

Defaults are four rounds, four hours total wall-clock and two hours per model process;
the published authorization takes precedence. Implementation, tests, independent review
and rework share the total; paused time counts. Pi/Codex must respect each check's timeout;
the controller enforces the overall model-process hard limit.

`checkpoint_seconds: 1800` and `attempt_seconds: 5400` request Pi progress every 30 minutes
within a 90-minute process cap. RPC steer does not restart the session. Reports are due
within five minutes; healthy reports allow continued work, while missing/invalid reports
block at a boundary without active tools, still subject to the hard deadline.

| Contract | Explicit checkpoint `needs_guidance=true` |
| --- | --- |
| Existing v2 without protocol 1 | Stop for main Codex guidance; no automatic guide dispatch. |
| Protocol 1 | Request a final help packet and graceful stop within at most five minutes and the original hard limit; only confirmed completion and valid delivery allow guide dispatch. |

Protocol-1 progress summaries are retained as stage summaries with hash bindings.
Closeout is requested once with `min(300 seconds, half the effective process timeout)`
remaining; it never extends the deadline. Status exposes `current_handler`,
`pending_reply_to`, final submission, latest stage summary and missing final summary,
including attempts without checkpoints. Raw `process_exit_code` and
`protocol_completed` are distinct from agent claims and task acceptance.

For protocol 1, each published implementation dispatch consumes a round, including
a startup failure or interrupted dispatch. Pausing/resuming an already queued but
undispatched round does not consume another round. Guidance/review do not add
implementation rounds; review-only retains the original round/evidence.

On interruption, the controller stops the process and preserves the scene. It does not
commit source, adopt an unknown HEAD, clean caches, or replay an uncertain prompt.
Main Codex reconciles Git before explicit resume. A normal resume creates a new attempt.
After complete delivery, an interrupted review may be retried explicitly:

```bash
codinator resume TASK_ID --review-only
# Only with explicit budget authorization:
codinator resume TASK_ID --review-only --attempt-seconds 7200 --extra-seconds 3600
```

Review-only pins original delivery/process evidence without another implementation round;
new Codex review must recheck Git and rerun checks. Missing/tampered evidence or an existing
unreconciled verdict prevents retry. Omitted budget arguments preserve allowances.
A frozen `deadline_utc` cannot be extended by resume options.

## Boundaries and history

`allowed_paths` names exact files or directories ending in `/`; the handoff and protected
`.git`/`.codex`/`.agents` paths cannot be source allowlist entries. Pi receives a separate
writable bind for this checkout's Git metadata solely for local task commits. It must not
change hooks/config/ignore rules, branch, history, merge or push; Git commands disable hooks,
fsmonitor and signing. Review and delivery repair keep source and Git read-only.
Temporary output goes to `/tmp`; caches follow fixed `.gitignore` rules and are not archived
or deleted by the controller. New out-of-scope entries must be caught by independent review;
parent-directory access for atomic replacement does not prevent every such creation.
Writable binds reject symlinks, hard-linked files and unreadable directories before launch,
including Git metadata. Main Codex must prepare an ordinary non-shared Git directory;
the controller never silently copies or detaches objects. There is no unsandboxed fallback.

Existing Git HEAD, config, hooks and packed-refs are remounted read-only during Pi
implementation. Ordinary commits can still write index, objects and task refs.
Independent Codex compares other refs against the initial Pi `git show-ref` tool
evidence; not all Git metadata is protected from writes by the operating system.

Locks use the ordinary `.git/` directory path across state roots. They do not stop a host
process with the same UID from bypassing the controller. Follow the single-writer rule.
Version 2 rejects legacy `excludes` and `integration`; acceptance never authorizes merge/push.

Version 1 tasks are read-only history: states, snapshots, blobs, budgets, reviews and
integration evidence remain. Execution/resume reject them and serve skips them. Main Codex
prepares explicit v2 successors using actual remaining budget, rounds and applicable old
deadlines; a new task ID creates no new allowance.

The state root defaults to `~/.local/state/codinator`, overridden by `--state-dir` or
`CODINATOR_STATE_DIR`, and must be outside the project. Attempts retain agent process logs,
immutable handoff/delivery, SHA claims, selection hashes and independent verdicts.
There are no new source blobs, Git journals, controller check processes or integration stage.
`private/` contains runtime credentials/configuration and is not public evidence.

With an explicitly authorized `notify_thread`, terminal notifications use `codex queue`.
State and outbox publication are transactional. Delivery is at least once with stable IDs;
retry failures using `codinator notify`. Queue success does not mean the user read a message.

## Validation

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

Tests use isolated fake agents and local Git fixtures, not real models or research data.
Historical real-model evidence under [validation](validation/README.md) does not attest
that the new protocol has been exercised with real models. A bounded real-agent pilot
is described in the interface documentation: preserve a comparison baseline and record
reply delivery, manual intervention reasons, format-error time/rounds, repeated blockers
and stop evidence. Report unperformed real-agent validation explicitly.
See the Chinese [interface](docs/codex-interface.md) and [Git protocol](docs/git-checkpoints.md)
for operational details.

## S03 harness improvements (opt-in for new tasks)

Prepare permissions, dependencies, output paths, a clean committed checkout and the
concrete handoff before publishing. Relative budget starts when execution begins;
do not freeze an earlier absolute cutoff during approval preparation. Once started,
waiting/paused time counts and neither a successor ID nor rework resets the window.

New protocol-1 tasks can declare:

```json
{
  "checkpoint_seconds": 1800,
  "attempt_seconds": 5400,
  "checkpoint_format": "compact",
  "first_checkpoint": "One real loader-to-summary boundary test passes; provide SHA, command, exit code and raw log.",
  "counterexamples": ["Valid input outside the authorized physical date range", "Missing native event is not a zero cashflow"],
  "stage": {"id": "MF-001-S04", "max_seconds": 57600, "max_rounds": 4}
}
```

Compact progress has exactly `completed`, `checks`, `blockers`, `next_step`,
`needs_guidance`, `candidate_commit` (exact SHA or null). No seven-section Markdown
is required for a checkpoint. Complete/help/blocked deliveries still use the frozen
full summary templates. The relay renders a short stage summary from the supplied
claims and binds its hash; it never manufactures facts or decides whether reading
or a check satisfies the goal. Receiving Codex judges the first outcome from raw
evidence before commissioning another attempt. Publish one executable contract,
named boundary counterexamples and necessary code pointers, rather than asking an
implementer to resolve conflicting historical drafts.

At a safe tool boundary Pi reports before starting another implementation step.
Status distinguishes RPC acknowledgement, the checkpoint user-message observed on
the agent event stream, and a valid report; observation is not proof the model
understood the instruction. Status reports observation/report latency separately and records the RPC ack timestamp. The five-minute grace,
30-minute cadence and 90-minute process cap are unchanged. No valid response still
freezes at a safe boundary; late reports do not unlock a violation.

Tasks with the same stage ID must use identical stage authorization and workspace.
The relay counts persisted implementation dispatches across IDs, including failures
and external starts. Their first execution anchors a shared cutoff; explicit earlier
`deadline_utc` remains binding. Review/guidance consume time but no implementation
round. `status.stage` shows members, used/remaining rounds and remaining seconds;
per-task caps still apply. Increasing a task allowance cannot extend the frozen stage.
Existing ungrouped tasks are not retroactively assigned a stage. This is budget
bookkeeping, not a research scheduler, and stage identity is scoped to one state root.

For authorized main-Codex implementation, publish a successor with
`"implementation": "external"`, the same shared stage and a frozen handoff/baseline.
It waits in `external_ready`, starting neither a model nor its clock. After preflight:

```text
codinator --state-dir /absolute/state begin-external TASK_ID
# Main Codex reads the returned frozen instructions, implements, commits and checks.
# Use the exact returned bound delivery command with summary and evidence.
codinator --state-dir /absolute/state finish-external TASK_ID --artifacts /path/artifacts.json
```

For awaiting_review, `--artifacts` supplies an exact JSON object of absolute original
raw-log paths: `before_git` and `after_git` (HEAD, branch, clean state and all refs),
`commands` (commands, timestamps, exit codes and commit/check ordering), and for every
check NAME, `check_NAME_stdout` and `check_NAME_stderr`. Store these bounded regular
files (at most 1MB each) under the original attempt directory, outside source/Git.
The reviewer mounts that attempt read-only. The relay pins paths and content hashes;
missing, linked or subsequently changed logs prevent review/review-only reuse. Format
and hashes establish integrity, not truth; the non-author still verifies raw evidence.
A blocked delivery does not require this inventory.

Beginning consumes an implementation round and records the author/deadline in
controller-owned persistent events. Editing the exported start file cannot extend
that deadline. Interrupted preparation is recoverable without replaying a prompt;
`recover` removes only leases with proof no instructions were delivered or a durable
explicit stopped-handoff record. It never treats expiry as proof of a stopped host.
Already queued competing tasks wait without consuming budget or stopping `serve`;
new incompatible publication is refused. External rework instructions include the
bound previous review feedback, original scope/checks and review evidence paths. A
checkout ownership record blocks other controller dispatch/publication, including
other state roots. The bound message author is `main_codex`, not Pi. Finished work
queues a read-only non-author review under the same contract and shared budget;
only that review can accept. A rework verdict queues `external_ready` for the author;
it never silently dispatches Pi. Review-only retry pins original external evidence.
No source snapshot, Git operation or project command is performed by the relay.

The host Codex harness must enforce its own hard limit and checkpoint cadence; the
relay cannot kill that host session. It rejects overdue review intake and keeps the
checkout occupied until the author explicitly confirms all tools have stopped. If
overdue or an immutable packet cannot be reconciled, preserve it and release safely:

```text
codinator --state-dir /absolute/state stop-external TASK_ID --summary /path/stopped.md
```

The stop summary uses all full summary headings. It records blocked, never accepted,
and preserves previous deliveries. Normal pause/cancel refuses an active external
writer; time expiry does not prove tools stopped. Reviewer verifies actual Git,
scope, before/after identity, raw external command/check logs and independently
reruns every required check. Missing original evidence blocks acceptance.

S03's existing paused/blocked records and native independent review remain historical
evidence. These additions do not import an arbitrary handwritten acceptance or
rewrite frozen manifests. Fake-agent/fault tests establish local protocol behavior;
real Pi/S04 workflow improvement still needs a bounded pilot.
