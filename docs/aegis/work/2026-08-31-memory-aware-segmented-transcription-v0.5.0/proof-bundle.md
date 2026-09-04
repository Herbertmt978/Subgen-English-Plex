# Proof Bundle - 2026-08-31-memory-aware-segmented-transcription-v0.5.0

## Method Pack Boundary

This proof bundle is an advisory Aegis Method Pack record. It does not determine evidence sufficiency, produce authoritative `GateDecision`, or grant `completion authority`.

## Task Intent

- Requested outcome: Release and deploy memory-aware segmented transcription with highest-safe automatic model selection and invalid-media-only optional deletion
- Scope: Resource policy, model runtime, local-file segmentation, media validation, monitor/repair deletion migration, Plex Subgen retirement, packaging, release, and Frigate rollout

## Impact

- Compatibility boundary: HTTP/upload/output/queue/marker schema compatibility; explicit models win; no GitHub-hosted runners or destructive real-media tests
- Non-goals:
- Sonarr/Radarr API integration
- Ollama lifecycle coordination
- Frigate camera, detector, and embedding configuration
- Uploaded API segmentation

## Evidence Bundle Refs

- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-github-issue-7.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-plan-review.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-task-10-cpu4-constrained-inference.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-task-10-cpu6-pressure-attempts-9-through-11.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-task-10-post-amendment-runtime-and-image-freeze.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-task-10-pressure-helper-v7-local-verification.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-task-12-profiler-source-proof-chain.json
- docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/evidence-bundle-draft-workspace-preflight.json

## Drift Check

- Scope status: The active scope remains the frozen v0.5.0 candidate and approved simulator-only verification. No GitHub runner, public ref/release, registry release tag, live Frigate service, Plex service, or real media changed.
- Compatibility status: Public behavior remains highest-quality safe model selection when unset, adaptive 5-30 minute segmentation with bounded shrink/retry/regrowth, first-failure fingerprint markers by default, optional deletion only after both FFprobe and PyAV conclusively reject media, and dynamic yielding without changing the selected model.
- Retirement status: Plex Subgen remains retired. The live Frigate v0.3 deployment and rollback remain running/preserved as previously recorded and were not modified by this resumed local gate slice.
- Advisory decision: continue
