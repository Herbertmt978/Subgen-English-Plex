# Generation-Bound Failure Markers v0.4.0 — Checkpoint

Updated: `2026-08-30`
State: `active`

## TodoCheckpointDraft

- Current todo: Task 2 — add the shared marker contract and bounded reader.
- Active slice: one new schema/reader owner plus focused contract tests; no monitor/runtime wiring yet.
- Completed todos: approved design amendment; implementation plan; execution snapshot; no-GitHub-runner route amendment; public issue.
- Evidence refs: commits `9b78174`, `658ebdc`, `3c98071`, `ec863fc`; clean TaskStartSnapshot in `10-intent.md`; [GitHub issue #6](https://github.com/Herbertmt978/Subgen-English-Plex/issues/6) read back with the approved public scope.
- Blocked on: nothing.
- Next step: implement `subgen_failure_markers.py` and its focused tests, then run the isolated pytest command.

## Slice Card

- Goal: create one bounded, fail-open marker schema/reader contract.
- Parent plan/spec: implementation plan Task 1 and approved design spec.
- Files: `subgen_failure_markers.py`, `tests/test_failure_markers.py`; no monitor, facade, scanner, Compose, or deletion edits.
- Boundary: exact `/media` path, five-field identity, bounded private registry read, deterministic schema, fail-open decision.
- Verification: isolated `tests/test_failure_markers.py`, diff check, and scoped commit readback.
- Stop: contract tests pass and the slice is committed without unrelated paths.

## BaselineUsageDraft

- Required refs: approved design, initial baseline, contribution rules.
- Acknowledged refs: all required refs.
- Cited refs: parent plan and `CONTRIBUTING.md`.
- Missing refs: none.
- Decision: `continue`.

## DriftCheckDraft

- Original intent: aligned.
- Goal/stop condition: aligned.
- Compatibility boundary: no runtime/monitor integration in this slice; existing deletion and APIs untouched.
- New owner/fallback/adapter: none.
- Retirement track: unchanged.
- Evidence sufficiency: issue traceability is complete; contract evidence is the next required check.
- Execution Readiness View: present and aligned.
- Decision: `continue`.

## ResumeStateHint

Read `10-intent.md`, this checkpoint, the approved design, and the parent plan. Confirm the worktree still matches the TaskStartSnapshot before source edits.
