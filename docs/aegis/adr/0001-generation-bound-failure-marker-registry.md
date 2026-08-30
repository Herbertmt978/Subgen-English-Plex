# ADR 0001: Generation-bound failure marker registry

- Status: Accepted
- Date: 2026-08-30
- Decision owners: Subgen runtime and operations maintainers

## Context

A failed media file can be rediscovered by a full-folder scan. Deletion alone does not stop churn when cleanup is blocked, and a path-only exclusion would also suppress a repaired Sonarr/Radarr replacement. The monitor already owns exact failure attribution and file-generation fingerprints, while `subgen_core.media.gen_subtitles_queue` owns the first media probe.

## Decision

Use one atomically replaced, schema-versioned JSON registry in `SUBGEN_STATE_DIR`. Each entry contains an exact case-preserving container path beneath `/media`, a five-field device/inode/size/mtime/ctime identity, failure kind/count, and creation/update timestamps.

The monitor is the only producer and audit owner. It persists the marker before optional deletion. The canonical media enqueue boundary is the only consumer enforcement owner and checks the registry before AV/FFmpeg probing. Exact path plus exact identity skips; an identity mismatch is stale and proceeds. Invalid registry input fails open for transcription and cannot authorize deletion.

Public marker/skip defaults are enabled at one qualifying failure. Public deletion remains disabled. If marker creation and deletion are both enabled, a delete threshold greater than the marker threshold is rejected as unreachable.

## Alternatives rejected

- Directory `.subgen_skip`: blocks healthy siblings and replacements and has a separate operator-controlled purpose.
- Path-only marker: cannot distinguish a replacement at the same path.
- Exposing private monitor recovery state: leaks host-only details and couples the runtime to destructive-operation internals.
- Deletion without a marker: resumes churn whenever deletion is blocked or interrupted.
- A remote marker mutation API: adds an unnecessary trust boundary and arbitrary skip authority.

## Consequences

- Source and packaged deployments must carry the shared schema module and mount the same state directory read-only into Subgen.
- The host monitor and container must share numeric-UID read access to owner-only registry state.
- Marker reads are bounded, cached by metadata, strict about schema/symlink/regular-file safety, and rate-limit unchanged warnings.
- A replacement self-unblocks without an explicit marker cleanup transaction.
- Previous runtimes ignore the registry, so rollback can preserve it as evidence.

## Security and compatibility boundary

The registry contains no host paths, credentials, tokens, or subtitle content. It is non-destructive input. Only `subgen_ops_safety.py` can perform exact-file deletion, and a marker-write failure blocks marker-dependent deletion. APIs, subtitle naming, model choice, concurrency, directory skip markers, and repair state remain compatible.

## Retirement

Retire the registry only after marker creation is disabled, every runtime consumer is removed, and audit/rollback retention is explicitly resolved. Do not repurpose it for arbitrary path suppression or deletion authorization.
