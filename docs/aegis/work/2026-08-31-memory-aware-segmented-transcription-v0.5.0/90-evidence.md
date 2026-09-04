# Memory-Aware Segmented Transcription v0.5.0 - Evidence

No evidence has been recorded yet.

## EvidenceBundleDraft

- Artifact key: plan-review
- Type: review
- Source: independent plan reviewer
- Summary: Plan approved after single-flight release, classifier truth-table, compatibility-mode, and secure publication corrections
- Verifier: /root/review_v05_plan

## EvidenceBundleDraft

- Artifact key: workspace-preflight
- Type: command
- Source: aegis-workspace.py check --root .
- Summary: Checker remains blocked only by pre-existing baseline/index/ADR format drift from the v0.4 workspace
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: github-issue-7
- Type: external
- Source: https://github.com/Herbertmt978/Subgen-English-Plex/issues/7
- Summary: Public v0.5 traceability issue opened with approved privacy-safe scope
- Verifier: gh issue view

## EvidenceBundleDraft

- Artifact key: plex-subgen-retirement
- Type: operational
- Source: Plex VM read-only checks plus scoped Subgen retirement
- Summary: Subgen container removed, monitor disabled, no Subgen process or port 9000 listener, Plex HTTP 200, and Compose/model/state/prior image retained with a private recovery manifest
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: frigate-postboot-baseline
- Type: operational
- Source: Frigate VM and Proxmox audit task
- Summary: Balloon floor 20 GiB/max 24 GiB active; 15 cameras live at 8/2/1 fps; Frigate and v0.3.0 Subgen up with zero post-boot restarts; no loaded Ollama model; no disk errors; about 7.5 GiB guest MemAvailable
- Verifier: independent Proxmox audit task

## EvidenceBundleDraft

- Artifact key: frigate-gpu-amendment-final-approval
- Type: review
- Source: independent final amendment re-review
- Summary: Historical approval of the exact-image catalog/identity bootstrap, Task 2A/2B/2C ordering, feasible fallback budgets, unconditional profiler packaging, margin-consistent admission, then-current 12 GiB profiling-only to 10 GiB production requalification, shared-GPU fail-closed behavior, candidate identity chain, unit isolation, and rollback boundaries. The 10 GiB production boundary was superseded on 2026-09-02 by the 17 GiB cap generated from VM 902's 20 GiB guaranteed floor.
- Verifier: /root/review_corrected_gpu_amendment

## EvidenceBundleDraft

- Artifact key: frigate-gpu-amendment-review
- Type: review
- Source: independent plan reviewer and GPU semantics subreviewer
- Summary: First amendment rejected one-snapshot model selection; required exact packaged ModelEnvelope evidence, explicit priority reserve, fail-closed load/reload telemetry, idle resident-model release, a pre-publication isolated Frigate candidate gate, and unambiguous v0.3.0 operational rollback
- Verifier: /root/review_frigate_gpu_amendment

## EvidenceBundleDraft

- Artifact key: corrected-frigate-gpu-amendment-review
- Type: review
- Source: independent second amendment reviewer
- Summary: Large-v3-first, fail-closed telemetry, idle unload, candidate isolation, rollback separation, and Plex retirement were present; review still blocked on an external ModelEnvelope catalog/bootstrap contract, margin-inclusive host/device admission, an executable OCI identity chain, and explicit legacy repair-timer isolation
- Verifier: /root/review_corrected_gpu_amendment

## EvidenceBundleDraft

- Artifact key: frigate-passive-gpu-baseline
- Type: operational
- Source: Proxmox/Frigate audit after balloon activation
- Summary: RTX 3090 total 24,576 MiB, about 18,111 MiB free with Frigate and legacy Subgen resident, no loaded Ollama model, all cameras live, and zero post-boot container restarts. Passive evidence could not bound maximum incremental higher-priority demand, so deployment remains blocked. A future candidate gate proposal uses a 2,048 MiB reaction margin, at least 15 minutes of representative traffic, and immediate abort on Xid/OOM, restart increase, sustained camera-FPS regression, detector stalls/errors, or embedding errors.
- Verifier: independent Proxmox audit task

## EvidenceBundleDraft

- Artifact key: task-2a-final-local-verification
- Type: command
- Source: focused local pytest, Ruff, compileall, and diff checks on commit `18f92b2`
- Summary: 123 Task 2A tests passed; 22 Windows skips were limited to POSIX filesystem/resolver semantics; Ruff format/check, compileall, and whitespace checks passed
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-2a-final-spec-review
- Type: review
- Source: fresh independent Task 2A specification review
- Summary: Passed the approved immutable identity/catalog, strict matching, bounded parsing, canonical integrity, public fallback, canonical fail-closed, and owner-only artifact contracts
- Verifier: /root/rereview_model_catalog_spec

## EvidenceBundleDraft

- Artifact key: task-2a-final-quality-review
- Type: review
- Source: fresh independent adversarial Task 2A security and quality review
- Summary: Approved after primitive revalidation, mandatory canonical provenance, strict resolution states, bounded iterators, Windows fail-closed behavior, held-directory POSIX I/O, and cross-platform malformed-parser regressions
- Verifier: /root/rereview_model_catalog_quality

## EvidenceBundleDraft

- Artifact key: task-2b-final-local-verification
- Type: command
- Source: focused and full local pytest, Ruff, compileall, whitespace, staged-diff, and commit checks on `601efdd`
- Summary: 232 focused resource tests and 714 full-suite tests passed; 82 expected skips and one third-party Starlette deprecation warning remained; formatting, bounded Ruff checks, bytecode compilation, whitespace checks, and staged diff validation passed
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-2b-final-spec-review
- Type: review
- Source: fresh independent Task 2B specification review after the final fail-closed correction cycle
- Summary: Passed host/cgroup/GPU admission, reserve floors, conservative unstabilized CUDA fallback, contradictory-telemetry handling, explicit-model authority, strict PSI, recovery hysteresis, and adaptive chunk-state contracts
- Verifier: /root/task2b_spec_review_v4

