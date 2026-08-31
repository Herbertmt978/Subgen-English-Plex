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

## Checkpoint Update - Task 2B Complete

- Current todo: Implement and independently review Task 2C, the isolated ModelEnvelope profiler
- Active slice: Plan Task 2C only; no runtime integration or live Frigate change
- Completed todos:
- Task 2B resource discovery, host/cgroup/GPU admission, mandatory reserves, exact-device stabilization, pressure/recovery control, allocation-failure handling, and adaptive chunk-duration policy
- Public CUDA auto is limited to the conservative `small` fallback without stabilization; contradictory telemetry fails closed; canonical shared CUDA requires exact-device stabilization and an explicit audited reserve
- Recognized explicit models retain their fixed identity and may use conservative fallback budgets, but still require stabilization, fresh admission, and explicit controller authority
- Task 2B commit `601efdd` contains only the four planned source and test files
- Evidence refs:
- task-2b-final-local-verification
- task-2b-final-spec-review
- task-2b-final-quality-review
- Blocked on: none for Task 2C; Task 10 still owns Linux execution of POSIX-only catalog tests, and live Frigate promotion remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Implement `profile_model_envelopes.py` and its focused tests without duplicating admission arithmetic or authorizing production from profiling-cap evidence

## DriftCheckDraft

- Scope status: Task 2B remained within the approved resource-policy owner and bounded platform-probe leaf
- Compatibility status: Explicit-model authority, conservative public fallback, and canonical shared-CUDA fail-closed behavior match the amended design; no live runtime consumes Task 2B yet
- Retirement status: No alternate admission path, hidden model downgrade, or hosted-runner dependency was introduced
- Test status: 232 focused tests and 714 full-suite tests passed locally; 82 expected skips remain for platform/dependency-specific coverage
- Advisory decision: continue

## Checkpoint Update - Task 2C Complete

- Current todo: Integrate model selection and safe pressure release in Task 3
- Active slice: Plan Task 3 only; live Frigate and retired Plex deployments remain untouched
- Completed todos:
- Task 2C isolated owner-operated profiler with one explicit model per process, pinned immutable model revision, exact policy/workload validation, three or more cold cycles, paired incremental peaks, verified backend unload, and staged owner-only catalog output
- Fresh initial and per-cycle host/cgroup/exact-GPU admission remains owned by Task 2B; missing/stale/inconsistent telemetry is fatal and cannot authorize model descent
- Safe model-specific capacity/allocation failure is uniquely exit code 3 with bounded JSON; ordinary usage/configuration failures return 1 and cannot masquerade as clean-process fallback authority
- Automatic chunk policy resolves through the Task 2B capacity tier; the disposable fixture must equal one worst-case working chunk, including both five-second overlap guards
- The ordered 12 GiB to 10 GiB regression proves profiling evidence is not production authority and lower-model profiling requires a distinct safe-failure process handoff
- Task 2C commit `21fbbf9` contains only the profiler and focused tests
- Evidence refs:
- task-2c-final-local-verification
- task-2c-final-spec-review
- task-2c-final-quality-review
- Blocked on: none for Task 3; Task 10 still owns POSIX/Linux and packaged-runtime execution, Task 11 owns exit-3 process-destruction handoff, and live Frigate promotion remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Implement Task 3 model selection, two-phase inference admission, single-flight pressure release, fresh in-gate reload checks, and idle resident-model observation

## DriftCheckDraft

- Scope status: Task 2C is an isolated profiler and imports neither scanner nor worker entry points; it writes only a distinct staged catalog through Task 2A
- Compatibility status: Public fallback and canonical shared-CUDA behavior are unchanged because no live runtime consumes the profiler yet
- Retirement status: No image rebuild path, direct canonical replacement, in-process model downgrade, hosted runner, or live deployment path was added
- Test status: 358 combined catalog/resource/profiler tests and 738 full-suite tests passed locally; 82 full-suite skips remain platform/dependency specific and one third-party Starlette deprecation warning remains
- Advisory decision: continue

## Checkpoint Update - Task 3 Complete

- Current todo: Implement and independently review the adaptive segmentation engine in Task 4
- Active slice: Plan Task 4 only; live Frigate and retired Plex deployments remain untouched
- Completed todos:
- Task 3 exact catalog/identity loading, strict public-versus-canonical startup policy, highest-quality admitted model selection, and status publication
- Two-phase inference admission with generation checks, single-flight model load/release, fail-closed release barriers, fresh reload admission, and terminal load-profile handling
- Pressure callbacks that yield outside the inference permit, typed pathless runtime errors that cannot mark or delete media, and idle resident-model release that rechecks queued/direct work at the safe boundary
- Concurrency fixes for superseding releases, stale cleanup timers, completed same-generation releases, atomic controller publication, release-failure diagnostics, and terminal-profile wakeups
- Task 3 commit `145b83b` contains the runtime integration and its focused regression coverage
- Evidence refs:
- task-3-final-local-verification
- task-3-final-concurrency-review
- task-3-final-failure-attribution-review
- task-3-final-test-gap-review
- Blocked on: none for Task 4; Task 10 still owns Linux/packaged-runtime execution, Task 11 owns clean-process profiler fallback and live-candidate evidence, and Frigate promotion remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Implement `subgen_core/segmentation.py` and focused result-assembly tests without yet wiring it into live transcription

## DriftCheckDraft

