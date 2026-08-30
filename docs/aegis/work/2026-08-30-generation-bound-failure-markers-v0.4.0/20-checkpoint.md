# Generation-Bound Failure Markers v0.4.0 — Checkpoint

Updated: `2026-08-30`
State: `active`

## TodoCheckpointDraft

- Current todo: Task 7 — publish v0.4.0 and GHCR from the simulator-built candidate without GitHub-hosted runners.
- Active slice: verify live remote divergence/run history, fast-forward the verified commit to `main`, create the tag/release, authenticate securely on the simulator, push both image tags, and verify one immutable registry digest.
- Completed todos: approved design amendment; implementation plan; execution snapshot; no-GitHub-runner route amendment; public issue; shared marker contract and bounded reader; monitor marker-before-delete producer; canonical pre-probe runtime enforcement; v0.4.0 packaging/docs/workflow/version/ADR surface; full Windows and Linux regression; simulator Compose/build/HTTP/disposable-marker smokes.
- Evidence refs: commits `977bb96`, `4fcd8fb`, `c8403bd`, `4b2d4c1`, `b53151b`, `6ae937f`; clean TaskStartSnapshot in `10-intent.md`; [GitHub issue #6](https://github.com/Herbertmt978/Subgen-English-Plex/issues/6); complete pre-publication evidence in `90-evidence.md`.
- Blocked on: nothing.
- Next step: commit this verification evidence, inspect live GitHub state without invoking a workflow, then follow Task 7's fast-forward/tag/release and simulator-local GHCR publication sequence.

## Slice Card

- Goal: publish exactly the verified v0.4.0 tree and simulator-built image without allocating a GitHub runner.
- Parent plan/spec: implementation plan Task 7 and approved design spec.
- Files: GitHub `main`, tag/release metadata, simulator candidate tags, and GHCR manifests; no intended source changes.
- Boundary: normal fast-forward only, no pull request, no workflow invocation, no force-push, no credential output or plaintext token file, and no simulator shutdown because it was already online before this task.
- Verification: remote main/tag identity, manual-only workflow readback, zero hosted runs for the release commit/tag/release, shared GHCR digest, image label/status smoke, and retained simulator candidate identity.
- Stop: main/tag/release and both GHCR tags resolve to the verified commit/image with no hosted run, ready for immutable Plex deployment.

## BaselineUsageDraft

- Required refs: approved design, initial baseline, contribution rules.
- Acknowledged refs: all required refs.
- Cited refs: parent plan and `CONTRIBUTING.md`.
- Missing refs: none.
- Decision: `continue`.

## DriftCheckDraft

- Original intent: aligned.
- Goal/stop condition: aligned.
- Compatibility boundary: package preserves deletion-off public defaults, stable APIs, and prior-runtime rollback compatibility; verification does not mutate production.
- New owner/fallback/adapter: none.
- Retirement track: unchanged.
- Evidence sufficiency: issue, contract, monitor sequencing, runtime enforcement, distribution/version consistency, complete local/Linux regression, image build, both HTTP boots, and disposable destructive sequencing are complete; live publication evidence is next.
- Execution Readiness View: present and aligned.
- Decision: `continue`.

## ResumeStateHint

Read `10-intent.md`, this checkpoint, the approved design, the parent plan, and `90-evidence.md`. Verify remote state and hosted-run history, then follow Task 7 without invoking GitHub Actions or exposing registry credentials.