## EvidenceBundleDraft

- Artifact key: task-2b-final-quality-review
- Type: review
- Source: fresh bounded adversarial Task 2B quality review on the committed snapshot
- Summary: Approved with no concrete blockers after impossible host telemetry, unstabilized exact-small envelope use, cross-sample GPU conflicts, and reserve-floor fail-open paths were corrected and regression-tested
- Verifier: /root/task2b_quality_review_v4b

## EvidenceBundleDraft

- Artifact key: task-2c-final-local-verification
- Type: command
- Source: focused and full local pytest, Ruff, compileall, privacy, whitespace, staged-diff, and commit checks on `21fbbf9`
- Summary: 358 combined catalog/resource/profiler tests and 738 full-suite tests passed; 82 expected full-suite skips and one third-party Starlette deprecation warning remained; formatting, bounded Ruff checks, bytecode compilation, privacy scan, whitespace checks, and staged diff validation passed
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-2c-final-spec-review
- Type: review
- Source: fresh independent Task 2C specification and security review after the final safe-exit and fresh-load correction cycle
- Summary: Passed exact pinned-revision evidence, workload-policy fidelity, fresh admitted-preload reuse, model-specific descent, owner-only staged writing, verified backend unload, clean-process handoff, and 12 GiB profiling-evidence-only contracts
- Verifier: /root/task2b_spec_review_final

## EvidenceBundleDraft

- Artifact key: task-2c-final-quality-review
- Type: review
- Source: fresh independent Task 2C regression and quality review on the committed snapshot
- Summary: Approved with 24 profiler tests after validation was moved before fresh admission, auto chunking used the Task 2B tier, reserves became positive-only, and fatal configuration errors were separated from unique safe-descent exit code 3
- Verifier: /root/task2b_quality_review_v4

## EvidenceBundleDraft

- Artifact key: task-3-final-local-verification
- Type: command
- Source: focused and full local pytest, bounded Ruff, scoped formatting, compileall, whitespace, staged-diff, and commit checks on `145b83b`
- Summary: 314 focused model/runtime tests passed with 22 platform skips and 162 deselections; 26 module-boundary tests passed; the full suite passed 819 tests with 82 expected skips and one third-party Starlette deprecation warning; no GitHub-hosted runner was used
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-3-final-concurrency-review
- Type: review
- Source: independent narrow concurrency re-review after the terminal-profile wakeup correction
- Summary: Passed single-flight release coordination and confirmed that a waiter already inside controller recovery wakes promptly, raises `ModelLoadProfileUnhealthy`, and leaves admission fail-closed
- Verifier: /root/task3_concurrency_review

## EvidenceBundleDraft

- Artifact key: task-3-final-failure-attribution-review
- Type: review
- Source: independent runtime-error attribution review
- Summary: Passed all pathless model-runtime controls; terminal load, release, cancellation, and pressure-yield errors cannot reach media marking/deletion or clear unrelated task state
- Verifier: /root/task3_failure_attribution_review

## EvidenceBundleDraft

- Artifact key: task-3-final-test-gap-review
- Type: review
- Source: independent Task 3 telemetry and regression-gap review
- Summary: Passed after status reporting separated immediate GPU total capacity from stabilized free capacity and covered the corrected flow with a regression
- Verifier: /root/task3_test_gap_review

## EvidenceBundleDraft

- Artifact key: task-4-final-local-verification
- Type: command
- Source: focused and full local pytest, bounded Ruff, scoped formatting, compileall, whitespace, staged-diff, and commit checks on `84e89cf`
- Summary: 29 segmentation tests and 71 model-runtime tests passed together; 41 pressure/segmentation boundary tests passed with 97 deselections; the full suite passed 849 tests with 82 expected skips and one third-party Starlette deprecation warning; no GitHub-hosted runner was used
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-4-final-concurrency-review
- Type: review
- Source: independent Task 4 pressure/concurrency review after two correction loops
- Summary: Passed after cancellation was moved before shrink/recovery, model cleanup was deferred until payload release, and generation-bound release tickets prevented a delayed yielded worker from unloading a newly reloaded model
- Verifier: /root/task3_concurrency_review

## EvidenceBundleDraft

- Artifact key: task-4-final-failure-attribution-review
- Type: review
- Source: independent Task 4 structured-copy and failure-attribution review
- Summary: Passed midpoint ownership, timestamp offsets, immutable staging, monotonic rejection, language/ID/back-reference construction, mixed wordless content, and deliberate removal of backend-local faster-whisper `seek` frame indices
- Verifier: /root/task3_failure_attribution_review

## EvidenceBundleDraft

- Artifact key: task-4-final-test-gap-review
- Type: review
- Source: independent Task 4 regression-gap re-review of the settled implementation
- Summary: Passed after the real model-runtime plus segmentation boundary proved audio collection before allocator cleanup, same-cursor retry, local-seek removal, and stale-generation release suppression
- Verifier: /root/task3_test_gap_review

## EvidenceBundleDraft

- Artifact key: task-5-final-local-verification
- Type: command
- Source: focused and full local pytest, bounded Ruff, scoped formatting, compileall, whitespace, staged-diff, and commit checks through `3d9ae82`
- Summary: 133 final focused segmentation/transcription/model-runtime tests passed; the complete suite passed 886 tests with 82 expected skips and one third-party Starlette deprecation warning; selected-stream extraction, pressure/allocation retry, upload bypass, atomic SRT/LRC publication, stable-ts extension handling, permission/fsync ordering, and unsupported network-directory sync behavior were covered without GitHub-hosted runners
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-5-final-adversarial-review
- Type: review
- Source: independent Task 5 adversarial review and re-review after the stable-ts staging and durability correction cycle
- Summary: Passed generation-ticket/release ordering, cancellation, adaptive retry, selected-track parity, stable-ts `WhisperResult`/`Segment` construction, `.tmp.srt` staging, readable-mode inode sync, post-commit directory-sync warnings, exactly-once completion, and explicit unsegmented-upload limitations
- Verifier: /root/task5_final_review

