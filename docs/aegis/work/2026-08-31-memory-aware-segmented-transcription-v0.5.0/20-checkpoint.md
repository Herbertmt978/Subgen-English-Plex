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
- Defined large-v3-first exact-image profiling, owner-only catalog/identity artifacts, fail-closed shared-CUDA admission, and the then-current 12 GiB profiling-only to 10 GiB production requalification boundary. This boundary was superseded on 2026-09-02: VM 902's 20 GiB guaranteed floor now generates a 17 GiB production cap.
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

## Checkpoint Update - Task 6 Complete

- Current todo: Restrict failure marking, deletion, and legacy repair behavior in Task 7
- Active slice: Plan Task 7 only; live Frigate and retired Plex deployments remain untouched
- Completed todos:
- Media admission now combines bounded FFprobe evidence with a fresh-interpreter PyAV fallback using a conservative 16-case truth table; only dual `invalid_format` evidence classifies a file as invalid media
- FFprobe output, duration, stream metadata, and subprocess lifetime are bounded; the PyAV child decodes at most one frame, returns only normalized JSON, covers all usable audio streams, and cannot keep the parent blocked through an inherited output handle
- Queue admission is generation-bound before, between, and after both validators, and the admitted identity is rechecked through language detection, segmented extraction/inference, and immediately before atomic publication
- Duration and exact audio-track metadata are handed from admission into transcription without a second probe; missing duration fails before any Whisper model load
- Silent media is retained and skipped, indeterminate media is retained with one typed event, and invalid media emits explicit dual-validator evidence for Task 7 without deleting or marking anything in Task 6
- Worker lifecycle events now preserve the admitted source identity, and stale-generation termination is distinct from inference failure so a replacement cannot inherit an earlier file's failure
- Task 6 commit `f558114` contains the conservative validator, generation-bound transcription handoff, typed event evidence, and focused regression coverage
- Evidence refs:
- task-6-final-local-verification
- task-6-final-adversarial-review
- Blocked on: none for Task 7; Task 10 still owns Linux/packaged-runtime execution, and live Frigate promotion remains blocked until Task 11B proves the higher-priority GPU reserve
- Next step: Require durable marker creation plus typed dual-invalid evidence and a still-current source identity before optional deletion; make legacy repair/report paths non-destructive

## DriftCheckDraft

- Scope status: Task 6 stayed inside media classification, queue admission, transcription generation checks, and structured evidence handoff; it did not change monitor deletion or repair behavior
- Compatibility status: Valid media retains legacy language/track selection, silent media is still skipped, and both indeterminate validation and stale replacement are explicitly non-destructive
- Retirement status: Canonical queue admission no longer reprobes through separate `has_audio` and `get_audio_tracks` calls; `has_audio` remains only as a compatibility wrapper, and no hosted runner or live deployment path was introduced
- Test status: 204 focused tests and the complete 961-test local suite passed with 82 expected skips and one third-party Starlette deprecation warning; bounded Ruff, compileall, whitespace, staged-diff, and final independent review passed
- Advisory decision: continue

## Checkpoint Update - Task 7 Complete

- Current todo: Package, document, govern, and version v0.5.0 in Task 8
- Active slice: Plan Task 8 only; live Frigate and retired Plex deployments remain untouched
- Completed todos:
- Only a canonical `media_validation_failed` event with exact dual-validator `invalid_format` evidence, a valid five-field source identity, an unchanged current generation, enabled deletion policy, threshold satisfaction, and a durably re-read processing-error marker can reach monitor unlink
- Generic processing errors, inference/resource/OOM failures, pressure yields, SIGSEGV/restarts, raw log text, indeterminate validation, malformed events, and stale replacements are retained; a replacement generation is processed normally
- Failure markers remain schema v1 and are enabled publicly on the first qualifying failure; marker identity prevents an old failure from suppressing a replacement
- `AUTO_DELETE_INVALID_MEDIA` is the canonical optional switch and defaults off; the deprecated `AUTO_DELETE_FAILED_FILES` alias is narrowed to invalid-media-only deletion with one warning
- Repair is report-only for both requested actions, old delete intents become policy-blocked evidence, malformed/private-state failures preserve original bytes, and repair never removes media or legacy subtitle markers
- Monitor and repair state/log readers are bounded, private, link-safe, and process-locked; recovery requires the exact deployment context, canonical path binding, source identity, typed proof, and durable marker
- Task 7 commit `2e96cb2` contains the deletion restriction, repair retirement, state hardening, and regression coverage
- Evidence refs:
- task-7-final-local-verification
- task-7-final-linux-verification
- task-7-final-security-review
- Blocked on: none for Task 8; exact packaged-runtime, Frigate reserve, publication, and rollout gates remain owned by Tasks 10-13
- Next step: Add v0.5 package parity, public configuration, human-written release notes, migration guidance, and proposed ADR without changing live services

## DriftCheckDraft

- Scope status: Task 7 stayed inside failure marking, monitor deletion admission, legacy repair retirement, and their private persistence/logging boundaries
- Compatibility status: Marker schema v1 and explicit public configuration compatibility are preserved; the legacy delete alias is accepted but deliberately narrowed and warned
- Retirement status: Generic/crash monitor deletion and every repair-side deletion path are retired; old intents remain auditable rather than executable
- Test status: Final Windows/local checks passed 125 focused and 1,023 full-suite tests; the simulator passed all 158 Linux Task 7 tests and 1,101 full-suite tests with one expected skip; both independent security reviews approved the final policy
- Lifecycle status: The simulator was woken by this task, used with a dedicated Ubuntu venv, cleaned, shut down gracefully, and verified unreachable afterward; no GitHub-hosted runner was used
- Advisory decision: continue

## Checkpoint Update - Task 8 Complete

- Current todo: Run complete local verification in Task 9
- Active slice: Plan Task 9 only; no image publication or live deployment
- Completed todos:
- Packaged the owner-operated profiler at `/subgen/profile_model_envelopes.py`, moved the project/image version to v0.5.0, and retained the stable `/status` runtime version `2026.07.1`
- Set the public automatic model, adaptive segmentation, pressure-yield, reserve, first-failure marker, invalid-media-only deletion-off, and report-only repair defaults across all three base profiles
- Added an opt-in long-syntax ModelEnvelope overlay that preserves the mode-0700 parent plus both mode-0600 leaves, refuses to create missing host paths, and composes cleanly with every base while ordinary bases retain genuine missing-evidence fallback
- Published human-written repository release notes that lead with bounded long-file memory, compare v0.4.0/v0.4.1/v0.5.0, separate public and Frigate policy, and distinguish public v0.4.1 rollback from the preserved Frigate v0.3.0 rollback
- Added typed validator evidence to the human-readable failure report without changing deletion authority or state schema
- Kept ADR 0002 Proposed until the complete simulator and Frigate evidence gates pass
- Task 8 commit `70512f3` contains the reviewed packaging, configuration, documentation, release body, report evidence, tests, and proposed ADR
- Evidence refs:
- task-8-final-root-verification
- task-8-final-spec-review
- task-8-final-quality-review
- task-8-release-note-humanity-review
- Blocked on: none for Task 9; exact Linux image, installed stable-ts, constrained inference, shared-GPU reserve, publication, and rollout gates remain pending
- Next step: Run the complete local suite and repository release checks without GitHub-hosted runners

## DriftCheckDraft

- Scope status: Task 8 stayed inside package parity, public/operator configuration, release documentation, report evidence, and proposed governance; no live Frigate/Plex service changed
- Compatibility status: Base profiles remain directly runnable through conservative fallback, exact evidence is an explicit overlay, upload APIs remain unsegmented, explicit models stay fixed, and the stable runtime status version is unchanged
- Retirement status: Public deletion remains off, monitor deletion remains invalid-media-only, repair remains report-only, and no Sonarr/Radarr or Ollama lifecycle integration was introduced
- Test status: Independent specification and quality reviews passed; the final root slice passed 138 focused tests with 17 platform skips, compileall, all three base and all three base-plus-overlay Compose validations, the release/config audit, and whitespace checks
- Release-note status: The reviewed byte-for-byte GitHub body scored 96/100 on the local human-voice audit with no canned AI vocabulary or hedging
- Advisory decision: continue

