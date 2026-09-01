# ADR 0002: Memory-aware segmented transcription and safe media classification

- Status: Proposed
- Date: 2026-09-01
- Decision owners: Subgen runtime, packaging, and operations maintainers
- Related decision: ADR 0001 remains authoritative for the generation-bound
  marker registry

## Source evidence

This proposal is derived from the approved memory-aware segmentation design,
its v0.5.0 execution plan, the implemented Tasks 2 through 8 owners, and the
sealed candidate-absent Frigate control that disproved free VRAM as shared-GPU
compute authority. Complete evidence for the amended priority signal, rebuilt
exact image, fresh envelopes, and Frigate gate is intentionally absent until
Tasks 11A through 11C finish.

## Context

Whole-file audio and inference allocations can grow with media duration, while
a fixed model may already consume substantial RAM or VRAM. On a shared CUDA
host, total VRAM and one idle free-memory reading do not prove that Subgen can
load safely without displacing higher-priority work. Generic parser, inference,
resource, or native-crash failures also do not prove that media is corrupt and
must not authorize deletion.

The Frigate control added a stronger constraint: camera process/skip pressure
can persist while roughly 17.5 GiB of VRAM is free. Global GPU utilization is
also not attribution because Subgen's own inference raises it. Shared compute
priority must be decided by the higher-priority workload, not inferred from a
memory counter inside Subgen.

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

For a shared accelerator, optionally consume one owner-only host priority file.
An unset path preserves public memory-only behavior; setting an absolute path
makes the signal required and stale-fail-closed. A separate Frigate host service
owns Frigate/Ollama evaluation and atomically writes only a coarse boot-bound
monotonic `clear|neutral|asserted` observation. The resource probe consumes the
typed, validated observation,
and the existing pressure controller remains the sole admission/yield/recovery
owner. Fresh assertion or unavailable required telemetry closes admission,
unloads an idle model, or unwinds an uncommitted chunk at the next callback.
Recovery requires three consecutive, strictly increasing clear source
generations plus every existing resource/model check; repeated heartbeat polls
never advance it, and any intervening pressure/unavailable/invalid observation
or producer epoch change resets it. Neutral input cannot count clear or trigger
a new yield from normal, and it keeps admission closed while recovering.
This path never changes the selected model, consumes a media failure, creates a
marker, authorizes deletion, calls Frigate/Ollama, or publishes partial output.

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
- Global GPU utilization as pressure authority: cannot distinguish Subgen's
  own inference from higher-priority demand and would self-trigger/oscillate.
- Fixed idle scheduling: cannot react to unpredictable camera activity after a
  chunk begins; pausing/stopping the container does not provide same-cursor
  release/retry semantics.
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
- `PRIORITY_PRESSURE_FILE` is empty publicly. A configured path is a required
  owner-only signal with no companion fail-open boolean. Status exposes only
  `disabled|clear|neutral|asserted|unavailable` state, bounded-age and causal
  generation/digest metadata, never its
  path, reasons, observation ID, or raw input. Recovery counts only distinct
  increasing producer source generations, not repeated heartbeat polls.
- Public rollback is v0.4.1 with deletion disabled. The operator-specific
  Frigate rollback is its preserved v0.3.0 config/cache/OCI identity and is not
  interchangeable with the public path. Plex-hosted Subgen remains retired.

## Consequences

- Input duration no longer requires a correspondingly large single inference
  input, but model weights still must pass admission independently.
- Shared-CUDA telemetry loss may pause Subgen indefinitely; preserving higher-
  priority work is preferred to speculative admission.
- A configured priority producer that dies or becomes stale also pauses Subgen
  indefinitely. The callback cannot preempt an already-running CUDA kernel, so
  simulator and Frigate gates must prove bounded safe-boundary reaction.
- Ashby's private Frigate producer uses an evidence-bound detection-load policy;
  its 80-FPS predictor is not a public or universal Frigate threshold. Version,
  topology, camera-map, configuration, or policy drift fails closed.
- Source and packaged deployments carry the profiler at the same fixed path,
  but ordinary scanner/worker code never invokes it.
- Operators gain explicit provenance and conservative deletion behavior at the
  cost of managing owner-only identity, model-envelope, priority-policy, signal,
  and unloaded-GPU evidence artifacts where those integrations are enabled.

## Evidence required before acceptance

This ADR remains proposed until Task 11C acceptance. Acceptance requires the complete
Task 11 evidence: focused and full local tests, Linux/simulator package parity,
real constrained 4/6/9 GiB inference and pressure smokes, exact-image
ModelEnvelope and OCI-identity continuity, conservative deletion proofs, and
the isolated Frigate candidate gate with a positive audited GPU reserve, a
fresh required priority signal, one causally bound real busy/degraded assertion-
to-unload-to-reload proof, and then a separate uninterrupted 900-second clear-
signal shared-health pass under the final 10 GiB hard/no-swap runtime boundary.
An unavailable/fail-closed episode cannot satisfy the cooperative-yield proof,
and asserted time cannot count toward the 900 seconds. All pre-amendment image/
envelope/sampler/gate evidence is diagnostic history only. A 12 GiB profiling
result alone cannot satisfy acceptance.

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