## EvidenceBundleDraft

- Artifact key: task-6-final-local-verification
- Type: command
- Source: focused and full local pytest, bounded Ruff, compileall, whitespace, staged-diff, and commit checks on `f558114`
- Summary: 204 focused media/transcription tests passed; the complete local suite passed 961 tests with 82 expected skips and one third-party Starlette deprecation warning; dual-validator aggregation, bounded subprocesses, PyAV isolation, generation changes, exact metadata handoff, and typed worker events were covered without GitHub-hosted runners
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-6-final-adversarial-review
- Type: review
- Source: independent media-flow and validator reviews followed by a settled-diff release-blocker review
- Summary: Passed after generation checks were extended through detection, chunks, and publication; missing duration was rejected before model load; reserved queue fields were protected; multi-track PyAV evidence and validator outcomes were preserved; descendant-inherited output handles were bounded; and malformed stream metadata remained indeterminate rather than becoming deletion authority
- Verifier: /root/task6_flow_review and /root/task6_validator_review

## EvidenceBundleDraft

- Artifact key: task-7-final-local-verification
- Type: command
- Source: final focused and complete local pytest, bounded Ruff, compileall, whitespace, staged-path, and commit checks on `2e96cb2`
- Summary: 125 focused marker/monitor/repair tests passed with 33 platform skips; the complete local suite passed 1,023 tests with 79 expected skips and one third-party Starlette deprecation warning; no hosted runner, real-media deletion, or live deployment was used
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-7-final-linux-verification
- Type: command
- Source: checksum-verified local patch applied to public base `af11933` in a dedicated Ubuntu 24.04 simulator venv
- Summary: All 158 Linux marker/monitor/repair tests passed, including descriptor-relative unlink, marker durability, flock, symlink, hardlink, and recovery cases; the complete simulator suite passed 1,101 tests with one expected skip and one third-party Starlette warning. The task-owned artifacts were removed and the task-woken simulator was shut down and verified offline
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-7-final-security-review
- Type: review
- Source: independent monitor/deletion and repair/state re-reviews after the final fail-closed corrections
- Summary: Passed the exact typed dual-invalid proof chain, source and delete identity checks, canonical path binding, durable marker re-read, current recovery context, monitor-only secure unlink, generic/crash/resource retention, report-only repair, legacy intent preservation, bounded parsing, and lifetime-lock requirements
- Verifier: /root/task6_flow_review and /root/task6_validator_review

## EvidenceBundleDraft

- Artifact key: task-8-final-root-verification
- Type: command
- Source: final focused local pytest, compileall, six Compose validations, contract audit, whitespace check, staged-path review, and commit `70512f3`
- Summary: 138 packaging/module/monitor tests passed with 17 expected Windows platform skips; bytecode compilation passed; all three base profiles and all three base-plus-ModelEnvelope-overlay combinations validated; exact release/config/version/systemd contracts and the reviewed staged diff passed without a GitHub-hosted runner or live-system action
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-8-final-spec-review
- Type: review
- Source: fresh independent specification reviews across the original candidate and every correction
- Summary: Passed profiler packaging, public fallback and opt-in exact-evidence overlay semantics, automatic model/segmentation/pressure defaults, strict artifact identity/modes, invalid-media-only deletion, report-only repair, Frigate reserve gate, rollback separation, stable runtime/project versions, exact release structure, proposed ADR status, and historical v0.4 preservation
- Verifier: /root/task8_spec_review

## EvidenceBundleDraft

- Artifact key: task-8-final-quality-review
- Type: review
- Source: fresh independent operational and maintainability reviews after first-run, report-evidence, cleanup-default, migration, and UID-gate corrections
- Summary: Passed after ordinary base profiles gained a real missing-evidence fallback, the exact overlay disabled host-path creation, the human-readable report exposed bounded typed validator evidence, CPU/GPU cleanup defaults rendered as 60/60/300 seconds, migration listed all three mounts, and the Linux gate validated configured PUID before requiring runtime exact-match acceptance
- Verifier: /root/task8_quality_review

## EvidenceBundleDraft

