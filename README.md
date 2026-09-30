# Codinator

[Chinese version](README.md)

## Overview

Codinator was designed to enable **collaboration between Codex and Pi: a stronger model working with a weaker model**.
Codex uses a strong model for requirements analysis, task planning, and independent acceptance review.
Pi runs a weaker model (currently Bonsai) to implement code and make revisions based on review feedback.
The controller connects implementation, checks, review, and rework into a persistent, traceable workflow,
reducing the effort of manually dispatching tasks, tracking progress, and passing feedback between agents.

**Codex is the only interactive interface.** Use the main Codex session to discuss requirements,
publish tasks, inspect status, and handle exceptions. An independent background service runs
Pi + Bonsai implementation, required checks, independent Codex review, and automatic rework.
Closing the main interface does not pause background tasks. Only an independent review verdict can accept work.

The project supports task pausing and explicit recovery, modification scope and execution budgets,
sandbox isolation, and evidence preservation for each attempt. With explicit authorization,
it can also commit accepted changes and merge them by fast-forward.

## Requirements

Designed for Linux, a single user, and serial task execution. Requires Python 3.11+,
installed `git` and `bwrap`, and installed, authenticated `pi` and `codex` clients.
The Python runtime has no third-party dependencies.
The controller dispatches Pi implementation through RPC and Codex acceptance through a separate review process.

## Installation and service setup

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --no-index --no-build-isolation --no-deps -e .
.venv/bin/codinator doctor

mkdir -p ~/.config/systemd/user
.venv/bin/codinator service > ~/.config/systemd/user/codinator.service
systemctl --user daemon-reload
systemctl --user enable --now codinator.service
systemctl --user is-active codinator.service
```

`doctor` checks local dependencies and sandbox availability without calling models.
`service` only prints a unit file, capturing the current PATH and proxy environment.
For an existing installation, inspect active tasks and configuration first, then update and restart when the service is idle.
If a proxy is required, set it when generating the unit:

```bash
env http_proxy=http://127.0.0.1:8888 https_proxy=http://127.0.0.1:8888 \
    .venv/bin/codinator service > ~/.config/systemd/user/codinator.service
```

Adjust the address for your environment. Pi + Bonsai subprocesses remove proxy variables and connect directly
to the local service; Codex review and notification processes retain the service's proxy settings.
Global proxy and client configuration remain unchanged. Implementation is fixed to
`bonsai / bonsai2-27b / xhigh`, and review to `gpt-6-astra / xhigh`.
The controller verifies and records Pi's actual client identity through RPC, rather than relying on a model's self-description.

## Managing tasks from Codex

Copy [examples/task.json](examples/task.json) and set the actual workspace, handoff document,
allowed paths, and check commands. Use the [sample handoff](examples/demo-handoff.md) to prepare
a task in an empty, separate repository. The workspace must be the root of a separate Git repository
or worktree. Publication captures its baseline, including uncommitted changes.

```bash
.venv/bin/codinator submit /absolute/path/to/task.json
.venv/bin/codinator status TASK_ID
.venv/bin/codinator pause TASK_ID
.venv/bin/codinator resume TASK_ID
.venv/bin/codinator cancel TASK_ID
```

`submit` explicitly authorizes dispatch; the service only processes published tasks.
`status` is read-only. It reports the state, reason, round, attempt, evidence paths, and latest review
with its attempt identifier. It never resumes a task or starts a model.
`serve` is the controller entry point for the independent user service. For everyday use,
the main Codex session invokes the management commands above.
See the [Codex interface protocol](docs/codex-interface.md) (Chinese) for operations and troubleshooting.

## Execution and recovery

```text
ready → implementing → checking → reviewing → accepted
             ↑                       │
             └──── needs_changes ────┘