## Checkpoint Update - Task 9 Complete

- Current todo: Verify the exact committed candidate on the idle simulator in Task 10
- Active slice: Plan Task 10 only; no GitHub-hosted runner, publication, or live Frigate deployment
- Completed todos:
- Ran the complete local Windows suite from a dedicated fixed-`D:` pytest base directory with global plugin autoload disabled and only the repository's required requests-mock plugin enabled
- Passed 1,049 tests with 79 expected platform/dependency skips and one unchanged third-party Starlette deprecation warning
- Passed bytecode compilation for the facade, language/ops/marker/monitor/repair owners, complete `subgen_core`, and owner-operated profiler
- Passed all three base Compose validations and all three corresponding exact-evidence overlay combinations
- Passed the complete `origin/main...HEAD` whitespace check, reviewed the 64-file release diff and 25-commit feature history, and confirmed a clean worktree before recording this checkpoint
- Evidence refs:
- task-9-complete-local-verification
- Blocked on: none for Task 10; the simulator must still prove Linux/POSIX, exact image packaging, installed stable-ts behavior, constrained inference, pressure recovery, and disposable media safety
- Next step: Confirm simulator power/activity state, wake only if idle/offline, transfer an exact checksum-verified committed candidate, and run the Task 10 Linux/package gates

## DriftCheckDraft

- Scope status: Task 9 was read-only verification plus this governance record; no runtime source, package behavior, or live host changed
- Compatibility status: The full suite preserved the approved API/output/marker/model/deletion contracts across the complete branch
- Retirement status: No deprecated destructive path or hosted-runner dependency reappeared
- Test status: 1,049 passed, 79 expected skips, one third-party warning; compileall, six Compose validations, range whitespace, history, and worktree checks passed
- Advisory decision: continue

## Checkpoint Update - Task 10 Complete

- Current todo: Gate the exact candidate on the shared Frigate RTX 3090 after the existing v0.3 queue reaches a proven idle boundary
- Active slice: Plan Task 11B only; no publication, production promotion, or Plex-host recreation
- Completed todos:
- Froze runtime source commit `4418b3c97296a04b311d29d9ce52abefef64e108` and built the exact linux/amd64 candidate with OCI configuration digest `sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae`, platform manifest `sha256:9e557e124ca6994c4aa30af77301a75d31145e53ec17e6b18997969c67308b5b`, OCI index `sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc`, and 19 ordered rootfs diff IDs
- Re-ran the final Windows suite at 1,058 passed and 79 expected skips, and the exact transferred Linux source at 1,136 passed with one expected skip; compileall, bounded Ruff, whitespace, and all six Compose renderings passed
- Passed packaged and source-mounted HTTP smokes with zero restarts, and proved the installed stable-ts callback propagates a pressure-yield exception without consuming a second segment
- Completed a 31-minute synthetic workload under 4 GiB with `small` in four bounded windows, 405 monotonic cues, 2,499,293,184-byte peak, and no swap/OOM/restart
- Completed the same workload under 6 GiB while real same-cgroup pressure caused unload, recovery, and same-cursor shrink from a 605-second to a 305-second working window; it later regrew, published one 405-cue monotonic SRT, and recorded no swap/OOM/restart
- Completed the same workload under 9 GiB with `medium` in two bounded windows, 403 monotonic cues, 3,608,408,064-byte peak, and no swap/OOM/restart
- Passed the disposable packaged safety chain: a real silent container was retained, deterministic junk required dual-invalid FFprobe/PyAV evidence, the marker audit preceded deletion, and a valid replacement at the same path was stale-marker-unblocked and requeued; no real media was touched
- Sealed the exact 4,402,460,160-byte image archive at SHA-256 `73f45dc1721a804d569359f5afd51068be5b2d9c562729d4d4a61fd2f5e8bce9`; the owner identity file is SHA-256 `5d3a7e7d5839a9496ef05cddcd8b10c8a71e04f8676243c5ed90ae1968fff87c`
- Evidence refs:
- task-10-final-platform-verification
- task-10-exact-image-and-package
- task-10-installed-callback-propagation
- task-10-constrained-inference
- task-10-disposable-media-safety
- Blocked on: Frigate v0.3 is still draining its pre-existing scan queue; Task 11B must not interrupt it and must independently prove the conservative shared-GPU reserve, exact ModelEnvelope, real-Linux bind identity, and 15-minute camera/detector/embedding health gate
- Superseded next step (2026-09-02): do not transfer or promote the older sealed candidate from this checkpoint. Freeze and verify the replacement commit after the staged-chunk lifetime correction, then run the 12 GiB profiler followed by an independent 17 GiB automatic Frigate gate derived from VM 902's 20 GiB guaranteed floor.

## DriftCheckDraft

- Scope status: Task 10 used only the task-owned simulator, synthetic media, local Docker artifacts, and read-only Frigate queue observation; no GitHub-hosted runner or real library mutation was used
- Compatibility status: Exact packaged behavior preserved HTTP status, installed callback propagation, adaptive chunk merge, atomic output, schema-v1 replacement semantics, and public deletion-off defaults
- Retirement status: Plex-hosted Subgen remains retired; generic/crash deletion and repair deletion remain unavailable
- Test status: Every local/simulator Task 10 gate passed; the real shared-CUDA reserve and real-Linux host/container identity proof remain correctly deferred to Task 11B
- Lifecycle status: The simulator remains task-owned and awake only because the sealed image must still be transferred, published locally to GHCR after the gate, and pull-smoked; shutdown remains mandatory after final activity and task-marker checks
- Advisory decision: continue

## Checkpoint Update - Task 11B Safe Handover and Rollback Ready

- Current todo: Freeze and independently approve the hardened health sampler, then transfer the exact candidate and begin isolated Frigate profiling
- Active slice: Task 11B rollback and gate preparation only; no candidate has started and no real media has been mutated
- Completed todos:
- Diagnosed the legacy v0.3 restart as a scan-only state: after its one `SIGSEGV` restart it had only the launcher and scanner processes, no worker or FFmpeg child, no post-restart transcription, no OOM, and an unopened HTTP endpoint while it enumerated the library
- Used that no-worker scan boundary instead of waiting for a rebuilt queue that could reach the known crash candidate again; isolated the monitor and repair units first, then stopped the immutable legacy container ID cleanly and proved it remained stopped with restart count one
- Preserved the complete stopped Compose/config/model/state tree as a 4,799,109,120-byte owner-only archive with SHA-256 `28da1de7f02ab7968f904e387e7484295119ed0b05b69a9f2d1bc45c48543408`
- Detected that Docker Desktop/containerd-style `docker image save` emitted only metadata for the lazy v0.3 image and did not accept it as a rollback archive
- Hydrated the exact released linux/amd64 content, exported all 16 referenced blobs through containerd, verified every blob and descriptor, and sealed a 4,403,125,248-byte OCI archive at SHA-256 `c2b5594bb96b66569c34000b27ec0070d66a5d997a680f0a834b4837afab99be`
- Proved the rollback image by importing it into an isolated temporary containerd namespace and matching manifest `sha256:7782ed135eaf9c0ee093a9649777e0bfb15587639b9cb5f7f8b5acacb6987e0e` and config `sha256:de721d392c53fb644e812aee309337f56cc864ae7fbddba48f44a64165438fc1`
- Sealed a create-once owner-only rollback-ready record after rechecking the stopped container and inactive/disabled legacy monitor and repair schedule
- Evidence refs:
- task-11b-safe-handover-and-rollback
- Blocked on: the hardened sampler's final independent review and exact transfer hashes; no v0.5 profiler or runtime candidate may start before that boundary passes
- Next step: finish the fail-closed sampler review, commit it as `SAMPLER_COMMIT`, transfer its exact bytes plus the already sealed candidate, and run the supervised disposable profiler gate

## DriftCheckDraft