- Artifact key: task-8-release-note-humanity-review
- Type: review
- Source: local content-humanizer audit of `docs/RELEASE_NOTES_0.5.0.md`
- Summary: Scored 96/100 with good sentence variance, acceptable passive voice, no canned AI vocabulary, and no hedging; the reviewed file remains the required byte-for-byte GitHub release body
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-9-complete-local-verification
- Type: command
- Source: complete local pytest, compileall, six Compose validations, `origin/main...HEAD` whitespace/stat/history checks, and clean-worktree review after commits `70512f3` and `728e6f6`
- Summary: 1,049 tests passed with 79 expected Windows platform/dependency skips and one unchanged third-party Starlette deprecation warning; compilation and every base/base-plus-overlay Compose configuration passed; no GitHub-hosted runner, image build, real-media mutation, or live-system action was used
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-final-platform-verification
- Type: command
- Source: fresh Windows verification and checksum-matched Ubuntu 24.04 simulator source at runtime commit `4418b3c97296a04b311d29d9ce52abefef64e108`
- Summary: Windows passed 1,058 tests with 79 expected skips; Linux passed 1,136 tests with one expected skip. Compileall, bounded Ruff, whitespace, and every base/base-plus-ModelEnvelope Compose rendering passed without a GitHub-hosted runner.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-exact-image-and-package
- Type: artifact
- Source: locally built exact linux/amd64 candidate plus packaged/source-mounted HTTP and identity checks
- Summary: Historical pre-priority-amendment evidence only. The candidate passed HTTP 200 smokes with zero restarts and was frozen as config `sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae`, platform manifest `sha256:9e557e124ca6994c4aa30af77301a75d31145e53ec17e6b18997969c67308b5b`, OCI index `sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc`, and 19 ordered diff IDs. The 4,402,460,160-byte archive hashes to `73f45dc1721a804d569359f5afd51068be5b2d9c562729d4d4a61fd2f5e8bce9`; its owner identity file hashes to `5d3a7e7d5839a9496ef05cddcd8b10c8a71e04f8676243c5ed90ae1968fff87c`. None can authorize publication or deployment after Task 11A.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-installed-callback-propagation
- Type: command
- Source: exact packaged stable-ts/faster-whisper adapter probe
- Summary: The installed progress callback propagated `MemoryPressureYield` out of inference and consumed only the first yielded segment, proving cooperative pressure control reaches the packaged backend boundary.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-constrained-inference
- Type: command
- Source: exact candidate processing the same 31-minute synthetic source under 4, 6, and 9 GiB hard/no-extra-swap cgroups
- Summary: The 4 GiB run selected `small`, completed four bounded windows with 405 monotonic cues, peaked at 2,499,293,184 bytes, and recorded no swap/OOM/restart. The 6 GiB pressure run selected `small`, unloaded under sustained same-cgroup pressure, retried cursor zero with a 605-to-305-second shrink, recovered and regrew, published one 405-cue monotonic SRT, and recorded no swap/OOM/restart. The 9 GiB run selected `medium`, completed two bounded windows with 403 monotonic cues, peaked at 3,608,408,064 bytes, and recorded no swap/OOM/restart.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-disposable-media-safety
- Type: command
- Source: exact packaged validators plus isolated monitor state and disposable Docker-volume media
- Summary: A real silent container classified `no_audio` and remained; deterministic junk classified dual-invalid, emitted the five-field generation identity, was durably marked before deletion, and was the only deleted test file. A valid audio replacement at the same path produced a stale marker and re-entered the queue. No real media was touched; the final real-Linux host/container bind identity proof remains a Task 11B gate.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11b-safe-handover-and-rollback
- Type: command
- Source: immutable legacy-container/process/unit readback, owner-only stopped-state capture, complete file/archive hashing, OCI blob validation, and isolated containerd import on Frigate
- Summary: The v0.3 container was stopped only after its post-crash state proved scan-only with no worker/FFmpeg child, no OOM, and restart count one. Monitor and repair execution was isolated first. The complete config/model/state tree was sealed as a 4,799,109,120-byte archive at `28da1de7f02ab7968f904e387e7484295119ed0b05b69a9f2d1bc45c48543408`. A metadata-only Docker export was rejected; the corrected 4,403,125,248-byte linux/amd64 OCI rollback archive hashes to `c2b5594bb96b66569c34000b27ec0070d66a5d997a680f0a834b4837afab99be`, contains all 16 referenced layers, and imported successfully in an isolated namespace with the recorded v0.3 manifest/config identities. No media file, Frigate camera setting, Ollama state, or other container changed.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11b-sampler-final-local-verification
- Type: command
- Source: exact uncommitted sampler/test bytes after the 30-run correction, executed on Windows and WSL/Linux before `SAMPLER_COMMIT`
- Summary: Historical pre-priority-amendment sampler evidence only. Windows passed 44 tests with seven expected platform skips and 79 subtests; direct WSL/Linux unittest passed all 51 tests. Python bytecode compilation, bounded Ruff (`E9`, `F63`, `F7`, `F82`), Ruff formatting, and `git diff --check` passed. The exact pre-commit SHA-256 values are sampler `0d10f738d42686562f2000d47bd091e45474cbc9b1a8d52930f8a12eda6a8336` and test `875bf2dc032d48be7ee1775844ed40842df403b47cf7863218a9f0ed3b1d9c96`; they cannot authorize the amended gate.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11b-sampler-final-independent-review
- Type: review
- Source: fresh independent review of schema-3 boundary enforcement, immutable-ID cleanup, profiler hold/receipt/catalog handling, runtime arithmetic, evidence ordering, and the final medium/30-run correction
- Summary: Historical pre-priority-amendment review only. No P0/P1 remained for that boundary. The inclusive 3-to-30 validator admitted the then-required medium profile, but the reviewed sampler lacks the required signal/mount/causal-yield contract and cannot authorize Task 11B after Task 11A.
- Verifier: /root/frigate_gate_runbook

## EvidenceBundleDraft

- Artifact key: task-12-anonymous-engine-preflight
- Type: command
- Source: live read-only context/Engine-ID inventory followed by local package provisioning and empty-engine verification on the task-owned simulator
- Summary: The simulator's Windows `default` and `desktop-linux` contexts resolved to the same non-empty Docker Desktop Engine and were rejected as false isolation. Docker Engine 29.1.3 was installed in the existing Ubuntu 24.04 WSL environment; its daemon is active on its local Unix socket, has a distinct Engine ID, and contained zero images and zero containers. No GitHub runner or registry mutation was used. The final release gate must still use an empty task-scoped Docker configuration, perform the anonymous digest pull/HTTP smoke, clean every exact task-owned object, and prove this engine empty again.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11b-parked-v03-restart-policy
- Type: command
- Source: exact full-ID Docker inspect/update/readback against the already stopped Frigate v0.3 rollback container
- Summary: The immutable rollback container matched full ID `b5cc4d8fca716e7b86cff5ef04605d8ba45a2c0e42acc4561d1fd95cb97b250a`, Compose project/service `subgen`, stopped state, restart count one, and original restart policy `always`. An owner-only mode-0600 record preserved that original policy and the required parked `no` policy at SHA-256 `fbd25cf5e8bb6f2278a526cda5b08bddca24ab1a59b95e7be45ef5ec03e5b5be`; `docker update --restart=no` then changed only that exact stopped container and a fresh readback proved `exited|false|no|1`. Restore `always` only inside the verified deletion-off v0.3 rollback transaction.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11b-resume-full-local-verification
- Type: command
- Source: fresh post-restart verification of the frozen runtime plus the approved owner-operated gate tooling on the local workstation
- Summary: Historical pre-priority-amendment regression evidence only. The complete local suite passed 1,102 tests with 86 expected platform/dependency skips, 79 subtests, and one known third-party Starlette deprecation warning. Bytecode compilation and all six base/base-plus-ModelEnvelope Compose renderings also passed. GitHub Actions was not invoked; the result does not cover or authorize the Task 11A signal contract.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-12-publication-review-open
- Type: review
- Source: combined independent review of the version and mutable-`latest` publication recovery paths
- Summary: Version-tag publication recovery is now callable and identity-bound. The mutable-`latest` recovery transaction still needs executable-code alignment for exact path, release-identity, push-quiescence, and explicit non-CAS residual handling. That work is isolated from Task 11B and remains a hard blocker before any GitHub/GHCR mutation; it does not block committing or running the already approved local Frigate health sampler.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11b-gate2-diagnostic-capacity
- Type: command
- Source: exact five-minute `large-v3` profiler candidate under the reviewed
  Task 11B sampler and full-ID cleanup supervisor
