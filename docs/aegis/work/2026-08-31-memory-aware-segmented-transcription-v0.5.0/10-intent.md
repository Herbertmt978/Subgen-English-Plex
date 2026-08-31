# Memory-Aware Segmented Transcription v0.5.0 - Intent

## TaskIntentDraft

- Requested outcome: Release and deploy memory-aware segmented transcription with highest-safe automatic model selection and invalid-media-only optional deletion
- Goal: Complete v0.5.0 locally and on the simulator, prove the exact CUDA candidate safely on Frigate before publication, publish an immutable image, and deploy it to Frigate while Plex-hosted Subgen remains retired
- Success evidence:
- Focused and full local tests, constrained simulator inference and pressure tests, exact-backend model envelopes, invalid-only deletion proof, isolated Frigate candidate evidence before publication, release digest, and healthy Frigate rollout evidence
- Stop condition: Done when every plan task and review gate passes; otherwise stop as blocked, needs-verification, or scope-exceeded with recoverable state
- Non-goals:
- Sonarr/Radarr API integration
- Ollama lifecycle coordination
- Uploaded API segmentation
- Scope: Resource policy, model runtime, local-file segmentation, media validation, monitor/repair deletion migration, Plex Subgen retirement, packaging, release, and Frigate rollout
- Change kinds:
- feature
- Risk hints:
- Memory concurrency, shared-GPU telemetry and priority, native inference failure, destructive deletion classification, release publication, and production rollout

## BaselineReadSetHint

- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md
- Acknowledged before plan:
- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md
- Cited in plan:
- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md
- Missing refs:
- none
- Advisory decision: continue

## ImpactStatementDraft

- Compatibility boundary: HTTP/upload/output/queue/marker schema compatibility; explicit models win; no GitHub-hosted runners or destructive real-media tests
- Affected layers:
- runtime
- operations
- distribution
- Owners:
- Root coordinator; canonical owners per approved plan
- Invariants:
- Only a fresh unchanged dual-validator invalid_media event may reach marker-before-delete; all other failures retain
- Automatic CUDA selection chooses the highest exact measured model that fits both RAM and VRAM admission envelopes while preserving the priority reserve
- Non-goals:
- Sonarr/Radarr API integration
- Ollama lifecycle coordination
- Frigate camera, detector, and embedding configuration
- Uploaded API segmentation

These records are Method Pack drafts / hints, not authoritative runtime decisions.

## BaselineUsageDraft

- Required baseline refs:
- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md
- Delivered context refs:
- none
- Acknowledged before plan:
- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md
- Cited in plan:
- docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md
- docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
- docs/aegis/baseline/2026-08-30-initial-baseline.md
- CONTRIBUTING.md
- Missing refs:
- none
- Advisory decision: continue