- Scope status: The only live change was the authorized migration handover: legacy Subgen stopped at a proven scan-only/no-worker boundary and its monitor/repair units were disabled before stop; Frigate, cameras, Ollama, media, and other containers were untouched
- Compatibility status: The original stopped container ID, full configuration/model/state tree, unit-state evidence, OCI manifest/config identity, and independently import-tested image archive preserve an operational v0.3 rollback
- Retirement status: Legacy destructive monitor/repair execution is inactive during the v0.5 gate; no deletion path was enabled and no media file was changed
- Test status: Both rollback archives passed SHA-256 verification; the OCI archive passed complete blob/descriptor validation and isolated containerd import
- Advisory decision: continue

## Checkpoint Update - Task 11B Sampler Approved

- Current todo: Commit the approved sampler as `SAMPLER_COMMIT`, checksum-transfer the frozen candidate and exact tooling, then execute the isolated Frigate profiler and automatic-runtime gates
- Active slice: Task 11B transfer and real-Linux disposable integration preflight; no v0.5 container has started on Frigate
- Completed todos:
- Hardened the schema-3 full Docker execution boundary, least-privilege runtime policy, immutable-ID cleanup, transient-systemd `ExecStopPost`, bounded profiler receipt/catalog protocol, and stop-before-seal evidence publication
- Corrected the profiler run validator to accept the required 30-run medium profile while continuing to reject values above 30
- Passed 44 Windows tests with seven expected platform skips and 79 subtests, 51 direct WSL/Linux unit tests, Python bytecode compilation, bounded Ruff, and Ruff formatting
- Obtained a fresh independent review with no remaining P0/P1 findings after the exact medium/30-run acceptance correction
- Froze pre-commit file hashes: sampler `0d10f738d42686562f2000d47bd091e45474cbc9b1a8d52930f8a12eda6a8336`; sampler test `875bf2dc032d48be7ee1775844ed40842df403b47cf7863218a9f0ed3b1d9c96`
- Detected that the simulator's `default` and `desktop-linux` contexts alias the same Docker Engine, rejected them as anonymous-pull isolation, installed Docker Engine 29.1.3 in the existing Ubuntu 24.04 WSL distro, and proved its local-unix-socket engine is active, empty, and distinct from Docker Desktop
- Revalidated the exact stopped v0.3 rollback container and changed only its parked restart policy from `always` to `no`, preventing a Docker daemon restart from resurrecting it beside v0.5; the original policy was written owner-only for rollback and sealed at SHA-256 `fbd25cf5e8bb6f2278a526cda5b08bddca24ab1a59b95e7be45ef5ec03e5b5be`
- Re-ran the complete local suite after the workstation restart: 1,102 tests passed with 86 expected skips and 79 subtests; bytecode compilation and all six Compose renderings also passed without GitHub Actions
- Evidence refs:
- task-11b-sampler-final-local-verification
- task-11b-sampler-final-independent-review
- task-12-anonymous-engine-preflight
- task-11b-parked-v03-restart-policy
- task-11b-resume-full-local-verification
- Blocked on: none before the sampler commit and transfer preflight; the candidate still must not start until both complete and the exact Linux boundary/supervisor checks pass. The mutable-`latest` recovery block remains a separate Task 12 publication blocker and is not used by Task 11B.
- Next step: validate and create the distinct `SAMPLER_COMMIT`, transfer and revalidate those exact committed bytes on Frigate, and run the supervised profiler/automatic-runtime gate while the independent Task 12 recovery refinement continues before publication

## DriftCheckDraft

- Scope status: Sampler changes are owner-operated gate tooling only and are excluded from the frozen runtime image; no product/runtime source or live service changed
- Compatibility status: Runtime commit `4418b3c97296a04b311d29d9ce52abefef64e108`, candidate OCI identity, public defaults, and deletion policy remain unchanged
- Retirement status: Legacy Frigate v0.3 remains stopped with its import-tested rollback intact and a parked `restart=no` policy; its recorded original `always` policy may be restored only during verified deletion-off rollback. Plex-hosted Subgen remains retired
- Test status: Windows and WSL sampler suites plus independent P0/P1 review passed after the 30-run correction; the separate anonymous engine passed active/empty/distinct-ID preflight but has not yet pulled the release candidate
- Advisory decision: continue

## Checkpoint Update - Task 11B Control Failure and Task 11A Rewind

- Current todo: implement the host-owned priority-pressure amendment, rebuild
  the exact candidate and ModelEnvelope evidence, then restart Task 11B from
  `large-v3`
- Active slice: Task 11A design/plan and source work; no Subgen candidate may
  run on Frigate under the superseded policy
- Completed todos:
- Passed the reviewed moved-bind `SIGKILL` lifecycle proof for the five-minute
  sampler and preserved Gate1/Gate2 exact cleanup evidence
- Preserved Gate2's expected `large-v3` profiler return-code-3 receipt as
  diagnostic capacity evidence only; the health gate aborted at about 75
  seconds and therefore produced no final observation/pass seal
- Completed a create-once candidate-absent 900-second Frigate control with 181
  samples: `clean_for_gate=false`, 51 skipped-FPS breach samples, 45 low-ratio
  samples, 75-second longest streaks, maximum 6.3 skipped FPS, minimum 0.2
  process ratio, and zero invalid environment samples
- Proved that the control breach occurred while Frigate remained healthy with
  restart count zero, Ollama remained unloaded, and about 17.5 GiB of VRAM was
  still free; free VRAM is therefore not shared-GPU compute authority
- Finalized and independently reverified the isolated runtime observer at
  commit `7254df3`: Windows 86 passed/7 skipped/106 subtests, Linux 93 passed/
  106 subtests, bounded Ruff/format/compileall passed, and no P0/P1 remained
- Amended the design, ADR proposal, and implementation plan so the
  higher-priority host publishes a coarse owner-only stale-fail-closed signal
  while the existing pressure controller remains the sole yield owner
- Evidence refs:
- task-11b-gate2-diagnostic-capacity
- task-11b-frigate-only-control-failed
- task-11b-runtime-observer-final
- Blocked on: runtime/image/envelope/sampler/gate evidence before the priority
  amendment is superseded and cannot authorize publication or deployment
- Next step: implement and locally verify the generic signal reader, Frigate
  producer, controller/runtime wiring, package surfaces, and revised gate;
  rebuild/refreeze only after those cohesive slices pass review

## DriftCheckDraft

- Scope status: no Frigate, camera, Ollama, media, or production Subgen
  configuration changed; the only live activity was bounded read-only aggregate
  telemetry under owner-only Task 11B evidence paths
- Compatibility status: the public signal path remains unset; routes, queue,
  output, fixed-model, marker, and deletion boundaries remain unchanged
- Retirement status: the stopped v0.3 rollback remains intact with
  `restart=no`; Plex-hosted Subgen remains retired; all pre-amendment candidate
  authority is explicitly retired to diagnostic history
- Test status: observer regression/static checks pass; amended product code has
  not started and receives fresh focused/full/simulator evidence before any new
  image or candidate
- Advisory decision: rewind to Task 11A and continue

## TaskStartSnapshot - Task 11A priority-pressure implementation

- Captured: `2026-09-01T13:10:04.5166080+01:00`
- Branch/HEAD: `Herb/memory-aware-segmentation` at
  `7254df3bb4be911fa17e812f8bfe1826cc99cc02`
- Pre-existing task delta: only the five Task 11A governance files (ADR, design,
  plan, checkpoint, and evidence) were modified; no product source was dirty
- Live boundary: no v0.5 candidate is running; Frigate remains the protected
  higher-priority workload; Ollama is unloaded; the parked v0.3 rollback remains
  stopped with `restart=no`; Plex-hosted Subgen remains absent
- TDD route: off/skipped under the recorded plan; every slice still receives
  focused regression tests, Linux parity, two-stage review, and coordinator
  verification before its scoped commit
- Test placement: Windows/WSL and the approved simulator only; GitHub-hosted
  runners remain prohibited

## Checkpoint Update - Task 11A source-generation and gate correction

- Current todo: implement the strict generic signal reader/controller/status
  slice, then the distinct-generation Frigate producer, packaging, and revised
  observer/sampler
