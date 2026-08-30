# Generation-Bound Failure Markers v0.4.0 — Checkpoint

Updated: `2026-08-30`
State: `active`

## TodoCheckpointDraft

- Current todo: Task 6 — run full local verification and simulator-only Docker/image smokes.
- Active slice: fresh full regression, compile/Compose/static checks, then idle-simulator Linux tests, image build/boot, disposable marker/replacement/delete smokes, and conditional simulator shutdown; no GitHub publication yet.
- Completed todos: approved design amendment; implementation plan; execution snapshot; no-GitHub-runner route amendment; public issue; shared marker contract and bounded reader; monitor marker-before-delete producer; canonical pre-probe runtime enforcement; v0.4.0 packaging/docs/workflow/version/ADR surface.
- Evidence refs: commits `9b78174`, `658ebdc`, `3c98071`, `ec863fc`; clean TaskStartSnapshot in `10-intent.md`; [GitHub issue #6](https://github.com/Herbertmt978/Subgen-English-Plex/issues/6) read back with the approved public scope.
- Blocked on: nothing.
- Next step: commit the verified package/docs slice, run the final local matrix, then activity-check and use the simulator without invoking GitHub Actions.

## Slice Card

- Goal: produce fresh local and Linux/Docker simulator evidence for the exact release tree without using hosted runners or live Plex for pre-release tests.
- Parent plan/spec: implementation plan Task 6 and approved design spec.
- Files: no intended source edits; verification fixes return to their owning slice.
- Boundary: disposable temporary media only, no production media, no GitHub Actions, no concurrent simulator workload, no simulator shutdown unless this task woke it and the final activity check is clear.
- Verification: full pytest, compile, all Compose configs, workflow/version/static checks, Linux suite, packaged/source HTTP boot, and disposable marker/replacement/delete sequencing.
- Stop: every check passes, simulator lifecycle is accounted for, and the exact verified commit is ready for a fast-forward publication.

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
- Evidence sufficiency: issue, contract, monitor sequencing, runtime enforcement, and distribution/version consistency are complete; simulator Linux/image evidence is next.
- Execution Readiness View: present and aligned.
- Decision: `continue`.

## ResumeStateHint

Read `10-intent.md`, this checkpoint, the approved design, the parent plan, and `90-evidence.md`. Verify the package/docs commit, then follow Task 6's simulator activity/wake/shutdown rules exactly.
