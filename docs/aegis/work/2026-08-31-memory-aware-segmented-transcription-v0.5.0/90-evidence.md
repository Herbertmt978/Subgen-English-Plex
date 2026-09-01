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
- Summary: Approved the exact-image catalog/identity bootstrap, Task 2A/2B/2C ordering, feasible fallback budgets, unconditional profiler packaging, margin-consistent admission, 12 GiB profiling-only to 10 GiB production requalification, shared-GPU fail-closed behavior, candidate identity chain, unit isolation, and rollback boundaries
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