- Summary: The profiler wrote the expected create-once return-code-3 safe-
  capacity receipt and promoted no catalog, but the shared-health gate aborted
  at about 75 seconds on the camera skipped-FPS boundary. The run has no final
  observation/pass seal and cannot authorize `medium`. Profiler result/stdout
  SHA-256 values are `40a3e7ffbd74d2b74feb6dd6bab6ab316fb27ffee12ed7f804185168bc45375e`
  and `6d7cc601c2f021a80685dd7489c226e1fe9bfbeb5fe046ddde268cdf527ab31d`;
  JSONL/seal/wrapper SHA-256 values are
  `40002301d5fe9b33658934341b4b6a0e3ae4621f4c03ced292633b5fa776b589`,
  `ae7c3ebc31dc8175a3219e25b5e7f9638edfdd47508b5e4873a940d8ae7ef573`,
  and `22200fbd46eb4231d04bb9a8a3cf73a168974705826efab3a862f29556ab4804`.
  Candidate OOM/restart remained false/zero, exact-ID cleanup passed, Frigate
  remained healthy at restart zero, and Ollama remained unloaded.
- Verifier: root coordinator plus independent gate-failure analysis

## EvidenceBundleDraft

- Artifact key: task-11b-frigate-only-control-failed
- Type: command
- Source: create-once owner-only candidate-absent 900-second Frigate control
  using the frozen 15-camera map, five-second cadence, and unchanged thresholds
- Summary: The 181-sample/900.071-second seal recorded
  `clean_for_gate=false`, 51 skipped-FPS breach samples, 45 low-ratio samples,
  75-second longest skipped and low-ratio streaks, maximum 6.3 skipped FPS,
  minimum 0.2 process ratio, and zero invalid environment samples. Frigate
  remained running/healthy with no restart/OOM change and Ollama remained
  unloaded. The mode-0600 root-owned JSONL and seal SHA-256 values are
  `68486f7d0159170737b616808549452b1da6a796bd599a65377402fb1b2c3d7d`
  and `163605cb9ac4806708febe96ee902046379c366e1223a8ce3194a35bee70f564`.
  Roughly 17.5 GiB free VRAM during the breach disproves free VRAM as shared-
  GPU compute authority. No candidate may run under the superseded policy.
- Verifier: root coordinator plus independent shared-GPU policy audit

## EvidenceBundleDraft

- Artifact key: task-11b-runtime-observer-final
- Type: command and review
- Source: committed runtime observer/test files at commit `7254df3` plus fresh
  coordinator verification on Windows and WSL/Linux
- Summary: Historical pre-priority-amendment observer evidence only. Exact startup/workload isolation, matching owner-only API key,
  deterministic task/type/source-identity lifecycle pairing, lock-atomic phase
  freshness, and exact non-ignored `ExecStopPost` parsing are enforced without
  secret disclosure. Windows passed 86 tests with seven expected skips and 106
  subtests; Linux passed 93 tests and 106 subtests; bounded Ruff, Ruff format,
  compileall, and whitespace checks passed. Independent final review found no
  P0/P1. Observer/test SHA-256 values are
  `f0935a1ff8febd58e53653142c8228390d70f0e2bc6bb05368897a47b012f038`
  and `ee23a769e89d2dcc8ee6b2d2df94483d4ee2ecb77b7789611949415a9b33bcfc`.
  The observer lacks the required signal/mount/policy and causal generation
  checks, so commit `7254df3` cannot authorize the amended runtime gate.
- Verifier: root coordinator and `/root/runtime_observer_impl/observer_review`

## EvidenceBundleDraft

- Artifact key: task-11a-frigate-contention-predictor
- Type: command and review
- Source: owner-only candidate-absent 900-second aggregate Frigate diagnostic,
  threshold replay, Frigate 0.17.2 source-generation verification, and
  independent gate-policy review
- Summary: The root-owned mode-0600 trace completed successfully with 181
  samples over 900.023 seconds: 42 health-boundary breach samples, maximum 6.4
  skipped FPS, minimum 0.2 process ratio, breach detection load 88.9-123.8 FPS,
  and clean detection load 32.6-94.0 FPS. A private two-distinct-generation
  predictor at total detection FPS at least 80 caught 41/42 breach samples and
  anticipated three of four episodes, while conservatively asserting for
  22/139 clean samples. It is therefore operator-specific pause policy, not a
  public Frigate threshold. The evidence/seal/script SHA-256 values are
  `29ee33ec53116e7abc4aaafe82e55749ebcd4827c86dfef0bfb1bad08ec85988`,
  `d26e235da5f75c895d47b9ca678f671ad6c351876cd528b8b7c0b9e6f77d03eb`,
  and `9a7df5aede4262cdb168d2455287c041eb5698292b65331a165dc58c413875e6`.
  The independent review required distinct `service.last_updated` generations,
  causal observation/release/load evidence, one separate real assertion/reload
  proof, and a reset followed by an uninterrupted 900-second clear pass.
