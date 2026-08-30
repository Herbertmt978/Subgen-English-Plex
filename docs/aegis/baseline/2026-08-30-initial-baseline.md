# Subgen English for Plex Initial Baseline

Date: `2026-08-30`
Status: `initial dual-baseline snapshot`

## 1. Purpose

This snapshot records the repository state used to design generation-bound failure markers without weakening exact-file deletion safety or blocking replacement media.

## 2. Workspace Structure

- `subgen_override.py` is the FastAPI composition root and compatibility facade.
- `subgen_core/` owns queueing, scanning, media, transcription, and runtime algorithms.
- `monitor_subgen_failures.py` owns live failure attribution and threshold handling.
- `subgen_ops_safety.py` owns fail-closed exact-file validation and deletion.
- `tests/` provides mocked CPU-only regression coverage.

## 3. Current Authority Surfaces

- `README.md`, `docs/CONFIGURATION.md`, and `docs/MIGRATION.md` define operator behaviour.
- `CONTRIBUTING.md` requires an issue before deletion or API behaviour changes and specifies the local verification suite.
- Tests are the executable compatibility boundary.

## 4. Product / Requirement Baseline

### 4.1 Current Truth

- Directory-wide exclusion exists through a `.subgen_skip` ancestor marker.
- Exact-file generation exclusion does not exist.
- Automatic deletion is disabled publicly and requires three matching failures when enabled.
- The Plex deployment intentionally deletes after one matching failure so Sonarr/Radarr can replace the missing file.
- The current public release is `v0.3.0`; the approved marker feature requires a new `v0.4.0` package and GHCR image.

### 4.2 Non-negotiables

1. A marker for an old file generation must never suppress a replacement at the same path.
2. Public deletion remains opt-in.
3. Only exact regular files beneath the configured media root are deletion candidates.
4. Existing directory `.subgen_skip` behaviour remains compatible.

### 4.3 Product Non-goals

- Triggering or verifying Sonarr/Radarr searches.
- Replacing the existing deletion/quarantine algorithm.
- Coordinating Ollama or changing VM memory policy.

## 5. Architecture / Runtime Boundary Baseline

### 5.1 Current Truth

- Every enqueue path converges on `gen_subtitles_queue` before media probing and queue insertion.
- Failure counts and file identities are owned by the monitor.
- Secure deletion is owned by `subgen_ops_safety.py`.
- The monitor state directory is the existing persistent host-owned boundary.

### 5.2 Architecture Non-negotiables

1. Exclusion is checked before `has_audio` or another parser opens a known-bad file.
2. Marker persistence is atomic and versioned.
3. Subgen consumes only a narrow marker contract, not the monitor's private internal state.
4. A corrupt or unavailable marker registry cannot delete media.

### 5.3 Architecture Non-goals

- A second queue implementation.
- A path-only blacklist.
- A mutable API endpoint that allows arbitrary remote exclusions.

## 6. Ownership / Contract Snapshot

- Marker schema and identity matching: new small shared marker module.
- Marker production and audit events: `monitor_subgen_failures.py`.
- Pre-probe enforcement: the canonical enqueue facade using the shared marker reader.
- Deletion: unchanged `subgen_ops_safety.py` owner.

## 7. Current State and Risks

- Baseline verification: `315 passed, 56 skipped` with unrelated global pytest plugins disabled and `requests-mock` explicitly enabled.
- Primary risk: a stale path-only marker blocking a healthy replacement.
- Secondary risks: partially written state, permissions mismatch, marker-file tampering, or feature drift between source and packaged Compose profiles.

## 8. Alignment Use

Use this baseline when reviewing marker identity, monitor/deletion sequencing, packaged mounts, or replacement-file behaviour.

## 9. Compatibility Boundary

Existing status responses, queue identity, subtitle naming, directory markers, webhook routes, and public deletion defaults must remain compatible.