- Completed todos:
- Sealed the read-only candidate-absent contention diagnostic at 181 samples/
  900.023 seconds with evidence SHA-256
  `29ee33ec53116e7abc4aaafe82e55749ebcd4827c86dfef0bfb1bad08ec85988`
  and seal SHA-256
  `d26e235da5f75c895d47b9ca678f671ad6c351876cd528b8b7c0b9e6f77d03eb`
- Verified the private two-distinct-generation 80-FPS predictor catches 41/42
  breach samples, anticipates three of four onsets, and deliberately yields on
  22/139 otherwise-clean samples
- Corrected the design after independent review: duplicate five-second polls do
  not count as fresh Frigate decisions; one causal assertion/unload/reload proof
  is separate from and followed by an uninterrupted 900-second clear pass
- Evidence refs:
- task-11a-frigate-contention-predictor
- Blocked on: no source blocker; live candidate and publication remain blocked
  until the amended exact image, producer, observer/sampler, and all regenerated
  evidence pass
- Next step: execute the two non-overlapping implementation slices through
  spec-compliance and code-quality review, then integrate and refreeze

## DriftCheckDraft

- Scope status: only owner-only aggregate telemetry and governance changed; no
  live service, camera, Ollama, media, container, or production configuration
  was mutated
- Compatibility status: public priority integration stays unset; the 80-FPS
  policy is private/evidence-bound and cannot become a public universal default
- Retirement status: all pre-amendment runtime/image/envelope/gate authority
  remains diagnostic history; legacy and Plex retirement boundaries are intact
- Test status: no Task 11A product source exists yet; source work begins from the
  recorded clean product baseline and receives fresh local/Linux verification
- Advisory decision: continue

## Checkpoint Update - Post-restart contract closure

- Captured: `2026-09-01`
- Branch/HEAD: `Herb/memory-aware-segmentation` at
  `7254df3bb4be911fa17e812f8bfe1826cc99cc02`
- Active slice: finish the amended Task 11/12 contract before creating product
  source so implementation and release verification share one exact boundary
- Completed todos:
- Replaced the lossy single receipt with an append-only, fsync-before-exposure
  runtime journal carrying exact priority, model, workload, cursor, completion,
  CUDA-OOM, and media-failure generations
- Bound separate immutable Phase A and Phase B workload identities and made all
  four gate-only environment variables all-empty/all-valid
- Expanded both gate phases to prove zero Docker OOM state, cgroup OOM/kill
  deltas, runtime failure-counter deltas, bounded candidate-log CUDA matches,
  and kernel-journal NVIDIA Xid matches
- Added the missing focused coverage for priority parsing/cadence, receipt
  durability and continuity, process-lifetime failure counters, and ordered
  two-workload gate isolation
- Ran the fresh local baseline without GitHub-hosted runners: 1,142 passed,
  86 skipped, 106 subtests, and two expected release-note contract failures
  caused only by the still-draft v0.5 prose (`5 to 30 minutes` wording and the
  required two-sentence human introduction)
- Blocked on: Task 12 must finish eliminating working-tree verifier trust,
  ambiguous release absence, normalized body comparison, incomplete hosted-run
  snapshots, and the race-prone ordinary `v0.5.0` registry push
- Next step: obtain an independent governance PASS, commit the governance
  boundary, then implement the priority/runtime amendment in cohesive locally
  tested slices

## DriftCheckDraft

- Scope status: the five governance files remain the only task delta; no source,
  live service, camera, Ollama, media, container, GitHub ref, release, or package
  was changed
- Test status: product baseline is healthy; the two release-note failures are
  explicit future-slice assertions rather than runtime regressions
- Publication status: blocked until the corrected exact-byte, full-run-baseline,
  and registry create-only contracts independently pass review
- Advisory decision: continue

## Checkpoint Update - Task 11A generic priority consumer complete

- Captured: `2026-09-01`
- Current todo: commit the verified generic priority reader, controller,
  model-runtime, startup, status, tests, and aligned contract prose as one
  cohesive source slice; then implement the distinct Frigate host producer,
  packaging, and revised gate tooling
- Active slice: final commit-scope audit for the product-side Task 11A priority
  consumer; no candidate or legacy Subgen container is running on Frigate
- Completed todos:
- Added the strict maximum-4-KiB owner/mode/regular/no-follow canonical signal
  reader with host-boot, source-generation, heartbeat, policy, and privacy-safe
  observation validation
- Kept `PressureController` as the sole admission/yield/recovery owner, with
  priority-first one-second polling, immediate resident-model yield, closed
  admission while unavailable, and exactly three distinct post-epoch clear
  generations before recovery
- Wired one shared reader through startup, bootstrap replay, active inference,
  idle observation, model load admission, unload/reload generations, and one-
  lock/one-clock public status without changing the unset public default
- Closed two final independent-review findings with intrinsic reader
  serialization and exact no-eviction replay history; a 4,097th distinct epoch
  now latches unavailable until process restart instead of evicting history
- Passed 28 reader tests, 474 integrated priority/controller/runtime tests, and
  the complete local suite with 1,209 passed, 86 expected skips, and 106
  subtests; bounded Ruff, compileall, and whitespace checks also passed
- Received a fresh independent re-review with no remaining P0/P1 findings
- Assembled and indexed the advisory proof bundle and gate-input pack. The
  workspace structure check remains nonzero only on pre-existing governance,
  legacy-ADR-shape, and old-index drift outside this source slice; no workspace-
  clean claim is made and that migration is not folded into this commit
- Evidence refs:
- task-11a-priority-consumer-final-local-verification
- task-11a-priority-consumer-final-independent-review
- task-11a-priority-consumer-workspace-structure
- Blocked on: no source blocker for this commit; any new image, Frigate
  candidate, packaging, GitHub publication, or deployment remains blocked until
  the producer/package/gate slices and fresh post-amendment evidence pass
- Next step: create the cohesive source commit, then implement and locally test
  the distinct-generation Frigate producer and its package/configuration surface

## DriftCheckDraft

- Scope status: only Task 11A product source, focused tests, release/contract
  prose, and Aegis state changed; no Frigate, camera, Ollama, media, container,
  VM, GitHub ref, registry, release, or production configuration was mutated
- Compatibility status: `PRIORITY_PRESSURE_FILE` remains empty by public
  default; existing non-shared deployments keep their prior pressure behavior,
  and canonical shared CUDA intentionally fails startup closed until the host
  signal is configured
- Retirement status: the Frigate v0.3 rollback remains stopped with
  `restart=no`; Plex-hosted Subgen remains retired; all pre-amendment candidate
  authority remains diagnostic history only
- Test status: the exact working-tree consumer slice passed focused, integrated,
  full-suite, static, and independent P0/P1 review locally without GitHub-hosted
  runners
- Workspace status: the current proof bundle is indexed; the helper check still
  reports documented pre-existing structural drift and therefore does not
  provide a clean-workspace result
- Advisory decision: continue

## Checkpoint Update - Task 11A Frigate producer and package surface complete

- Captured: `2026-09-01`
- Current todo: commit the verified host producer, package/configuration
  surface, release prose, consumer checkpoint/high-water correction, and
  focused regressions as one cohesive slice
- Active slice: final commit-scope audit; no v0.5 candidate, live Frigate
  service, camera, Ollama model, media file, VM, registry, GitHub ref, or release
  was changed
- Completed todos:
- Added the standalone low-priority Frigate/Ollama/NVIDIA producer with strict
  private policy/config identity, bounded concurrent probes, absolute shared
  HTTP deadline, exact source-generation evaluator, canonical coarse
  publication, and owner-only atomic file boundary
- Added the host-writer environment, hardened systemd unit, public blank
  consumer default, zero-setup base profiles, explicit parent-only read-only
  overlay, installation/configuration/migration/security guidance, and
  human-written release comparison
- Corrected recovery so epoch, pressure, neutral, and validated sequence-gap
  generations remain a monotonic high-water floor; their duplicate heartbeat
  can never substitute for one of three new clear generations
- Closed independent findings for numeric overflow, complete reason unions,
  FIFO blocking, leaf swaps, trickled HTTP headers/bodies, official Frigate
  plain-text version handling, and fresh-consumer sequence-N checkpointing