- Verified source fact: Frigate 0.17.2's generated stats snapshots use
  `service.last_updated` as their source-generation timestamp, and the live
  loopback `/api/stats` response returned that field as a JSON integer. Repeated
  five-second polls of the same integer are therefore one source generation,
  not independent assertion or recovery evidence.
- Verifier: root coordinator and `/root/priority_gate_review`

## EvidenceBundleDraft

- Artifact key: task-11a-amendment-era-evidence-retirement
- Type: governance boundary
- Source: cross-record identity and acceptance review after adopting the
  owner-only higher-priority signal, causal unload proof, and two-phase gate
- Summary: Every pre-amendment publication artifact rooted in runtime commit
  `4418b3c97296a04b311d29d9ce52abefef64e108`, OCI index
  `sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc`,
  inner config
  `sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae`,
  sampler commit `86ac798`, or observer commit `7254df3` is retired as
  root-cause, cleanup, or rollback evidence only. This includes their
  ModelEnvelope, lifecycle, profiler, sampler, observer, platform, and Gate-2
  results; no combination can authorize packaging, publication, or deployment.
  The sealed v0.3 identity remains valid only for deletion-off rollback. A new
  post-amendment runtime/image/identity/catalog, all four freshly frozen gate
  programs, a causal assertion-unload-recovery Phase A, and a separate exact-
  clear 900-second Phase B are mandatory.
- Verifier: root coordinator plus independent amendment-governance review

## EvidenceBundleDraft

- Artifact key: task-11a-post-restart-contract-audit
- Type: local verification and independent review
- Source: amended design/ADR/plan plus a fresh complete Windows pytest baseline
  from the approved local virtual environment; GitHub-hosted runners were not
  invoked
- Summary: The local suite completed with 1,142 passed, 86 expected skips, 106
  subtests, and two failures isolated to the draft v0.5 release-note wording.
  Independent governance review found and drove closure of receipt-journal,
  ordered Phase-A/Phase-B workload, failure-counter, Docker/cgroup/CUDA/Xid, and
  three-second sample-spacing gaps. A separate publication audit identified five
  remaining Task 12 blockers: working-tree verifier execution, non-authoritative
  release absence, newline-normalized body comparison, incomplete hosted-run
  baselining, and a race-prone ordinary version-tag push. Those findings remain
  hard blockers until the corrected plan receives a fresh explicit PASS; no
  remote or live mutation occurred.
- Verifier: root coordinator plus `/root/governance_schema_recheck`,
  `/root/release_chain_recheck`, and `/root/release_patch_design`

## EvidenceBundleDraft

- Artifact key: task-11a-priority-consumer-final-local-verification
- Type: local command verification
- Source: current Task 11A generic priority reader/controller/runtime working
  tree using the approved local Windows virtual environment; GitHub-hosted
  runners were not invoked and no live host was changed
- Summary: Reader-only regression verification passed 28 tests. The integrated
  priority/startup/probe/controller/model-runtime/status surface passed 474
  tests. The complete repository suite then passed 1,209 tests with 86 expected
  skips, one known Starlette deprecation warning, and 106 subtests. Bounded Ruff
  `E9,F63,F7,F82`, Python `compileall`, and `git diff --check` passed; the latter
  reported only normal LF-to-CRLF working-copy notices. The final regressions
  prove serialized reader transactions, exact old-epoch replay rejection,
  retained current-epoch `+1` recovery, and terminal fail-closed history
  saturation.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11a-priority-consumer-final-independent-review
- Type: independent code review and focused command verification
- Source: full generic priority reader/controller/runtime diff after the replay
  and concurrency corrections
- Summary: The final reviewer found no remaining P0/P1 issue. It verified that
  the reader lock covers the complete stateful transaction, accepted-epoch
  history is exact and non-evicting, replay rejection precedes checkpoint writes,
  saturation remains terminal, and the current epoch's exact next publication is
  the only post-replay recovery path. Its independent focused cross-layer run
  passed 447 tests with the same known Starlette warning; source compilation and
  whitespace checks passed.
- Verifier: `/root/priority_runtime_final_review`

## EvidenceBundleDraft

- Artifact key: task-11a-priority-consumer-workspace-structure
- Type: Aegis structural bundle and check
- Source: installed zero-dependency `aegis-workspace.py` against the current
  target repository and active v0.5.0 work record
- Summary: `bundle` completed and generated the indexed advisory
  `proof-bundle.md` plus `gate-input-pack.json`. The subsequent `check` exited 1
  on pre-existing workspace drift: three missing phrases in
  `BASELINE-GOVERNANCE.md`, older unindexed Markdown records, and the two legacy
  ADRs' pre-current filename/section convention. It did not report a missing
  index entry for either newly generated artifact. These structural findings do
  not prove or disprove runtime behavior, remain outside this cohesive source
  slice, and prevent any claim that the whole Aegis workspace is clean.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11a-priority-producer-final-local-verification
- Type: local cross-platform command verification
- Source: final distinct-generation Frigate host producer, generic signal
  encoder/reader, controller recovery high-water, optional package surfaces,
  and human release documentation; no GitHub-hosted runner or live host was
  used
- Summary: The complete Windows repository suite passed 1,267 tests with 91
  expected platform skips and 106 subtests. The focused Windows producer,
  reader, controller, and packaging surface passed 363 tests with five POSIX
  skips. A disposable Ubuntu Python 3.12 venv then ran that final focused
  surface with every POSIX boundary active: 368 passed and zero skipped; the
  venv was removed and the previously stopped distro was terminated and
  verified stopped. Package/module contracts passed 88 tests. All ten required
  base and overlay Compose renderings passed. Python compileall, bounded Ruff
  `E9,F63,F7,F82`, formatting of the two new producer files, and
  `git diff --check` passed; line-ending notices were informational. Exact Git
  ignore checks reject both the documented private draft and canonical policy
  basenames. Real loopback drip-header/body tests prove the absolute shared
  three-second HTTP deadline, and regressions cover FIFO/nonblocking reads,
  descriptor-bound leaf swaps, numeric overflow, reason unions,
  restart/checkpoint duplicate generations, and exact three-distinct-clear
  recovery.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-11a-priority-producer-final-independent-review
