# ADR 0002: Memory-aware segmented transcription and safe media classification

- Status: Proposed
- Date: 2026-09-01
- Decision owners: Subgen runtime, packaging, and operations maintainers
- Related decision: ADR 0001 remains authoritative for the generation-bound
  marker registry

## Source evidence

This proposal is derived from the approved memory-aware segmentation design,
its v0.5.0 execution plan, the implemented Tasks 2 through 7 owners, and Task
8's focused package/module checks. Complete Linux/simulator, constrained real-
inference, exact-image, and Frigate evidence is intentionally absent until
Tasks 9 through 11 finish.

## Context

Whole-file audio and inference allocations can grow with media duration, while
a fixed model may already consume substantial RAM or VRAM. On a shared CUDA
host, total VRAM and one idle free-memory reading do not prove that Subgen can
load safely without displacing higher-priority work. Generic parser, inference,
resource, or native-crash failures also do not prove that media is corrupt and
must not authorize deletion.

The existing architecture already has one queue, one inference gate, one
transcription/output owner, an exact-generation marker registry, and a fail-
closed exact-file unlink owner. The new design must preserve those owners and
the public API/output contract.

## Proposed decision

For local files, plan one capacity-derived half-open core window at a time with
five seconds of available context on each side. Process windows sequentially,
assign structured words or wordless segments by source-time midpoint, and
clip their copied timestamps to the core that owns them before publishing one
final subtitle atomically only after every window succeeds. Do not
semantically deduplicate matching seam text: independent decodes cannot prove
whether it is repeated output or legitimate repeated speech, so preserving
transcript content takes precedence.
Pressure yield discards the uncommitted window, releases the fixed selected
model at the canonical safe boundary, waits, and retries the same cursor with a
smaller duration down to five minutes. Three healthy windows may grow toward
the original baseline. Uploaded byte-buffer APIs remain unsegmented.

Choose an automatic model once by enumerating `large-v3`, `medium`, `small`,
`base`, then `tiny`. A strict immutable `ModelEnvelope` tied to the OCI config
digest, ordered layer diff IDs, exact runtime/model/compute/device/decoder/
concurrency/chunk policy, and repeated incremental peaks is authoritative.
Generic system-memory and allocatable-VRAM tables are fallback ceilings only.
Fresh host/cgroup admission and stabilized exact-device free VRAM, after
separate reserves, must cover the selected envelope or fallback requirement
immediately before load/reload. Explicit recognized models remain fixed and are
never silently downgraded within a file.

Keep the identity and catalog outside the image at owner-only host paths and
mount them read-only into the runtime. Only the isolated owner-operated profiler
may write a staged catalog. Missing, unsafe, invalid, or non-matching evidence
uses conservative public fallback; canonical shared CUDA fails closed and
requires a positive audited GPU reserve.

Before inference, combine bounded typed FFprobe and isolated PyAV evidence.
Only two conclusive `invalid_format` results for the unchanged current
generation produce `invalid_media`. The monitor remains the sole automatic
deletion decision owner and must durably write/re-read the marker before exact
unlink. All silent, indeterminate, timeout, permission, inference, resource,
OOM, pressure, native-crash, generic/log-regex, legacy-intent, and stale-
replacement cases remain. Repair is report/evidence-only, including when its
legacy `delete` input is requested.

## Alternatives rejected

- Raising the memory limit: does not bound duration-driven allocations or
  protect a shared host from already-resident weights.
- One universal fixed window: wastes capable hosts and cannot adapt after
  pressure changes.
- Mid-file model downgrade: changes quality within one subtitle and makes
  retries nondeterministic.
- Total VRAM or static GPU tables as authority: ignores current higher-priority
  use and exact backend/runtime cost.
- String/log-based media deletion: cannot distinguish corrupt media from
  transient validation, inference, resource, or native failures.
- Repair-side deletion: creates a second destructive decision owner and can
  revive stale/untyped intents.

## Compatibility and security boundary

- When accepted, this decision amends ADR 0001's broad repair/deletion
  compatibility language: the schema-v1 marker compatibility remains, but
  repair deletion and generic/crash deletion do not.
- Preserve routes, response fields, upload processing, queue identity,
  subtitle naming, language/task behavior, webhooks, marker schema v1,
  directory `.subgen_skip`, and descriptor-relative exact unlink ownership.
- `SEGMENTATION_ENABLED=False` disables local-file segmentation only; model
  admission, validation, markers, pressure release/wait, and deletion safety
  remain active.
- `AUTO_DELETE_FAILED_FILES` remains a deprecated compatibility alias through
  0.5.x, narrowed to invalid-media-only deletion. `SUBGEN_REPAIR_ACTION=delete`
  remains accepted but report-only. Neither retains broad destructive behavior.
- No Sonarr/Radarr API integration, Ollama lifecycle coordination, parallel
  chunk inference, public mutation API, or marker schema v2 is introduced.
- Public rollback is v0.4.1 with deletion disabled. The operator-specific
  Frigate rollback is its preserved v0.3.0 config/cache/OCI identity and is not
  interchangeable with the public path. Plex-hosted Subgen remains retired.

## Consequences

- Input duration no longer requires a correspondingly large single inference
  input, but model weights still must pass admission independently.
- Shared-CUDA telemetry loss may pause Subgen indefinitely; preserving higher-
  priority work is preferred to speculative admission.
- Source and packaged deployments carry the profiler at the same fixed path,
  but ordinary scanner/worker code never invokes it.
- Operators gain explicit provenance and conservative deletion behavior at the
  cost of managing two owner-only external evidence files when exact envelope
  selection is used.

## Evidence required before acceptance

This ADR remains proposed during Task 8. Acceptance requires the complete
Task 11 evidence: focused and full local tests, Linux/simulator package parity,
real constrained 4/6/9 GiB inference and pressure smokes, exact-image
ModelEnvelope and OCI-identity continuity, conservative deletion proofs, and
the isolated Frigate candidate gate with a positive audited GPU reserve under
the final 10 GiB hard/no-swap runtime boundary. A 12 GiB profiling result alone
cannot satisfy acceptance.

## Baseline sync

The 2026-08-30 initial baseline remains a historical pre-v0.4 snapshot and
must not be rewritten as v0.5 truth. Task 11C owns the verified current-state
baseline/work evidence and this ADR's acceptance after every gate above passes.
Until then the index labels both the baseline and this decision accurately as
historical or proposed.

## Evidence references

- [Approved design](../specs/2026-08-31-memory-aware-segmented-transcription-design.md)
- [Execution plan](../plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md)
- [Active work record](../work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/10-intent.md)

## Retirement

Retire generic/crash monitor deletion and all repair deletion in v0.5.0. Keep
the compatibility inputs only through their documented window and remove them
no earlier than 1.0.0. Retire segmentation or the external envelope contract
only through a later accepted decision that preserves atomic output, fixed-
model determinism, shared-host admission, and invalid-media-only deletion.