- Closed the final packaging boundaries: standard Docker boot ordering,
  outside-checkout private policy/draft storage with exact ignore defenses,
  fixed producer constants, and upgrade preservation of the selected base plus
  active overlays
- Passed the complete Windows suite with 1,267 tests, 91 expected skips, and 106
  subtests; passed the final focused Linux suite with 368 tests and no skips;
  passed package/module tests, all ten Compose renderings, compilation, bounded
  lint, new-file formatting, whitespace checks, and fresh independent contract,
  security, and release-surface review
- Evidence refs:
- task-11a-priority-producer-final-local-verification
- task-11a-priority-producer-final-independent-review
- Blocked on: no source/package blocker for this commit; candidate rebuild,
  ModelEnvelope refreeze, revised observer/sampler, Frigate isolated gates,
  publication, and deployment remain blocked until their later plan steps pass
- Complexity closure: the roughly 1,300-line producer is a strong pressure
  signal but remains a single standalone canonical host-policy owner rather
  than adding responsibility to the already over-budget resource controller or
  destructive failure monitor. Its policy, transport, evaluator, and file
  seams have focused tests and independent review; no duplicate owner or
  compatibility fallback was introduced. Status: exceeded-and-governed for
  this slice, with extraction only if future behavior adds another reason to
  change.
- Next step: create the cohesive local commit, read it back, then implement and
  verify the revised runtime observer/sampler and post-amendment candidate gate

## DriftCheckDraft

- Scope status: aligned with the Execution Readiness View's intent, scope,
  owner, compatibility, and no-public-mutation locks; the producer remains
  optional publicly and mandatory only for the reviewed shared-CUDA target
- Compatibility status: routes, queue, subtitle output, model choice, marker
  schema, and deletion boundaries are unchanged; the public priority path is
  blank and all three base profiles have no signal bind
- Retirement status: pre-amendment candidate/gate authority remains diagnostic
  only; Frigate v0.3 rollback remains stopped and preserved; Plex-hosted Subgen
  remains retired; no old destructive path was restored
- Test status: direct Windows/Linux, package, Compose, static, and independent
  review evidence passed without GitHub-hosted runners
- Workspace status: the existing Aegis structural baseline/legacy-index drift
  remains documented and outside this code slice; no whole-workspace-clean
  claim is made
- Advisory decision: continue

## Checkpoint Update - Task 11A runtime receipt integration

- Captured: `2026-09-01`
- Current todo: finish the amended sampler/observer live-host interfaces, then
  verify the complete repository and freeze the post-amendment runtime and gate
  commits
- Active slice: gate-only application receipt lifecycle plus the owner-operated
  unloaded-GPU envelope and two-phase gate tooling; live Frigate, the parked
  rollback container, Plex retirement, GitHub refs, registries, and releases
  remain untouched
- Completed todos:
- Added the normally-disabled append-only owner-only runtime receipt journal,
  exact Phase-A/Phase-B workload binding, process identity, model identity and
  load/unload generations, chunk unwind/commit cursors, terminal completion,
  CUDA-OOM, and media-failure generations
- Integrated atomic final publication for both segmented and explicit opt-out
  local-file paths; Task 11B now requires successful parent-directory fsync
  before completion while ordinary/network-filesystem installs retain the
  prior warning-only compatibility behavior
- Closed the independent review's three application blockers: Task 11B
  controller mutations are serialized under model-condition then controller
  lock until the matching receipt returns; directory-sync failure aborts gate
  completion; and marker-worthy `worker_error` increments the media-failure
  generation while stale-media and model-runtime control errors do not
- Passed 458 focused Windows receipt/controller/transcription/worker tests,
  then the complete Windows application suite with 1,245 passed and 88
  expected skips
- Passed the complete disposable Ubuntu/POSIX application suite with 1,332
  passed, one expected platform skip, and the known third-party Starlette
  deprecation warning; the temporary venv was removed in the same command
- Passed bounded Ruff `E9,F63,F7,F82`, Python compilation, and whitespace
  checks for the application-side delta
- Evidence refs:
- task-11a-runtime-receipt-integration-local-verification
- Blocked on:
- Fresh independent re-review of the three corrected application findings
- Completion of the sampler/observer host interfaces for uninterrupted Docker
  logs, continuous kernel-journal cursors, exact cgroup/PID GPU attribution,
  and two fully bound fixture records
- Next step: integrate and cross-test those sampler/observer interfaces on
  Windows and Linux, then run full repository/package/Compose verification

## DriftCheckDraft

- Scope status: the receipt and gate changes remain owner-operated Task 11A
  evidence surfaces; public defaults leave all four receipt settings empty and
  therefore incur no private journal or host-policy dependency
- Compatibility status: existing routes, queue identity, subtitle naming,
  output contents, markers, and deletion defaults remain unchanged; only the
  internal render target is temporary before atomic rename
- Retirement status: the pre-amendment gate remains non-authoritative, the
  Plex instance remains retired, and the stopped Frigate v0.3 rollback remains
  preserved rather than restarted or modified
- Test status: application behavior is green on Windows and true POSIX/Linux;
  whole-repository gate-tool evidence is still pending
- Advisory decision: continue

## Checkpoint Update - Task 12 publisher hardening complete locally

- Captured: `2026-09-02`
- Current todo: freeze the cohesive runtime, gate, and publication commits,
  then rebuild and exercise their exact candidate on the approved simulator
- Active slice: local source, release-tool, documentation, and failure-
  injection verification only; GitHub refs/releases, GHCR release tags, the
  Frigate host, the stopped v0.3 rollback, Plex retirement, and media remain
  unchanged
- Completed todos:
- Bound retained GHCR create/CAS probe winners to their exact strong ETags and
  made recovery reject any replacement winner, even when its bytes match
- Made the owner-only v3 publication journal start from one exact pre-mutation
  checkpoint, enforce monotonic safety state, bind its terminal sequence and
  SHA-256 through a canonical head, and publish transaction/receipt/head-next
  records through durable staged no-replace operations on Windows and POSIX
- Added fail-closed torn-write recovery: only an exact strict-prefix file may
  be repaired from a valid durable transaction and prior head; foreign bytes,
  aliases, collisions, non-prefix data, and a torn published transaction block
- Removed unsafe mutable-`latest` cancellation. Once the single write is armed
  or ambiguous, the publisher never retries it and never unlocks while the
  authoritative digest remains prior; only the exact expected digest permits
  completion and exact lock removal
- Made lock-create and lock-remove pending receipts hard-crash resumable only
  from their exact expected remote state, without a duplicate create or delete
- Bound anonymous Docker smoke verification to one long-lived
  `docker system dial-stdio` process and Engine API stream, with repeated same-
  session daemon identity, strict bounded HTTP framing, fragmented-read,
  trailing/prequeued-byte, process-control, stream-close, and guaranteed
  private-config cleanup tests
- Preserved exact committed Git-blob execution under isolated Python, reject
  replacement refs/shallow/grafts, keep credential material out of the journal
  and push arguments until needed, and reject ambiguous duplicate response
  headers
- Corrected the human release notes to describe process-wide fixed model
  selection, three-healthy-chunk regrowth, optional-monitor marker persistence,
  and the no-GitHub-runner verification route
- Passed the final Task 12 suite with 173 passed and two expected skips; the
  combined gate/publication matrix with 364 passed, 22 expected skips, and all
  127 subtests; and the complete Windows repository suite with 1,622 passed,
  110 expected skips, and all 127 subtests
- Passed the complete disposable Ubuntu/POSIX suite with 1,724 passed, eight
  expected skips, and all 129 subtests. Bounded Ruff, targeted Ruff format,
  compileall, and `git diff --check` also passed; Ubuntu and Docker Desktop were
  returned to their initial stopped state
- Obtained a fresh independent adversarial review with no remaining P0, P1, or
  P2 finding after 173 tests, two expected skips, and additional low-level
  crash/framing probes
- Evidence refs:
- task-12-publisher-final-local-verification
- task-12-publisher-final-independent-review
- Blocked on: cohesive commits and the fresh immutable simulator candidate,
  ModelEnvelope profiles, exact Task 11B Frigate gate, publication capability
  probes, and controlled deployment; this local result does not authorize any
  remote or live mutation