```

When required checks fail or independent review requests changes, the controller automatically
schedules Pi rework within the remaining round budget. Defaults are four implementation rounds,
four hours of total wall-clock time, and two hours per Pi or Codex process.
Implementation, checks, review, and rework share the total budget; time spent paused also counts.
Repeated issue IDs do not stop rework early. Connection failures, truncated output, missing delivery,
out-of-scope changes, and untrustworthy verdicts block the task. Prompts with uncertain execution outcomes are never replayed.

A normal `resume` creates a new implementation attempt and preserves previous evidence.
If Pi completed delivery and all checks passed, but Codex review was interrupted by quota,
connectivity, or process failure, you can explicitly retry only the review:

```bash
codinator resume TASK_ID --review-only
# Explicitly adjust budgets when needed:
codinator resume TASK_ID --review-only --attempt-seconds 7200 --extra-seconds 3600
```

Review-only recovery verifies the original delivery, check evidence, and frozen snapshot,
then creates a new review attempt without increasing the implementation round.
It rejects missing or damaged evidence, workspace changes, failed checks, incomplete implementation,
and verdicts already written to disk but not yet recorded by the controller.
If the new review requests changes, the service schedules the next implementation round and its checks.

`--attempt-seconds` overrides the time limit for subsequent individual model processes.
`--extra-seconds` extends the overall deadline; if it has already expired, the added time starts at recovery.
Omitting these options preserves the existing budgets. Individual check timeouts remain unchanged.
Budget updates are recorded atomically in SQLite events, and each new attempt's `budget.json` records
the effective values. The original manifest and previous evidence are not rewritten.

The service examines interrupted work on startup. Manually running `recover` also only performs
recovery checks; it does not dispatch tasks. Inspect the reason and evidence before explicitly resuming.
If the workspace differs from the recorded snapshot, inspect and restore it manually or publish a new task.
The controller does not automatically reset, stash, or roll back changes.
Increasing timeouts cannot fix proxy, quota, or connectivity problems.

A manifest can explicitly authorize Pi to commit and fast-forward merge accepted changes through `integration`.
The controller verifies the accepted snapshot and complete commit tree, promotes the same commit,
and marks the task `accepted` only after the merge finishes. It never pushes automatically.
Without this configuration, the task ends when review accepts it.
See [integration after acceptance](docs/codex-interface.md#验收后由-pi-提交并合并) (Chinese) for authorization and recovery rules.

## Upgrade boundaries

This version removes the native Pi terminal interface, the foreground `run` command,
and the standalone Codex review probe. Status queries no longer accept `--mode`,
and their results no longer contain a `mode` field. All operations use the background task state root.
Existing `interactive/` directories, task contracts, and attempt evidence remain outside the repository;
they are not automatically migrated or redispatched.
Before upgrading, end old interface sessions and inspect outstanding tasks. Do not point the background
service at their state directories or republish a workspace that is still being executed.

## Task contracts

`handoff` is a read-only contract and must not appear in `allowed_paths`.
The implementer writes a separate summary for each attempt. SQLite is the sole source of workflow state;
this version does not automatically update status text in project READMEs or handoff documents.

- `allowed_paths`: exact files, or directories ending in `/`. Wildcards and `.git`, `.codex`, and `.agents` are forbidden.
- `checks[].argv`: an argument array used to launch a process directly, without an implicit shell. Checks run against a read-only workspace; write temporary output to `/tmp`.
- `excludes`: environment or cache paths omitted from content snapshots. They cannot cover tracked files or overlap the allowed modification scope.
- `max_rounds`, `max_seconds`, `attempt_seconds`: execution limits established before publication.
- `notify_thread`: an optional Codex session ID explicitly selected by the user. Without it, notifications remain local and are not sent externally.

If a task needs a separate worktree, create it manually or through Codex, establish its environment
and baseline, then publish the task. Codinator does not automatically create worktrees or relocate
virtual environments, avoiding implicit loss of uncommitted files or broken absolute-path contracts.

## Evidence and permissions

The default state directory is `~/.local/state/codinator`; override it with `--state-dir`
or `CODINATOR_STATE_DIR`. It must be outside the task workspace and should not be committed to Git.
The directory permissions are 0700.

```text
state.sqlite                         State, rounds, notification outbox
blobs/<sha256>                       Deduplicated, immutable file contents
tasks/<task>/
  manifest.json / intake.json         Published contract and complete baseline snapshot
  handoff.md                         Original contract text at publication
  attempt-0001/
    implementation.json             Raw workspace after Pi delivery, before cache archival
    before.json / submission.json    File contents, types, modes, Git HEAD/index
    diff.json                        Changed paths and snapshot digest
    pi-runtime.json                  Actual client model identity reported by RPC
    pi/                              Raw RPC, stderr, process and exit records
    delivery/summary.md               Pi changes, deviations, and items not run
    delivery/completion.json          Submission identifier for this attempt
    checks/<name>/                   Controller-run commands, exit codes, raw output
    codex/                           Raw independent review events and exit records
    review-delivery/verdict.json      Structured verdict bound to the submission digest
    outcome.json / review.md          Final outcome or rework requirements
