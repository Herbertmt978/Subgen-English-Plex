# Memory-Aware Segmented Transcription v0.5.0 - Intent

## TaskIntentDraft

- Requested outcome: Release and deploy memory-aware segmented transcription with highest-safe automatic model selection and invalid-media-only optional deletion
- Goal: Complete v0.5.0 locally and on the simulator, publish an immutable image, and deploy it safely to Plex
- Success evidence:
- Focused and full local tests, constrained simulator inference and pressure tests, invalid-only deletion proof, release digest, and healthy Plex scan/transcription evidence
- Stop condition: Done when every plan task and review gate passes; otherwise stop as blocked, needs-verification, or scope-exceeded with recoverable state
- Non-goals:
- Sonarr/Radarr API integration
- Ollama lifecycle coordination
- Uploaded API segmentation
- Scope: Resource policy, model runtime, local-file segmentation, media validation, monitor/repair deletion migration, packaging, release, and Plex rollout
- Change kinds:
- feature
- Risk hints:
- Memory concurrency, native inference failure, destructive deletion classification, release publication, and production rollout

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
- Non-goals:
- Sonarr/Radarr API integration
- Ollama lifecycle coordination
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
