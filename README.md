# Codinator

[中文文档](README_CN.md)

Codinator provides the handoff protocol and background transport between main Codex,
Pi + Bonsai, and independent Codex review. Main Codex owns requirements, planning,
handoffs and corrective guidance. Pi implements, runs project commands/tests and makes
local Git commits. Independent Codex verifies the work and decides acceptance.

**The controller does not implement project work, run project tests or invoke Git.**
It dispatches agents, transports immutable requirements and results, preserves process
evidence, manages state and budgets, and serializes writers.

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
Implementation is fixed to `bonsai / bonsai2-27b / xhigh`, review to `gpt-6-astra / xhigh`.
Pi RPC attests client configuration, not the weights loaded by the remote server.

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
Both agents read the controller's immutable copy of the published handoff.
The publication SHA is a publisher assertion, verified against real Git by Pi/Codex.

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

## Budgets and recovery

Defaults are four rounds, four hours total wall-clock and two hours per model process;
the published authorization takes precedence. Implementation, tests, independent review
and rework share the total; paused time counts. Pi/Codex must respect each check's timeout;
the controller enforces the overall model-process hard limit.

`checkpoint_seconds: 1800` and `attempt_seconds: 5400` request Pi progress every 30 minutes
within a 90-minute process cap. RPC steer does not restart the session. A valid report is
due within five minutes; missing reports or `needs_guidance=true` block at a boundary with
no active tool, still subject to the hard deadline. Healthy reports permit continued work.
Main Codex supplies guidance; the controller does not generate a plan or diagnosis.

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
that the new protocol has been exercised with real models.
See the Chinese [interface](docs/codex-interface.md) and [Git protocol](docs/git-checkpoints.md)
for operational details.

Existing Git HEAD, config, hooks and packed-refs are remounted read-only during Pi implementation.
Ordinary commits can still write index, objects and task refs. Other refs are compared by independent
Codex against the initial Pi `git show-ref` tool evidence; not all Git metadata is OS-write-protected.
