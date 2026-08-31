# Memory-Aware Segmented Transcription v0.5.0 - Checkpoint

- Task ID: 2026-08-31-memory-aware-segmented-transcription-v0.5.0
- Current todo: Open public v0.5 issue, then implement and review pure resource policy
- Active slice: Plan Task 1 followed by Task 2
- Blocked on: none
- Next step: Verify planning delta, commit it, create issue, then dispatch the Task 2 implementer

## DriftCheckDraft

- Scope status: Approved v0.5 scope unchanged
- Compatibility status: HTTP/upload/output/marker and invalid-only deletion boundaries explicit
- Retirement status: Generic/crash monitor deletion and repair deletion scheduled for retirement
- New risk signals:
- Pre-existing Aegis workspace format drift remains outside this feature scope
- Advisory decision: continue

## Checkpoint Update

- Current todo: Open public v0.5 issue, then implement and review pure resource policy
- Active slice: Plan Task 1 followed by Task 2
- Completed todos:
- Approved design and independently reviewed executable plan
- Evidence refs:
- plan-review
- workspace-preflight
- Blocked on: none
- Next step: Commit planning records, open issue, dispatch Task 2 implementer

## DriftCheckDraft

- Scope status: Issue matches approved v0.5 scope
- Compatibility status: No runtime or compatibility change in issue slice
- Retirement status: Invalid-only deletion and repair retirement remain explicit
- New risk signals:
- none
- Advisory decision: continue

## Checkpoint Update

- Current todo: Implement and review pure resource policy
- Active slice: Plan Task 2: pure resource and adaptive policy
- Completed todos:
- Approved design and independently reviewed plan
- Opened GitHub issue 7
- Evidence refs:
- github-issue-7
- Blocked on: none
- Next step: Dispatch Task 2 implementer with base 52ceff9

## Checkpoint Update - Frigate Deployment Amendment

- Current todo: Correct and independently re-review the Frigate/shared-GPU amendment
- Active slice: Plan amendment before Task 2 finalization
- Completed todos:
- Retired the Plex-hosted Subgen container and monitor while preserving Compose, model cache, marker/state data, prior image, and a private recovery manifest
- Verified Plex remained healthy with HTTP 200 and no Subgen process or port 9000 listener
- Selected Frigate as the canonical deployment target and recorded the existing v0.3.0 rollback boundary
- Unloaded an indefinitely pinned `qwen3:8b` model, increasing free RTX 3090 VRAM from about 11.1 GiB to about 17.4 GiB without deleting the model
- Verified the post-balloon Frigate baseline at a 20 GiB floor: all 15 cameras live, Frigate and Subgen up with zero post-boot restarts, no loaded Ollama model, no disk errors, and about 7.5 GiB guest `MemAvailable`
- Recorded about 18.1 GiB free RTX 3090 VRAM after reboot with Frigate and legacy Subgen resident, but no passive proof of maximum incremental higher-priority demand; live v0.5 deployment remains blocked pending that evidence
- Evidence refs:
- plex-subgen-retirement
- frigate-postboot-baseline
- frigate-gpu-amendment-review
- corrected-frigate-gpu-amendment-review
- Blocked on: P1 ModelEnvelope artifact/bootstrap, admission-margin, OCI identity-chain, and legacy repair-timer corrections from the second amendment review
- Next step: Re-review the corrected amendment; then resume Task 2 while keeping live deployment blocked until the higher-priority GPU reserve is proven

## Checkpoint Update - Amendment Approved

- Current todo: Implement and review Task 2A, the external ModelEnvelope catalog and runtime identity contract
- Active slice: Approved amended plan Task 2A
- Completed todos:
- Corrected and independently approved the Frigate/shared-GPU amendment after four review loops
- Defined large-v3-first exact-image profiling, owner-only catalog/identity artifacts, fail-closed shared-CUDA admission, and a 12 GiB profiling-only to 10 GiB production requalification boundary
- Preserved public fallback behavior, Frigate v0.3.0 operational rollback, public v0.4.1 rollback, and Plex retirement
- Evidence refs:
- frigate-gpu-amendment-final-approval
- Blocked on: none for local implementation; live Frigate deployment remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Implement Task 2A without modifying the partial Task 2B files

## User Requirement - Human GitHub Release Notes

- Task 8 must produce a human-written GitHub v0.5.0 release body rather than a generated commit list
- It must clearly compare v0.4.0, v0.4.1, and v0.5.0; separate public defaults from the Frigate deployment; and explain upgrade, rollback, compatibility, and deletion safety in ordinary user language
- Task 12 must publish the reviewed repository release-notes file without substituting generated text

## Checkpoint Update - Task 2 Review Corrections

- Current todo: Correct and independently re-review Task 2A and Task 2B
- Active slice: Task 2A catalog hardening followed by Task 2B resource-policy hardening
- Completed todos:
- First Task 2A implementation and independent specification review
- First Task 2B implementation plus independent specification and quality reviews
- Evidence refs:
- task-2a-first-review
- task-2b-first-review
- Blocked on:
- Task 2A must close immutable revision, measurement-invariant, bounded-read, ownership, and validated-construction findings
- Task 2B must close validated-envelope, fresh-admission, canonical-reserve, recovery, numeric-boundary, distinct-sample, OOM-telemetry, and concurrency findings
- Complexity decision: extract only bounded platform/cgroup/GPU readers and parsers to `subgen_core/resource_probes.py`; keep all resource policy and arithmetic in `subgen_core/resource_management.py`
- Next step: Finish both corrections, run focused local checks, and obtain fresh independent approvals before either implementation commit

## DriftCheckDraft

- Scope status: The probe split implements the approved resource-discovery seam and adds no user-visible behavior or new policy owner
- Compatibility status: Explicit-model authority, shared-CUDA fail-closed admission, and public fallback contracts remain unchanged
- Retirement status: No fallback, adapter, or duplicate policy path was introduced; the leaf probe module has no policy responsibility
- Test status: Focused resource verification now covers both resource test files
- Advisory decision: continue

## Checkpoint Update - Task 2A Complete

- Current todo: Correct, verify, and independently re-review Task 2B
- Active slice: Task 2B resource and adaptive policy
- Completed todos:
- Task 2A immutable catalog, runtime identity, canonical serialization, strict matching, and owner-only artifact boundary
- Task 2A specification and security reviews after adversarial correction loops
- Task 2A local commit `18f92b2` with only its source and focused test file
- Evidence refs:
- task-2a-final-local-verification
- task-2a-final-spec-review
- task-2a-final-quality-review
- Blocked on:
- Task 2B exact-resolution provenance, canonical recovery-candidate retention, controller-state admission, distinct polling, PSI parsing, and bounded clock findings
- Linux simulator execution of Task 2A's 22 POSIX filesystem/resolver tests remains a Task 10 release gate
- Next step: Freeze a corrected Task 2B snapshot, rerun focused local checks, and obtain fresh independent specification and quality approvals

## DriftCheckDraft

- Scope status: Task 2A stayed inside the approved catalog/identity owner and Task 2B remains inside the approved resource-policy owner plus probe leaf
- Compatibility status: Public fallback and canonical shared-CUDA fail-closed behavior remain explicit; no live runtime consumes the new code yet
- Retirement status: No temporary compatibility path was added
- Test status: Task 2A local verification is complete; POSIX filesystem semantics remain explicitly deferred to Task 10
- Advisory decision: continue