- Next step: inspect and commit only the intended logical slices, then check
  simulator ownership/idle state and run the local image/cgroup/profile gates

## DriftCheckDraft

- Scope status: all work in this checkpoint is local and release-scoped; no
  GitHub-hosted runner, registry release tag, public ref/release, live service,
  or media mutation occurred
- Compatibility status: public v0.5 behavior remains process-fixed highest-
  safe model selection, adaptive five-to-thirty-minute chunks with recovery,
  optional first-failure marker persistence, and opt-in dual-invalid deletion
- Retirement status: Plex Subgen remains retired and the Frigate v0.3 rollback
  remains stopped, preserved, and unmodified during this slice
- Trust-boundary status: owner/admin compromise, GHCR conditional/strong-ETag
  semantics, and the root-controlled Docker daemon remain explicit external
  assumptions; ambiguity inside those boundaries fails closed and retains the
  publication lock
- Test status: final Windows and true POSIX/Linux complete suites, publication
  failure injection, static checks, and independent adversarial review pass
- Advisory decision: continue to immutable local/simulator candidate creation;
  publication and live deployment remain blocked

## Checkpoint Update

- Current todo: Run a new, never-reused Attempt 12 exact 6 GiB same-cgroup pressure gate with the frozen v0.5.0 candidate and reviewed v7 helper; if it passes, finish and validate the full 31-minute transcription.
- Active slice: Only the approved simulator local gate is active. GitHub refs, tags, releases, runners, GHCR release tags, the live Frigate v0.3 Subgen deployment, the stopped Plex deployment, and real media deletion remain untouched.
- Completed todos:
- Froze runtime commit 3bef1fe and candidate tag subgen-english-plex:v0.5.0-candidate-3bef1fe at image sha256:4296fc6af2b406264b06eb8f4a9f032a26147de4f6ae3638806f552001c6f6a7.
- Passed the complete Linux application suite with 1,754 passed, 9 skipped, and 129 subtests, plus exact 4 GiB CPU transcription with 405 monotonic cues, 2,422,546,432-byte peak memory, no swap, OOM, or restart.
- Sealed pressure Attempts 9, 10, and 11 as inconclusive without reusing a candidate; Attempt 11 remained safe but its post-calibration median was 527,945,728 bytes, below the 612-628 MiB band, and the exact container was removed after evidence capture.
- Committed local gate-chain hardening as fe08588 and profiler source-proof binding as eb73b25; the latter passed 176 tests with 6 expected skips, static checks, and independent review.
- Reviewed and froze pressure helper v7 at SHA-256 8aaa6e5802c37015ad762bee369df5068134bcf06e78a02d605a9fb2c52582b5; six focused tests, compilation, Ruff, formatting, and final independent review pass.
- Evidence refs:
- task-10-post-amendment-runtime-and-image-freeze
- task-10-cpu4-constrained-inference
- task-10-cpu6-pressure-attempts-9-through-11
- task-12-profiler-source-proof-chain
- task-10-pressure-helper-v7-local-verification
- Blocked on: Attempt 12 exact 6 GiB pressure/full-transcription pass, then the exact 9 GiB medium-model gate, synthetic deletion/marker safety suite, Task 11B Frigate GPU coexistence gate, and local publication/deployment verification.
- Next step: Reconstruct Attempt 10 container inputs from sealed evidence, create a fresh Attempt 12 directory/container, prove the v7 helper hash and pure plan inside the frozen image, then issue exactly one transcription request and one helper launch.

## DriftCheckDraft

- Scope status: The active scope remains the frozen v0.5.0 candidate and approved simulator-only verification. No GitHub runner, public ref/release, registry release tag, live Frigate service, Plex service, or real media changed.
- Compatibility status: Public behavior remains highest-quality safe model selection when unset, adaptive 5-30 minute segmentation with bounded shrink/retry/regrowth, first-failure fingerprint markers by default, optional deletion only after both FFprobe and PyAV conclusively reject media, and dynamic yielding without changing the selected model.
- Retirement status: Plex Subgen remains retired. The live Frigate v0.3 deployment and rollback remain running/preserved as previously recorded and were not modified by this resumed local gate slice.
- New risk signals:
- Attempts 9-11 show calibration control, not cgroup safety, is the remaining 6 GiB gate risk. Attempt 12 must fail closed if its one re-settle or immediate hold-boundary sample is outside 612-628 MiB; no pressure may be added back after reclaim.
- Advisory decision: continue

## Checkpoint Update - adaptive-capacity pre-freeze verification

- Captured: `2026-09-02`
- Current todo: complete the remaining pre-freeze review, then create and test a
  fresh immutable candidate from the verified adaptive-capacity source
- Active slice: local Windows and task-owned simulator verification only;
  GitHub runners, public refs/releases, registry release tags, live Frigate,
  the retired Plex deployment, and real media remain untouched
- Completed todos:
- Passed the complete Windows suite with 1,566 tests, 100 expected skips, and
  one known third-party warning in 53.23 seconds
- Passed Python compileall and focused Ruff checks for the adaptive-capacity
  and disk-backed segmented-result slice
- Passed the complete Linux Python 3.10 suite with 1,661 tests and one expected
  skip in 94.13 seconds; the exact disposable container reported no OOM
- Treated the first stable-ts comparator/normalization discrepancy as a
  pre-freeze blocker, corrected it, and passed the distinct exact
  stable-ts 2.19.1 Linux gate with 52 tests in 7.54 seconds
- Ran the real 12 GiB capacity configurator and generated a 10,240 MiB hard,
  no-extra-swap Subgen limit while retaining a 2,048 MiB host reserve
- Rendered all ten required Compose base/overlay combinations against the
  generated literal capacity file
- Proved the live disposable cgroup had Docker Memory and MemorySwap both set
  to 10,737,418,240 bytes, `memory.swap.max=0`, and `oom_score_adj=1000`
- Proved a missing generated capacity file fails closed during Compose
  rendering rather than silently starting without the limits
- Evidence refs:
- task-10-adaptive-capacity-pre-freeze-verification
- Blocked on: the fresh immutable candidate and its constrained inference,
  pressure, publication, and Task 11B shared-Frigate gates; these pre-freeze
  results do not authorize image publication or a live deployment
- Next step: finish the exact source/diff review, freeze a new immutable
  candidate only from that reviewed state, and rerun the remaining simulator
  gates before any Frigate candidate is considered

## DriftCheckDraft

- Scope status: this checkpoint records only local and disposable simulator
  evidence; no GitHub-hosted runner, public/registry mutation, live service,
  or media mutation occurred
- Compatibility status: the generated limit is derived from the guaranteed
  hardware capacity and preserves a host reserve; all ten documented Compose
  combinations retain the same no-extra-swap and last-priority controls
- Retirement status: the earlier `3bef1fe` image remains historical evidence
  for its earlier source only; the current adaptive-capacity source has no
  immutable candidate or deployment authority. Plex remains retired and the
  live Frigate deployment was not changed
- Test status: fresh Windows, Linux Python 3.10, exact stable-ts 2.19.1,
  capacity configurator, Compose rendering, and live disposable cgroup checks
  pass at the pre-freeze boundary
- Advisory decision: continue local verification; publication and live
  deployment remain blocked

## User Requirement - 72-hour pre-publication Frigate soak

- Captured: `2026-09-02`
- Current todo: finish the human-readable progress/RAM-control slice and full
  local verification, then freeze a fresh immutable candidate
- Active slice: local source, documentation, and Windows tests only; GitHub,
  GHCR, live Frigate, retired Plex, and real media remain unchanged
- Release order: local/simulator verification -> isolated Frigate acceptance ->
  continuous 72-hour private candidate soak -> publication of the exact soaked
  identity -> same-identity final operator-policy confirmation
- The 15-minute isolated acceptance seal is an entry gate, not publication
  authority. The soak must separately bind image/runtime/policy/config identity,
  uninterrupted start/end time, transcription and join outcomes, marker
  handling, Subgen/Frigate health, restart/OOM/CUDA/Xid deltas, and rollback
  readiness
