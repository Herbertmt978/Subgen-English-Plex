# Generation-Bound Failure Markers v0.4.0 — Checkpoint

Updated: `2026-08-30`
State: `active`

## TodoCheckpointDraft

- Current todo: Task 1 — open and verify the repository-required public issue.
- Active slice: issue traceability before production source code.
- Completed todos: approved design amendment; implementation plan; execution snapshot; no-GitHub-runner route amendment.
- Evidence refs: commits `9b78174`, `658ebdc`, `3c98071`; clean TaskStartSnapshot in `10-intent.md`; amended design/plan diff and incomplete-token scan passed.
- Blocked on: nothing.
- Next step: commit the plan/work-record correction, then create and verify the scoped issue.

## Slice Card

- Goal: establish the required public issue without exposing private deployment data.
- Parent plan/spec: implementation plan Task 1 and approved design spec.
- Files: no production files; GitHub issue state only.
- Boundary: exact-generation marker, first-failure skip, public delete off, Plex threshold-one policy, v0.4.0, and no hosted runners.
- Verification: `gh issue view` scope/readback.
- Stop: issue exists once with correct public content.

## BaselineUsageDraft

- Required refs: approved design, initial baseline, contribution rules.
- Acknowledged refs: all required refs.
- Cited refs: parent plan and `CONTRIBUTING.md`.
- Missing refs: none.
- Decision: `continue`.

## DriftCheckDraft

- Original intent: aligned.
- Goal/stop condition: aligned.
- Compatibility boundary: no runtime change in this slice.
- New owner/fallback/adapter: none.
- Retirement track: unchanged.
- Evidence sufficiency: amended route is durable and enough to proceed to the issue; implementation evidence has not started.
- Execution Readiness View: present and aligned.
- Decision: `continue`.

## ResumeStateHint

Read `10-intent.md`, this checkpoint, the approved design, and the parent plan. Confirm the worktree still matches the TaskStartSnapshot before source edits.