private/                             Runtime configuration, sessions, credential copies; not public evidence
notifications/                       Each notification delivery attempt
```

Development commands are recorded in Pi's raw tool events. Required controller checks have their own
exact exit codes and output digests. Arbitrary command output is never presented as structured test counts.
Snapshots cover tracked, untracked, and ordinary ignored files, directories, deletions, modes, and symlinks.
Explicitly excluded environment or cache contents have no integrity guarantee. Symlinks are not followed when reading content.

Pi runs inside bubblewrap. Host files are read-only by default, as are Git metadata and existing files
outside the allowed scope. Parent directories needed for allowed files are writable to support atomic replacement.
New out-of-scope entries are rejected by a subsequent scope check; this does not prevent every unauthorized
new file from being created. Scope violations preserve the workspace and stop execution rather than deleting files.
After complete background delivery, the controller preserves the raw workspace, then archives only strictly
recognized new Python caches. It performs no cleanup when other scope violations exist or during frozen checks and review.
Incomplete protocols and interrupted attempts still require inspection of the original evidence and snapshots;
see the interface protocol for details.

Codex also has outer process isolation and a read-only workspace; its CLI explicitly uses
`--sandbox read-only` and `-a never`. Both agents use parent-exit cleanup and PID namespaces.
There is no automatic fallback to unrestricted execution. This is engineering isolation for a single user,
not a security boundary against malicious host processes running under the same UID.

## Background service and notifications

The service only executes tasks explicitly published to its configured state directory.
Separate state directories require corresponding service arguments. Generated units retain the PATH
and proxy environment present at generation time, so background Codex processes keep their proxy access;
Pi continues to connect directly. Execution after logout depends on the local user-service and linger settings.
Codinator does not change system-wide policies.

When `notify_thread` is configured, `codex queue` enqueues acceptance or blocked-state notifications.
The terminal SQLite state and notification outbox entry are committed in one transaction.
Delivery failures do not roll back acceptance; retry with `codinator notify`.
Delivery uses at-least-once semantics and stable event IDs. A crash can cause duplicate delivery,
so recipients should deduplicate by ID. Successful CLI enqueueing does not mean the user has read the message.
Automatic review does not depend on notifications or the current chat window.

## Validation and maintenance

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests use explicitly identified fake agent subprocesses and do not call models.
They cover rework, recovery, truncation, failing exits, concurrent modification, scope violations,
notification retries, identity checks, evidence publication, and background process cleanup.
Real-model and sandbox integration evidence is recorded separately under `validation/`;
mock tests cannot substitute for it. See the [validation records](validation/README.md) (Chinese)
for verification methods and the limits of historical evidence.
Complete runtime state may contain credentials and raw tool output. Share only the necessary, sanitized evidence.