- Any source, image, runtime-policy, monitored-configuration, evidence-tool, or
  observer change, failed transcription/join, unexpected restart, OOM/Xid, or
  Frigate health breach invalidates the window. Correct the cause and restart
  all 72 hours
- Focused evidence: 256 tests passed with 21 expected skips across the new
  human-progress, segmented-transcription, media-validation, monitor, and
  packaging surfaces; targeted formatting and fatal Ruff checks passed
- Blocked on: complete local suite, fresh simulator/image gates, isolated
  Frigate acceptance, and the new 72-hour soak
- Next step: finish independent review and full local checks without any remote
  mutation

## DriftCheckDraft

- Scope status: the human logs and soak prerequisite directly serve the
  approved adaptive-memory release and add no new runtime owner or deletion path
- Compatibility status: structured worker events and monitor matching remain
  canonical; human log text is bounded and cannot inject monitor sentinels
- Retirement status: Plex remains retired; the current Frigate v0.3.0 rollback
  remains preserved and untouched in this slice
- Test status: focused source, failure-monitor, documentation-package, format,
  and fatal-static checks pass; full-suite and candidate evidence remain pending
- Advisory decision: continue local verification; publication remains blocked

## Checkpoint Update - Optional MQTT/Home Assistant inventory

- Captured: `2026-09-02`
- Current todo: finish the final inventory race/readback review and complete
  exact candidate validation on the simulator and disposable HAOS-DEV before
  any production Home Assistant or Frigate change
- Active slice: optional MQTT inventory source, package, documentation, and
  local verification only; the feature remains publicly disabled by default
- Completed todos:
- Implemented a full supported-media count and inspection pass across every
  configured library before decode, with watcher-first cutoff reconciliation,
  scan-generation isolation, and a bounded incomplete-scan escape
- Implemented retained QoS-1 Home Assistant discovery/state/availability for
  aggregate **Subgen Items Left** and **Subgen Scan %**, including immediate
  important updates, reconnect discovery, and a fixed 60-second refresh
- Kept retained payloads aggregate-only: no paths, filenames, titles, subtitle
  text, or path hashes; default library identifiers are generic and optional
  operator labels are explicitly documented as retained/public-to-the-broker
- Passed 151 focused MQTT and packaging tests
- Passed the complete 1,696-test local suite with 100 expected skips and one
  known Starlette deprecation warning
- Passed 46 observer tests with two Windows-only POSIX skips
- Passed Python compilation, bounded Ruff, and whitespace/diff checks for the
  current local slice
- Passed an independent hash-bound source gate covering watcher arrivals,
  moves, deletions, cutoff races, failed scans, cross-library transfers,
  rollback, and deduplication with no deterministic P0-P2 finding
- Evidence refs:
- task-8a-mqtt-ha-inventory-local-verification
- Blocked on: complete exact-candidate simulator and HAOS-DEV evidence,
  production Frigate/Home Assistant acceptance, and a
  fresh uninterrupted 72-hour private soak. No current evidence authorizes a
  GitHub ref/release, GHCR release tag, or production deployment
- Next step: freeze, transfer, and validate the exact source on the approved
  simulator, then prove discovery and state behavior in disposable HAOS-DEV

## DriftCheckDraft

- Scope status: this checkpoint records only the bounded optional MQTT/HA
  source slice and local command evidence; no live broker, production Home
  Assistant, Frigate service, GitHub/GHCR surface, or media was changed
- Compatibility status: the feature is publicly off by default; when enabled,
  it adds a pre-decode inventory barrier and diagnostic side channel without
  changing transcription ownership, queue concurrency, subtitle output, marker
  policy, or invalid-media-only deletion
- Retirement status: Plex remains retired. The preserved Frigate rollback and
  current live service were not modified or validated by this local slice
- Privacy status: retained state is aggregate-only and uses generic library
  identifiers unless an operator explicitly supplies retained display labels;
  credentials and media identity are outside the payload contract
- Test status: 151 focused MQTT/package tests, the complete 1,696-test suite
  with 100 expected skips and one known Starlette warning, 46 observer tests
  with two Windows-only POSIX skips, compilation, bounded Ruff, whitespace/diff
  checks, and the independent hash-bound source gate pass locally; exact
  simulator, HAOS-DEV, production, and 72-hour-soak evidence remain absent
- Advisory decision: continue local verification; production and publication
  remain blocked

## DriftCheckDraft

- Scope status: The exact candidate source is frozen locally; no GitHub, GHCR, production Home Assistant, Frigate, Plex, protected simulator container, or real media mutation occurred.
- Compatibility status: Adaptive segmentation, memory yielding, first-failure markers, invalid-media-only optional deletion, and optional aggregate MQTT inventory remain unchanged.
- Retirement status: Plex Subgen remains retired and Frigate v0.3 remains stopped/preserved as rollback; no deployment was changed.
- New risk signals:
- The simulator is currently unreachable and failed both configured and LAN-local Wake-on-LAN; exact build and Linux/package verification cannot proceed until it is reachable.
- Advisory decision: needs-verification

## Checkpoint Update

- Current todo: Resume the exact 590ccb3 simulator candidate build when the approved simulator becomes reachable.
- Active slice: Infrastructure wait only; the exact source commit and publication lock are preserved.
- Completed todos:
- Corrected the two final MQTT documentation findings and froze exact local commit 590ccb3c81c2a6d5b503a1a1b9fd556744af985c.
- Restored HAOS-DEV to pre-subgen-mqtt-20260902 and verified VM 103 stopped after the simulator gate became unavailable.
- Evidence refs:
- task-8a-mqtt-candidate-freeze-and-simulator-wake
- Blocked on: The approved simulator is unreachable after all established wake routes; exact image, Linux/package, HAOS-DEV, Frigate acceptance, and 72-hour soak gates remain unrun.
- Next step: The active hourly heartbeat will quietly retry the approved simulator wake path and resume the immutable build as soon as the host is reachable.

## Checkpoint Update - Rejected 590ccb3 priority producer

- Captured: `2026-09-04`
- Current todo: freeze and rebuild a replacement candidate containing the
  bounded HTTP response-lifetime correction, then repeat the exact simulator
  and Frigate producer gates before any CUDA transcription or soak begins
- Active slice: local source and regression verification only; the rejected
  candidate is stopped and no Subgen workload is running on Frigate
- Completed todos:
- Reproduced the live Frigate producer failure with a real loopback HTTP server
  returning `Connection: close`: Python's `HTTPConnection` cleared its public
  socket after header parsing and the client dereferenced `None` while reading
  the response body
- Rejected source commit `590ccb3c81c2a6d5b503a1a1b9fd556744af985c`
  before the isolated acceptance gate; it is not eligible for the 72-hour soak
- Corrected the canonical `BoundedHttpClient` owner by retaining the connected
  socket for request/deadline control and letting the response own body reads,
  while preserving the absolute watchdog deadline
- Passed the real close-response regression and both header/body trickle
  deadline cases, then passed 92 related priority tests with five expected
  Windows POSIX skips
- Passed the complete local test directory with 1,697 tests, 100 expected
  platform skips, and the known Starlette deprecation warning
- Passed compileall, bounded Ruff fatal checks, Ruff format verification, and
  `git diff --check` for the correction
- Evidence refs:
- task-11b-rejected-590ccb3-http-response-lifetime
- Blocked on: a fresh immutable source commit and archive, exact simulator
  Linux/image verification, fixed Frigate signal publication, the isolated
  15-minute coexistence gate, and a new uninterrupted 72-hour private soak
- Next step: commit the verified correction, freeze a replacement identity,
  and rerun every identity-bound predeployment gate without reusing the
  rejected candidate's acceptance authority

## DriftCheckDraft

- Scope status: the only source behavior change is within the existing bounded
  priority-probe client; no new service owner, network destination, deletion
  path, GitHub ref, registry tag, or media mutation was introduced
- Compatibility status: loopback source paths, byte limits, strict framing,
  content-type validation, short read timeouts, and the shared three-second
  absolute deadline remain unchanged
- Retirement status: Plex Subgen remains retired; Frigate v0.3 remains stopped
  and preserved for rollback; the rejected v0.5 producer service is stopped