- Type: independent contract, security, and release-surface review
- Source: final producer/consumer/controller/package/documentation working tree
  after deadline, checkpoint, boot-order, upgrade, and private-policy boundary
  corrections
- Summary: Independent contract, security, and release reviewers reported no
  remaining P0/P1 or release-blocking P2 findings. The security review verified
  the shared HTTP deadline, socket-shutdown watchdog, EOF rejection, timer
  cancellation/join, nonblocking inode-bound signal read, outside-checkout
  private-policy boundary, exact ignore defenses, and producer boot ordering
  without a Docker health dependency. The contract review verified epoch-start,
  asserted-state, and checkpointed duplicate generations cannot advance
  recovery, numeric overflow maps fail-closed, and source/Ollama reason unions
  preserve immediate degradation. Release review verified that the human notes
  lead with long-file adaptive segmentation, upgrades retain the selected base
  plus active overlays, and fixed producer constants are unambiguous. Official
  Frigate v0.17.2 source confirms `/api/version` is plain text; implementation,
  tests, design, and operator documentation agree.
- Verifier: `/root/priority_producer_security_review`,
  `/root/priority_producer_contract_review`, and
  `/root/frigate_producer_patterns`

## EvidenceBundleDraft

- Artifact key: task-11a-runtime-receipt-integration-local-verification
- Type: local cross-platform command verification plus independent defect
  reproduction
- Source: current Task 11A application/runtime working tree on Windows and one
  disposable Ubuntu Python 3.12 venv; no GitHub-hosted runner, live host,
  container, media library, registry, GitHub ref, or release was changed
- Summary: The focused Windows receipt/controller/transcription/worker surface
  passed 458 tests with four expected skips. The full Windows application suite
  passed 1,245 tests with 88 expected skips. A same-command disposable Ubuntu
  venv then ran the full POSIX application suite with 1,332 passed, one expected
  platform skip, and only the known Starlette deprecation warning before the
  venv was removed. Bounded Ruff `E9,F63,F7,F82`, compileall, and whitespace
  checks passed. Independent review reproduced three gate-only gaps in the
  prior snapshot: controller visibility before receipt durability, warning-only
  parent-directory fsync, and a missing `worker_error` failure generation. The
  corrected focused suite proves controller readers remain blocked until the
  receipt observer returns, a gate directory-sync failure aborts without
  completion, and only marker-worthy worker errors advance the added failure
  class. Fresh independent re-review remains pending and this evidence does not
  authorize Task 11B or publication.
- Verifier: root coordinator plus `/root/runtime_integration_review`

## EvidenceBundleDraft

- Artifact key: task-12-publisher-final-local-verification
- Type: local cross-platform command and failure-injection verification
- Source: current Task 12 publisher/journal/source materializer, anonymous-
  engine client, gate tooling, tests, design, and human release notes on the
  local Windows workstation and one disposable Ubuntu Python 3.12 environment;
  no GitHub-hosted runner was invoked
- Summary: The publisher now binds retained probe winners to exact strong
  ETags; enforces one strict initial checkpoint and monotonic journal state;
  anchors the terminal sequence/SHA-256; and uses durable staged, atomic,
  no-replace transaction/receipt/head publication with exact strict-prefix
  torn-write repair. Armed or ambiguous `latest` is never retried or unlocked
  while the prior digest remains; exact lock-create/remove pending states can
  recover without duplicate mutation. The anonymous Docker smoke uses one
  long-lived `system dial-stdio` Engine API stream with repeated daemon-ID,
  strict bounded framing, short-read, trailing/prequeued-byte, and guaranteed
  cleanup enforcement. Exact committed Git blobs run only from a private
  materialization under isolated Python; Git replacement/shallow/graft state,
  duplicate headers, foreign journal bytes, aliases, and credential leakage
  fail closed. The Task 12 suite passed 173 tests with two expected skips. The
  combined gate/publication matrix passed 364 tests, 22 expected skips, and all
  127 subtests. The full Windows repository suite passed 1,622 tests, 110
  expected skips, and all 127 subtests with only the known Starlette warning.
  A disposable Ubuntu run passed 1,724 tests, eight expected skips, and all 129
  subtests with the same warning; bounded Ruff, targeted formatting,
  compileall, and whitespace checks passed. Its temporary tree was removed and
  both Ubuntu and Docker Desktop were verified stopped. No live host, media,
  GitHub ref/release, GHCR release tag, or deployed container changed.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-12-publisher-final-independent-review
- Type: independent adversarial code review and fresh focused verification
- Source: final shared Task 12 worktree after journal, mutable-tag, lock,
  source-proof, Docker-stream, and cleanup corrections
- Summary: The reviewer reported no remaining P0, P1, or P2 finding and passed
  173 tests with two expected skips. Additional read-only probes covered low-
  level journal crash points, torn staged/final files, stale transaction replay,
  retained-ETag replacement, unresolved `latest`, lock reconciliation, and
  Docker HTTP fragmentation, framing, trailing-byte, and cleanup behavior. The
  accepted residual trust boundary is explicit: a same-owner/admin actor could
  coherently replace the local journal and head without an external monotonic
  service; GHCR conditional and strong-ETag semantics are trusted; and the
  root-controlled Docker CLI/socket/daemon is trusted. A compromised daemon
  could forge its identity and transcript. Within the supported boundary,
  ambiguous state blocks, preserves evidence, and retains the publication lock.
- Verifier: `/root/task12_postfix_adversarial`

## EvidenceBundleDraft