- Scope status: Task 3 stayed inside model-runtime integration and the minimum monitor/transcription seams needed to keep runtime failures pathless and non-destructive
- Compatibility status: Explicit model choices remain fixed, public auto remains conservative without trusted evidence, and canonical shared CUDA remains fail-closed
- Retirement status: No alternate model loader, hidden in-process downgrade, hosted runner, or live deployment path was introduced
- Test status: 314 focused tests plus 26 boundary tests passed; the full local suite passed 819 tests with 82 expected skips and one third-party Starlette deprecation warning
- Advisory decision: continue

## Checkpoint Update - Task 4 Complete

- Current todo: Integrate segmented inference and atomic final output in Task 5
- Active slice: Plan Task 5 only; live Frigate and retired Plex deployments remain untouched
- Completed todos:
- Task 4 adaptive half-open chunk planning with five-second context overlap, midpoint ownership, changing per-attempt duration, and same-cursor pressure retry
- Transactional structured-result staging with timestamp offsets, monotonic validation, fresh IDs/back-references, first-owned-language aggregation, and mixed wordful/wordless stable-ts construction
- Backend-neutral handling of faster-whisper `seek`: chunk-local mel-frame indices are deliberately dropped rather than incorrectly offset as seconds
- Reference-safe and generation-safe pressure recovery: inference permits unwind first, payload tracebacks/audio are released, and immutable private release tickets prevent delayed workers from unloading a reloaded model
- Cancellation now wins before policy shrink or recovery waiting; failed chunks never advance the cursor or enter the final result
- Task 4 commit `84e89cf` contains the bounded segmentation engine, focused tests, and the reviewed pressure-handoff correction
- Evidence refs:
- task-4-final-local-verification
- task-4-final-concurrency-review
- task-4-final-failure-attribution-review
- task-4-final-test-gap-review
- Blocked on: none for Task 5; Task 10 still owns installed stable-ts/Linux/package verification, and live Frigate promotion remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Preserve the short whole-file path while wiring long local media to sequential extraction, caller-owned pressure recovery, and one atomic final output

## DriftCheckDraft

- Scope status: Task 4 remains a pure coordinator and structured-result owner; it performs no media probing, file output, webhook, queue, or live deployment work
- Compatibility status: No upload/API path is segmented, no live runtime calls the new engine yet, and stable-ts construction stays behind injected seams pending the installed-package gate
- Retirement status: Premature model/cache release inside `transcribe_with_model` was removed in favor of generation-bound caller recovery; no alternate unload path was introduced
- Test status: 29 segmentation and 71 model-runtime tests passed together; 41 focused boundary tests passed; the full local suite passed 849 tests with 82 expected skips and one third-party Starlette deprecation warning
- Advisory decision: continue

## Checkpoint Update - Task 5 Complete

- Current todo: Add conservative dual-validator media admission in Task 6
- Active slice: Plan Task 6 only; live Frigate and retired Plex deployments remain untouched
- Completed todos:
- Long local media now uses sequential selected-stream FFmpeg windows sized from the runtime's capacity-derived baseline; short and exact-boundary media retains the legacy whole-file path
- Whole-file pressure or recognized allocation failure releases the bound model generation before recovery and falls back to the same adaptive segmented engine from source time zero; explicit segmentation opt-out retries pressure-yielded work as a whole file without invoking a segment extractor
- Inference allocation failures now carry immutable generation tickets, close admission, return their permit before propagation, and cannot let a delayed worker unload a replacement generation
- Uploaded ASR and language-detection buffers remain deliberately unsegmented; pressure tickets are consumed and recovered without entering local media segmentation, while the caller-owned input-buffer limitation is explicit
- Segmented SRT/LRC output renders once into a same-directory private staging file, persists the intended readable mode before inode sync, atomically replaces the destination, and completes webhook/task side effects once; unsupported directory fsync after commit is a bounded warning rather than a false pre-commit failure
- The stable-ts extension-appending behavior is covered explicitly: SRT staging names end in `.tmp.srt`, so no empty published file or leaked `.tmp.srt` sidecar is possible
- Selected audio mapping preserves legacy priority: a valid explicit index, then language match, then the first track; the segmented path never materializes the complete selected track
- Task 5 commits `dc0b375`, `4231a75`, `6ebdccc`, and `3d9ae82` contain the façade, focused integration regressions, publication-failure regression, and final adaptive transcription integration
- Evidence refs:
- task-5-final-local-verification
- task-5-final-adversarial-review
- Blocked on: none for Task 6; Task 10 still owns installed stable-ts/Linux/package verification, and live Frigate promotion remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Introduce bounded FFprobe plus isolated PyAV classification with generation snapshots, conservative aggregation, and exact duration/track handoff before any Whisper load

## DriftCheckDraft

- Scope status: Task 5 stayed inside runtime control propagation, local transcription integration, selected-stream extraction, and final subtitle publication; no marker/deletion or live deployment behavior changed
- Compatibility status: Short local jobs and all uploaded API jobs remain unsegmented, `SEGMENTATION_ENABLED=False` never invokes a chunk extractor, and legacy output naming/webhook/task behavior is preserved
- Retirement status: No alternative model release path, per-chunk subtitle file, hosted runner, or live Frigate/Plex modification was introduced
- Test status: The final adversarial slice passed 133 focused tests; the complete local suite passed 886 tests with 82 expected skips and one third-party Starlette deprecation warning; bounded Ruff, compileall, whitespace, staged-diff, and stable-ts extension regressions passed
- Advisory decision: continue