- Test status: local regression, deadline, related-priority, full-suite,
  compilation, formatting, static-fatal, and whitespace checks pass; exact
  Linux and live replacement-candidate evidence remain pending
- Advisory decision: reject `590ccb3`; continue only with a newly frozen and
  fully revalidated replacement candidate

## Checkpoint Update - Restore the 17 GiB Frigate gate boundary

- Captured: `2026-09-04`
- Current todo: freeze a second replacement source identity containing both
  the HTTP response-lifetime fix and corrected 17 GiB automatic-runtime gate,
  then repeat its Linux and host-tool identity checks before profiling
- Active slice: local owner-operated gate tooling and tests; no candidate
  runtime or profiler container has started on Frigate
- Completed todos:
- Loaded the exact `f8fee30` image on Frigate and verified OCI index
  `sha256:136f1bbd6ff33d3c6f779270776ae7cae170d56262bc0d096e4d12c3667dad7a`
  after a hash-matched 4,402,987,008-byte transfer
- Replaced the rejected producer with the `f8fee30` host-side producer and
  observed fresh canonical clear signals, zero producer restarts, and the old
  producer disabled
- Completed a seven-sample, 30-second candidate-absent Frigate admission
  baseline: all 15 cameras remained at ratio 1.0 with zero skipped FPS,
  detection FPS remained below 80, Frigate stayed healthy at zero restarts,
  Ollama stayed unloaded, and about 17,833 MiB VRAM remained free
- Found the frozen gate sampler still required the retired 10 GiB runtime cap,
  contradicting the approved 17 GiB Task 11B and production boundary; the
  sampler correctly blocked before any candidate start
- Updated only the runtime gate boundary and its runtime fixtures to 17 GiB;
  the isolated profiler boundary remains exactly 12 GiB
- Passed 169 focused sampler/observer tests with 18 expected platform skips
  and 155 subtests, then passed the combined repository and owner-tool suite
  with 1,912 tests, 120 expected skips, 155 subtests, and the known Starlette
  warning
- Passed compileall, bounded Ruff fatal checks, Ruff formatting, and
  whitespace validation for the boundary correction
- Evidence refs:
- task-11b-17gib-runtime-boundary-correction
- Blocked on: a fresh immutable commit/archive, simulator Linux verification,
  exact transferred sampler/observer hashes, moved-bind `SIGKILL` lifecycle
  proof, model profiling, and the isolated shared-GPU gate
- Next step: freeze the corrected host-tool source, repeat exact Linux checks,
  transfer the new owner-tool bytes, and perform the required cleanup proof
  before any profiler starts

## DriftCheckDraft

- Scope status: the change aligns an owner-operated acceptance tool with the
  already-approved 17 GiB Frigate runtime; it does not alter the runtime image,
  public defaults, production services, deletion behavior, or media
- Compatibility status: 12 GiB profiler isolation, no-extra-swap enforcement,
  GPU/host reserves, five-minute chunks, and every health threshold remain
  unchanged
- Retirement status: the old 10 GiB gate assumption is retired; `f8fee30`
  cannot authorize Task 11B because its committed sampler still contains it
- Test status: focused and combined local suites plus static/format/compile
  checks pass; exact Linux and live lifecycle evidence remain pending
- Advisory decision: freeze and validate another source identity before live
  profiling

## Checkpoint Update - Cross-platform owner-tool fixtures

- Captured: `2026-09-04`
- Current todo: freeze the final corrected owner-tool source and rerun the
  focused Linux sampler/observer suite from that exact archive
- Active slice: Task 11B test portability only; runtime and safety behavior are
  unchanged and no profiler/candidate container has started
- Completed todos:
- Ran the exact `0627f66` archive on simulator Linux and confirmed the new
  17 GiB sampler bytes were present
- Treated two Linux failures as blockers: the observer tests used Windows-only
  `C:/private` fixtures, making path serialization differ and causing the
  absolute-path boundary to reject the test input on POSIX
- Added one platform-correct private test root and used it consistently for
  observer paths, ordered profiler-chain expectations, and supervisor bundle
  fixtures
- Passed all 83 observer tests on Windows with one expected skip, plus bounded
  Ruff fatal checks, Ruff formatting, and whitespace validation
- Evidence refs:
- task-11b-owner-tool-cross-platform-private-root
- Blocked on: a newly frozen archive and successful exact Linux rerun; the live
  lifecycle, profiling, shared-GPU, and soak gates remain unstarted
- Next step: commit, archive, and execute the exact focused owner-tool suite on
  simulator Linux before transferring the final frozen tool bytes to Frigate

## DriftCheckDraft

- Scope status: test-only path construction changed; no runtime image,
  producer, policy, service, container, network, deletion path, or media changed
- Compatibility status: both platforms now exercise native absolute paths
  while asserting the same CLI ordering and private-path requirements
- Test status: Windows observer tests and static/format/whitespace checks pass;
  exact post-commit Linux evidence remains pending
- Advisory decision: continue local/simulator verification

## Checkpoint Update - Reproducible image build boundary

- Captured: `2026-09-04`
- Current todo: freeze the post-runtime sampler commit, transfer the exact
  runtime image and owner-tool bytes to Frigate, then rerun the moved-bind
  `SIGKILL` cleanup proof before profiling
- Active slice: Task 11B immutable identity transfer and lifecycle preflight;
  no CUDA profiler, candidate transcription, production Home Assistant change,
  GitHub ref, registry tag, or release has started
- Completed todos:
- Reproduced all three remaining Linux fixture failures from the exact
  `f150cd2` archive and corrected only their platform-specific test inputs
- Froze `d311180970e0e5e8a670de066a15a4cd08982fc7`; its exact Linux archive
  passed 2,022 tests with ten expected skips and 157 subtests
- Rejected the resulting image before Frigate transfer when Docker rebuilt its
  final layer despite unchanged runtime blobs
- Traced the changed layer to nested pytest bytecode: the prior
  `.dockerignore` excluded only top-level caches, so extraction-path-specific
  `subgen_core/**/__pycache__/*.pyc` files entered the image
- Added recursive cache exclusions and a packaging regression, then froze new
  runtime commit `c7f1fd1c9a84c54c25f1149f4e9c318a2b2132a5`
- Verified the exact `c7f1fd1` archive on Linux: 1,796 repository tests passed
  with two expected skips, and 227 owner-tool tests passed with eight expected
  skips and 157 subtests
- Built once from a clean extraction and once after pytest populated nested
  caches; both contexts were 2.686 MB and both resolved to image
  `sha256:8828fa4a333cf03ccbba2d9a02c6a9d12e7ab190b051da931f10f2fc09aba0d8`
  with identical ordered layer diff IDs and no image-resident `__pycache__`
- Sealed simulator image archive
  `subgen-v050-image-c7f1fd1.tar` at 4,402,698,240 bytes and SHA-256
  `90ba2884871f074687d52d47296961f88c8f521f4c0ca8b4b9afcf8ae70d6730`
- Evidence refs:
- task-11b-linux-portability-and-reproducible-image-boundary
- Blocked on: exact Frigate transfer and hash readback, sampler commit/blob
  binding, moved-bind lifecycle proof, profiling, runtime qualification,
  isolated coexistence, production MQTT acceptance, and a fresh uninterrupted
  72-hour private soak
- Next step: freeze the unchanged owner tools in a distinct post-runtime
  sampler commit and transfer only the exact `c7f1fd1` runtime identity plus
  that sampler identity to the private Frigate gate root

## DriftCheckDraft

- Scope status: the recursive ignore correction is owned by the approved
  packaging task and removes generated test artifacts from the image; it adds
  no runtime feature, service, network path, deletion path, or media mutation
- Compatibility status: runtime Python sources are unchanged, while build
  contexts are now invariant to prior local test execution and extraction path
- Retirement status: `f8fee30`, `f150cd2`, and `d311180` remain historical
  pre-gate evidence only and cannot authorize profiling, deployment, or soak
- Test status: exact Linux application, owner-tool, clean-build, post-test
  rebuild, image-content, image-ID, size, and layer-list checks pass
- Advisory decision: continue to identity transfer and lifecycle preflight;
  production and publication remain blocked