- Artifact key: task-10-post-amendment-runtime-and-image-freeze
- Type: immutable source and container-image identity
- Source: local Git commit 3bef1fe and simulator Docker image inspect
- Summary: The post-amendment v0.5.0 runtime is frozen at commit 3bef1fe335ae9c1132cd78ea8c25f727e0fa5453. The simulator candidate tag subgen-english-plex:v0.5.0-candidate-3bef1fe resolves to image sha256:4296fc6af2b406264b06eb8f4a9f032a26147de4f6ae3638806f552001c6f6a7. No mutable release tag or GitHub ref was created.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-cpu4-constrained-inference
- Type: exact constrained CPU transcription gate
- Source: sealed simulator 4 GiB candidate evidence for the frozen image
- Summary: The exact 4 GiB/no-swap CPU candidate completed the 31-minute speech fixture with the auto-selected small model and produced one valid SRT containing 405 monotonic cues. Peak memory was 2,422,546,432 bytes; swap remained zero; cgroup OOM events, OOMKilled, and restart count remained zero.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-10-cpu6-pressure-attempts-9-through-11
- Type: sealed same-cgroup pressure-attempt evidence
- Source: simulator attempt directories 9, 10, and 11 under the immutable task root
- Summary: Attempts 9-11 were each treated as new one-shot candidates and sealed inconclusive, never passed or reused. Attempt 11 recorded one HTTP request and one helper launch, a safe 3,296,722,944-byte fixed ramp, 527,945,728-byte post-calibration median, zero swap/events/OOM/restarts, complete mapping release, a final manifest SHA-256 7581a699701919e393a7d346b078a2a546d6e76d10709e2e725c65c2c0d41ec8, and verified removal of its exact container. Its missing run-start artifact is explicitly disclosed by continuity-recovery-note.json.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-12-profiler-source-proof-chain
- Type: local publication source-proof verification
- Source: commits fe08588 and eb73b25 plus focused Windows tests and independent review
- Summary: The release verifier now binds immutable profiler evidence/seal/boundary triples through config v3 and request v2 and includes all five profiler publication paths in the exact runtime-to-release status manifest. A real temporary-repository diff regression covers the chain. The focused suite passed 176 tests with 6 expected skips; Ruff, formatting, compilation, diff checks, and independent review passed.
- Verifier: root coordinator and /root/source_proof_review

## EvidenceBundleDraft

- Artifact key: task-10-pressure-helper-v7-local-verification
- Type: reviewed one-shot pressure-helper verification
- Source: D:\CodexTemp\subgen-v050-pressure-harness-3bef1fe-main\pressure_helper_v7.py
- Summary: Pressure helper v7 is frozen at SHA-256 8aaa6e5802c37015ad762bee369df5068134bcf06e78a02d605a9fb2c52582b5. It plans the full bounded correction before mutation, reclaims supplemental mappings before whole 8 MiB ramp mappings, caps normal correction at 384 MiB, latches allocations closed before reclaim, preserves the 480/384 MiB floors, performs one fail-closed re-settle, and structurally audits all post-latch allocation records. Six focused tests, compilation, Ruff, formatting, and final independent review passed.
- Verifier: root coordinator and /root/pressure_calibration_review

## EvidenceBundleDraft

- Artifact key: task-10-adaptive-capacity-pre-freeze-verification
- Type: local cross-platform, exact-backend, Compose, and disposable-cgroup
  verification
- Source: the current adaptive-capacity working tree on Windows and the
  task-owned simulator's Linux Python 3.10/Docker environment; no
  GitHub-hosted runner or live Frigate container was used
- Summary: The complete Windows suite passed 1,566 tests with 100 expected
  skips and one known third-party warning in 53.23 seconds; compileall and the
  focused Ruff surface also passed. The complete Linux Python 3.10 suite passed
  1,661 tests with one expected skip in 94.13 seconds and no container OOM.
  The initial exact stable-ts comparison stopped at a comparator/normalization
  discrepancy, which was treated as a pre-freeze blocker and corrected before
  a distinct stable-ts 2.19.1 Linux run passed 52 tests in 7.54 seconds. On an
  actual 12 GiB input, the configurator generated `10240m` for both the hard
  memory and memory-plus-swap limits while retaining a 2,048 MiB host reserve.
  All ten Compose base/overlay combinations rendered. A live disposable probe
  confirmed Docker Memory and MemorySwap were both 10,737,418,240 bytes,
  `memory.swap.max` was zero, and `oom_score_adj` was 1000. Removing the
  generated capacity file made Compose rendering fail closed. This is
  pre-freeze evidence only: no fresh immutable candidate was built or
  authorized, and no live Frigate deployment, public ref/release, registry
  release tag, or media changed.
- Verifier: root coordinator

## EvidenceBundleDraft

- Artifact key: task-8a-mqtt-ha-inventory-local-verification
- Type: local focused, related-surface, observer, and static verification
- Source: the current optional MQTT/Home Assistant inventory working tree on
  the approved local Windows workstation; GitHub-hosted runners, a live MQTT
  broker, production Home Assistant, Frigate, GHCR, and real media were not used
- Summary: The final focused MQTT and packaging surface passed 151 tests. The
  complete local suite passed 1,696 tests with 100 expected skips and one known
  Starlette deprecation warning. The soak-observer surface passed 46 tests with
  two expected Windows-only POSIX skips. Python compilation, bounded Ruff, and
  whitespace/diff checks passed. An independent hash-bound source review also
  passed its arrival, move, deletion, cutoff, failed-scan, cross-library,
  rollback, and deduplication gates with no deterministic P0-P2 finding. These
  results cover the public-default-off configuration,
  full pre-decode inventory barrier, aggregate sensor calculations, privacy
  boundary, 60-second refresh, reconnect/failure handling, and observer parsing
  only at the local source boundary. They do not prove the exact candidate on
  the simulator, Home Assistant discovery in HAOS-DEV or production, Frigate
  coexistence, a continuous 72-hour soak, deployment, or publication.
- Verifier: root coordinator
