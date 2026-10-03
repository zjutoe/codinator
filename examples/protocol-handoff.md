# Demo: explicit technical help followed by integer addition

## Goal

Implement `add.py` with `add(a, b) -> int`. Both inputs must satisfy `type(value) is int`;
reject booleans and other types with `TypeError`. Return the exact sum, including zero,
negative and arbitrarily large integers. Use only the Python standard library, with
no I/O or global state. This bounded pilot exercises a real technical-help exchange.

## Scope and constraints

Only `add.py` and `tests/test_add.py` are writable. Keep this handoff and fixed ignore
rules unchanged. Only local commits on the declared task branch are authorized. No
branch/config/hooks/ignore changes, history rewrite, merge, push, model switch, or
background execution. All phases share the published remaining budget and fixed deadline.

## Required inputs

The published manifest supplies branch, baseline SHA, allowed paths, check argv and
budget. The attempt-bound submission tool supplies document identities and destinations.
Read its immutable contract and frozen summary/help templates before submitting.

## Deliverables

First submit one help request from the clean baseline, asking Codex to explain why
`isinstance(True, int)` is insufficient for this contract and how to enforce strict
integer inputs. Use `needs_guidance`, the help-template summary, and evidence identifying
the unchanged baseline candidate. Checks are not yet executed: use `not_run`/null and
an honest reason. Then stop. This request is a deliberate pilot step, not a claim that
you lack authority or that implementation is complete.

After linked Codex guidance arrives in a fresh attempt, implement the function and
meaningful independent tests; commit before checks and deliver normal final evidence.
Do not repeat the pilot help step when the prior guidance is supplied.

## Acceptance criteria

Exact sums, strict type rejection, no unintended side effects, meaningful boundary
tests, the published check passing on the final exact SHA, and changes within scope.
An actual linked help → Codex guidance → new Pi attempt exchange must precede final
delivery. The final independent Codex verifies actual Git, tools and check evidence.
No new acceptance requirements may be invented between attempts.

## Handoff rules

Use the attempt's bound DELIVERY command with `--summary`, `--status` and `--evidence`.
Format receipt is not content approval or acceptance. Authors must distinguish actual
results from attempts, unperformed checks and unknowns. For an external/authority need,
uncertain execution or an unclean candidate, submit honest `blocked` and stop. Main Codex
reconciles the scene before any explicit recovery; no operation is blindly replayed.
