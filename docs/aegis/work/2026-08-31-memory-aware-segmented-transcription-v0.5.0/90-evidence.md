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
