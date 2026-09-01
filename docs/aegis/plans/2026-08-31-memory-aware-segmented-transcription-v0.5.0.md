# Memory-Aware Segmented Transcription v0.5.0 Implementation Plan

Status: `approved for execution with Frigate/GPU priority amendment 2026-09-01`
Date: `2026-08-31`

## Goal

Release Subgen English for Plex `v0.5.0` with bounded sequential audio
segmentation, deterministic highest-safe automatic Whisper model selection,
cooperative memory-pressure yielding, host-owned shared-GPU priority yielding,
and conservative media validation.
Public installs mark and skip the exact failed generation after the first
qualifying terminal failure but never delete by default. The Frigate-hosted
deployment uses its shared RTX 3090 and deletes on the first failure only when
FFprobe and isolated PyAV independently classify the same unchanged generation
as invalid media; all silent, indeterminate, inference, resource, and
native-crash failures are retained. The superseded Plex-hosted Subgen runtime
is removed without touching Plex or library media.

## Architecture

`subgen_core/resource_management.py` becomes the pure owner of capacity,
model/chunk policy, admission math, pressure interpretation, hysteresis, and
adaptive retry state. `subgen_core/resource_probes.py` is a leaf module for
bounded host, cgroup, PSI, and GPU readers/parsers only; it contains no policy
or admission arithmetic. `subgen_core/priority_pressure.py` owns the bounded,
owner-only, wrong-boot/stale-fail-closed priority-signal file contract.
`monitor_frigate_priority.py` is the only Frigate/Ollama-specific evaluator and
atomically publishes only that coarse signal; core runtime modules never call
or configure either higher-priority service. `subgen_core/model_envelope_catalog.py` owns catalog
and runtime
identity schemas, mode validation, canonical catalog serialization, integrity,
strict matching, and artifact writing. `profile_model_envelopes.py` is the
separate owner-operated measurement entry point; it consumes the catalog and
resource-policy owners rather than duplicating artifact or admission logic, so
the already-large resource owner does not absorb artifact I/O.
`subgen_core/segmentation.py` becomes the pure owner of chunk windows,
midpoint ownership, structured timestamp merge, and the segmented inference
coordinator. `subgen_core/model_runtime.py` remains the sole model/gate/cache
owner and gains a single-flight release coordinator/admission barrier, an
all-permit pressure release, and an in-gate reload check.
`subgen_core/transcription.py` remains the whole-job and output owner.
`subgen_core/media.py` remains admission and media classification owner, using
bounded FFprobe plus isolated PyAV. `monitor_subgen_failures.py` is the only
automatic deletion decision owner. `repair_subgen_failures.py` becomes
report-only for crash candidates. Existing marker schema v1 and exact secure
unlink primitives remain unchanged.

## Tech Stack

- Python 3.10+, pytest, stable-ts/faster-whisper, PyAV, FFmpeg/FFprobe
- FastAPI compatibility facade and the existing `subgen_core` package
- Docker/Compose cgroup v2 on the workstation, simulator PC, Frigate host, and
  retired Plex VM deployment evidence
- systemd monitor/repair integration on Linux
- Git, GitHub CLI, GitHub Releases, and GHCR without GitHub-hosted runners

## Baseline / Authority Refs

- `docs/aegis/specs/2026-08-31-memory-aware-segmented-transcription-design.md`
- `docs/aegis/baseline/2026-08-30-initial-baseline.md`
- `docs/aegis/BASELINE-GOVERNANCE.md`
- `CONTRIBUTING.md`
- Existing executable contracts under `tests/`
- User approvals through 2026-09-01: implement segmentation even if a fork is
  necessary; choose the highest-quality safe model from RAM; yield memory to
  other workloads; mark/skip at the first failure; delete only media that both
  FFprobe and PyAV cannot parse; package v0.5.0; deploy with first-failure
  invalid-media deletion; perform all tests locally or on the idle simulator
  and never on GitHub-hosted runners; retire the Plex-hosted instance and use
  Frigate's RTX 3090 as the canonical deployment target; make Subgen the
  cooperative last-priority workload; continue autonomously without another
  permission prompt

## Compatibility Boundary

- Preserve every HTTP route, required response field, queue identity, subtitle
  naming, task/language behavior, webhook count, and existing `.subgen_skip`
  and generation-marker behavior.
- Preserve explicit `WHISPER_MODEL` choices. Only blank or `auto` selects a
  model; one file never changes model after admission.
- `SEGMENTATION_ENABLED=False` disables segmentation only. Model selection,
  validation, markers, deletion safety, and pressure preflight/release/wait
  remain active; a yielded whole-file inference retries whole-file with a
  bounded limitation warning. Uploaded `/asr` and OpenAI-compatible byte-buffer
  requests never enter local-file segmentation in either mode.
- Invalid chunk/reserve values fail startup with a clear configuration error.
- CUDA automatic selection enumerates `large-v3` downward against a matching
  immutable exact-runtime `ModelEnvelope`; generic RAM/VRAM tables are
  fallback-only. Stabilized exact-device free VRAM and a mandatory automatic or
  explicit GPU reserve replace total-VRAM assumptions. Live GPU headroom,
  telemetry freshness, and resident-idle state join host/cgroup/PSI pressure.
- `PRIORITY_PRESSURE_FILE` is empty and disabled publicly. A non-empty absolute
  path means the owner-only signal is required; malformed, unsafe, wrong-boot,
  stale, missing, or unreadable input closes admission and yields/unloads. The
  combination of a non-empty path and `MEMORY_PRESSURE_YIELD=False` is rejected
  before scanner, worker, profiler, or model startup. The Frigate deployment
  must configure the path with yielding enabled. Signal state never consumes a
  media failure, creates a marker, changes the chosen model, or authorizes
  deletion.
- Preserve marker schema version 1 and its two kinds, mapping richer failures
  to `processing_error` or `sigsegv` rather than changing the shared contract.
- Preserve `subgen_ops_safety.py` as the sole exact-generation unlink owner.
- `AUTO_DELETE_FAILED_FILES=true` remains accepted but is narrowed to
  invalid-media-only and warns once. It can no longer delete generic errors or
  crashes. Removal is no earlier than 1.0.0.
- `SUBGEN_REPAIR_ACTION=delete` remains accepted but acts as report-only and
  warns. Legacy untyped or crash delete intents remain policy-blocked.
- A missing, malformed, timed-out, permission-denied, disappearing, changed,
  or validator-crashed file is indeterminate and can never authorize deletion.
- No test deliberately deletes or corrupts a real Plex library file.
- No GitHub Action is triggered or used for tests, builds, or publication.
- Frigate camera, embedding, and Ollama configuration are outside this task;
  Subgen must yield to them and no deployment action starts while the separate
  Frigate audit task owns that host.

## TDD Route

- Mode: `off`
- Decision: `skipped`
- Strict authority: `not applicable`
- Test posture: `post-change regression`
- Reason: no explicit strict-TDD authority exists; each cohesive implementation
  slice receives focused regression tests before its commit.
- Verification: focused local tests per slice, complete local suite, complete
  idle-simulator Linux suite/build/inference/pressure/safety smokes, then
  isolated pre-publication Frigate candidate evidence, Frigate production
  observation, and continued Plex-host retirement evidence.

## Verification

Fresh evidence must cover model/chunk boundaries; cgroup/host/PSI/GPU pressure;
the owner-only priority-signal schema, security/freshness boundary, immediate
assertion, three-clear recovery, idle unload, and same-core retry;
yield, unload, same-core retry, recovery, and cancellation; structured overlap
merge and atomic output; the dual-validator truth table and generation race;
marker-before-delete; every retained failure class; repair migration; package
parity; HTTP health; 4, 6, and 9 GiB real CPU inference; exact-backend model
envelopes; trusted runtime identity input and catalog/runtime/policy matching;
stabilized shared-GPU total/free/reserve selection; load/reload and resident-idle
release; stale-telemetry fail-closed behavior; no OOM/restart; release
identity/digest; Frigate health/FPS; and Subgen startup-scan progress.
Publication cannot begin until local, simulator, and isolated Frigate candidate
gates pass.

## Aegis Visibility

Planning is required because this change introduces three canonical core
modules plus an owner-operated profiler, a cross-process typed failure
boundary, adaptive state, destructive-policy migration, package defaults, a
public release, and a production rollout.

## Plan Basis

The current runtime loads one model and transcribes an entire selected audio
stream, sometimes materializing that stream in memory. Progress callbacks do
not sample pressure. PyAV admission reduces failures to a boolean. Generic
worker/file events cause the monitor to mark and optionally delete, while the
repair utility can independently delete crash candidates. Existing bounded
FFmpeg segment extraction, model locks, inference semaphore, exact marker
identity, and descriptor-relative secure deletion are reusable foundations.

## BaselineUsageDraft

- Required: current v0.5 design amendment under review, initial baseline, governance, contribution
  rules, current tests, retired Plex evidence, and the read-only Frigate
  snapshot: 24 GiB VM RAM maximum, 20 GiB balloon floor, about 7.5 GiB
  post-boot `MemAvailable`, no loaded Ollama model, and about 18.1 GiB free RTX
  3090 VRAM. Earlier qwen readings are historical only. The passive audit did
  not bound higher-priority incremental demand or establish a deployment
  reserve.
- Delivered: all required references and the user-approved safety boundary.
- Missing: none that blocks planning.
- Decision: `continue`.

## Requirement Ready Check

- Goal: long files complete within bounded memory and yield to other services.
- Public scenario: automatic highest-safe model, segmentation, first-failure
  marker/skip, deletion off.
- Frigate scenario: shared RTX 3090, a 12 GiB hard/no-swap profiling-only
  bootstrap followed by independently qualified auto/production operation at
  the evidence-checked 10 GiB hard/no-swap cap, startup scan, first-failure
  deletion only for unchanged dual-invalid media, and no Frigate/Ollama change.
- Plex retirement scenario: no Subgen container/process/port, monitor disabled,
  Plex HTTP 200, and deployment/model/state retained for recovery.
- Release scenario: locally/simulator-verified candidate, isolated Frigate gate,
  v0.5.0 tag/release/GHCR image, then immutable production promotion. Public
  rollback is v0.4.1; Frigate operational rollback is its preserved v0.3.0
  config/cache/image digest. Every rollback disables deletion first.
- Open blocker questions: source implementation can proceed, but Frigate
  candidate/deployment remains blocked until future evidence bounds
  higher-priority incremental demand or demonstrates a conservative explicit
  reserve under the defined representative-traffic gate.
- Decision: `ready` for implementation; `blocked` for Frigate deployment.

## Ripple Signal Triage

The model decision flows through configuration, status, loading, logging,
Compose, docs, and tests. Chunking flows through media metadata, queue payloads,
transcription, callbacks, model release, output, and webhook/task completion.
Failure classification flows through admission, event schema, monitor state,
marker mapping, delete recovery, repair migration, examples, systemd text, and
release notes. Packaging/version/publication, Plex retirement evidence, and
Frigate deployment are therefore all in scope; Plex/Sonarr/Radarr APIs,
Frigate camera configuration, and Ollama orchestration are not.

## Change Necessity

- Input duration currently permits whole-stream memory growth; a higher cap
  alone cannot provide a bounded public solution.
- Container priority cannot dynamically reclaim already allocated model/audio
  memory; a cooperative callback/release/retry path is required.
- Free VRAM cannot represent higher-priority GPU compute demand, and global GPU
  utilization cannot distinguish that demand from Subgen's own inference; an
  owner-generated external priority signal is required on the shared host.
- A path or generic parser error cannot distinguish corrupt media from a
  transient validator or resource failure; independent typed validation is
  required before deletion.
- Minimum code boundary: four focused core modules, one Frigate-specific host
  producer, the isolated profiler, narrow owner integration, typed event/state migration, and
  config/package/docs/version/release surfaces.
- Decision: `code-change`.

## Existence Check

- Reuse `extract_audio_segment_to_memory`, the inference gate/model lock,
  `ProgressHandler`, generation marker reader, and exact unlink safety.
- Add `resource_management.py` because no current owner represents cgroup/PSI
  capacity or adaptive pressure state.
- Add `segmentation.py` because window/ownership/merge policy does not belong
  in the already large facade or transcription orchestrator.
- Add `priority_pressure.py` because secure signal parsing does not belong in
  resource policy or the application facade. Add `monitor_frigate_priority.py`
  because failure/deletion monitoring and release-gate sampling are the wrong
  production owners for Frigate/Ollama health evaluation.
- Add focused test files rather than extending oversized monitor/facade tests.
- Do not add another marker taxonomy, delete primitive, queue, worker, or
  persistence store.
- Decision: `add-with-proof`.

## Architecture Integrity Lens

- Capacity/model/chunk decisions have one pure owner.
- Model unload/reload/cache release and inference exclusion have one runtime
  owner. One single-flight release coordinator closes admission before any
  permits are drained, so concurrent yielders cannot split permits and
  deadlock. The tested order is callback unwind, current-permit release,
  release coordinator, admission barrier, all inference permits, then model
  lock.
- The host producer alone interprets Frigate/Ollama state, the priority-signal
  module alone validates its coarse file contract, and the existing pressure
  controller alone interprets that observation for admission/yield/recovery.
- Media admission has one classifier and marker check still precedes probing.
- A dedicated `media_validation_failed` plus exact `invalid_media` class and
  unchanged identity is the only route to deletion.
- Monitor writes the marker durably before delegating unlink; repair cannot
  initiate deletion.
- Segmented coordination returns one real structured result and performs no
  output, marker, webhook, or deletion side effects.
- Transcription writes one final output atomically and completes one task once.
- Falsifier: any generic error, SIGSEGV, OOM, log regex, old intent, or repair
  candidate can reach unlink; if found, return to the current design review.
- Verdict: `aligned`.

## Complexity Budget

- Strong pressure already exists in `monitor_subgen_failures.py` (1,713 lines)
  and `subgen_override.py` (1,312 lines); `repair_subgen_failures.py` (904) and
  several tests are also large.
- New algorithms belong in `subgen_core/resource_management.py`,
  `subgen_core/segmentation.py`, `tests/test_resource_management.py`,
  `tests/test_segmentation.py`, and `tests/test_media_validation.py`. Bounded
  platform readers and parsers belong in the policy-free
  `subgen_core/resource_probes.py` leaf.
- Facade, monitor, repair, model runtime, transcription, and media edits are
  restricted to owner logic or wiring. No opportunistic refactor is allowed.
- `resource_management.py` is now materially large; priority-file I/O and
  schema logic must stay in the new leaf module, leaving only a bounded typed
  classification/recovery branch in the policy owner.
- Result: `at-risk but governed`.

## Plan-Time Complexity Check

- Resource policy and segmentation are cohesive new boundaries. The platform
  readers/parsers form a bounded leaf seam in `resource_probes.py` because the
  first Task 2B implementation made the resource owner materially over-budget;
  model, reserve, admission, pressure, and adaptive arithmetic must not move
  into that leaf.
- Media classifier remains in `media.py`; its isolated child entry point may be
  a module CLI so no third algorithm module is created.
- Monitor private state gains a bounded failure class but shared markers remain
  schema-compatible.
- Oversized source tests receive only shared-fixture/signature corrections;
  primary behavior coverage is created in focused files.
- Recommendation: proceed with new canonical modules and wiring-only facade.
- Priority parsing is a new leaf; Frigate-specific evaluation is a standalone
  host producer. Do not add either responsibility to the over-budget resource
  policy, facade, or failure monitor.

## Files

### Create

- `subgen_core/resource_management.py`
- `subgen_core/resource_probes.py`
- `subgen_core/priority_pressure.py`
- `subgen_core/model_envelope_catalog.py`
- `profile_model_envelopes.py`
- `monitor_frigate_priority.py`
- `priority-monitor.env.example`
- `systemd/subgen-priority-monitor.service`
- `subgen_core/segmentation.py`
- `tests/test_resource_management.py`
- `tests/test_resource_probes.py`
- `tests/test_priority_pressure.py`
- `tests/test_monitor_frigate_priority.py`
- `tests/test_model_envelope_catalog.py`
- `tests/test_model_envelope_profiler.py`
- `tests/test_segmentation.py`
- `tests/test_media_validation.py`
- `docs/RELEASE_NOTES_0.5.0.md`
- `docs/aegis/adr/0002-memory-aware-segmented-transcription.md`
- `docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/10-intent.md`
- `docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/20-checkpoint.md`
- `docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/90-evidence.md`
- `docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/99-reflection.md`

### Modify

- `subgen_override.py`, `subgen_core/model_runtime.py`,
  `subgen_core/transcription.py`, `subgen_core/media.py`
- `monitor_subgen_failures.py`, `repair_subgen_failures.py`
- Existing tests and fixtures only where integration contracts require it
- `Dockerfile`, `.env.example`, `monitor.env.example`, all three Compose files,
  `systemd/subgen-repair.service`
- `README.md`, `docs/CONFIGURATION.md`, `docs/INSTALL.md`,
  `docs/MIGRATION.md`, `SECURITY.md`, `CONTRIBUTING.md`
- `CHANGELOG.md`, `VERSION`, `docs/aegis/INDEX.md`, baseline governance evidence

## Contracts to Implement

### ModelEnvelope catalog and runtime identity

- External owner-only paths are
  `/var/lib/subgen/model-envelopes/v1/catalog.json` and
  `/var/lib/subgen/model-envelopes/v1/image-identity.json`; the parent is mode
  0700 and both regular files are mode 0600 with no symlink/group/other access.
  Mount both read-only at `/opt/subgen/model-envelopes/catalog.json` and
  `/opt/subgen/model-envelopes/image-identity.json`; configure exactly
  `MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json` and
  `MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json`.
- Identity schema `subgen.model-envelope.identity/v1` contains only `schema`
  and `image_identity`, whose config digest and nonempty ordered layer diff IDs
  use canonical lowercase `sha256:` syntax. Reject duplicate/missing/unknown
  fields, non-ASCII values, invalid modes, symlinks, malformed digests, and
  empty or reordered layers. Host-side `docker image inspect` must compare both
  components with the artifact immediately before every profiler or `auto`
  container start; tags and manifest digests are insufficient.
- Canonical schema `subgen.model-envelope.catalog/v1` contains
  `catalog_version`, strict entries, and SHA-256 integrity. Entries bind the OCI
  config digest plus ordered layer diff IDs to exact runtime/model/compute/
  device/decoder/concurrency/chunk keys and positive repeated measurement,
  incremental-peak, and margin bytes.
- Canonical payload bytes use stdlib
  `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  allow_nan=False).encode("utf-8")` over `schema`, `catalog_version`, and
  `entries`. Loading rejects duplicate keys before decoding; NaN/infinity;
  non-ASCII, missing, or unknown fields; bool-as-int; invalid digest syntax;
  fewer than three runs; non-positive values; duplicate matches; bad modes;
  and integrity mismatch. No secret, hostname, device UUID, user/media name,
  private path, environment value, or raw diagnostic may be written.
- `subgen_core/model_envelope_catalog.py` owns both loaders, both schema/mode
  validators, strict identity/catalog/current-runtime/policy matching, canonical
  catalog serialization, SHA-256, and atomic writers. The profiler is the only
  catalog-writer caller. Ordinary runtime code receives validated immutable
  entries and cannot write either artifact.
- Missing/invalid/non-matching catalog or identity logs one bounded reason.
  Public auto uses generic fallback; canonical shared CUDA enters
  `recovering(reason=no_safe_model)` and admits nothing. Matching has no ranges,
  wildcards, tag identity, or nearest-entry behavior.
- Bootstrap uses the already-built candidate. Capture OCI config digest and
  ordered diff IDs before transfer; verify them after load. The isolated
  profiler starts explicit `large-v3` only after the canonical resource owner
  approves its generic incremental-peak-plus-margin admission, records at least
  three repeated peaks, writes a staged catalog without rebuilding, and
  restarts the exact image with both artifacts mounted read-only and auto to
  prove selection. A safe failure fully unloads and may repeat in a clean
  explicit process for `medium`, `small`, `base`, then `tiny`.
- Frigate profiling alone uses a 12 GiB hard/no-swap cgroup. It retains the
  same fresh host/cgroup/GPU admission, priority reserve, legacy-unit isolation,
  and immediate health aborts, and cannot authorize a production model. Destroy
  the profiler after each envelope write, verify release, and restart the exact
  image with auto under the final 10 GiB hard/no-swap cap. The measured
  incremental peaks plus margins and separate reserves must pass fresh 10 GiB
  admission; otherwise `large-v3` is unqualified and `medium`/lower is profiled
  as needed and selected by the same rule.

### Resource policy

- Immutable `CapacityProfile`, `ModelEnvelope`, `ModelDecision`, and
  `PressureSample` values.
- Pure discovery supports finite cgroup v2/v1 limits and physical fallback.
- Generic CPU RAM ceilings (`<2` tiny, `2–<4` base, `4–<8` small,
  `8–<16` medium, `>=16` large-v3) and CUDA allocatable-VRAM ceilings (`<2`
  tiny, `2–<3` base, `3–<7` small, `7–<12` medium, `>=12` large-v3) are
  fallback-only; unknown cannot promote beyond small.
- Auto enumerates `large-v3` downward and may select above a generic fallback
  only when the strict identity/catalog/runtime/policy match and current
  incremental-peak-plus-margin admission fit.
- Fallback incremental load budgets in GiB are: host/cgroup
  `tiny=0.75, base=1, small=2, medium=5, large-v3=9`; device
  `tiny=1, base=2, small=3, medium=7, large-v3=12`. Add 512 MiB host and 1 GiB
  device fallback margins; exact envelopes provide positive measured margins.
  The host/cgroup fallback value populates both incremental fields and is
  selected once by `max`, never summed.
- Boundary tests use fresh host capacity that is not limiting and prove that a
  4 GiB cgroup with 1 GiB current use and 512 MiB floor has 2.5 GiB admission
  for `small`'s 2 GiB increment plus 512 MiB margin, while a 9 GiB cgroup with
  2 GiB current use and 0.9 GiB floor has 6.1 GiB admission for `medium`'s
  5 GiB increment plus 512 MiB margin. Worse fresh baselines demote normally;
  less than `tiny`'s nonzero increment plus margin enters no-safe-model recovery.
- Initial chunks: `<4` GiB 5m, `4–<8` 10m, `8–<16` 20m, `>=16` 30m,
  unknown 10m; explicit values are 5–60 minutes.
- Automatic reserve is `min(max(1 GiB, 15% host), 25% capacity)` and finite
  cgroups retain `max(512 MiB, 10% limit)` headroom.
- Automatic GPU reserve is `max(1 GiB, 10% total VRAM)` and an explicit
  positive `GPU_MEMORY_RESERVE_GIB` may raise but never lower it. Canonical
  Frigate requires the released audit's explicit priority-demand-plus-reaction
  reserve and blocks if the setting remains `auto`.
- Per-run deltas are independently
  `max(0, peak_i-preload_i)` for host, cgroup, and device; each recorded
  incremental peak is the maximum paired delta over the runs. Envelope host
  load bytes are the maximum of the recorded host/cgroup incremental peaks and
  device load bytes are the recorded device incremental peak. Required bytes
  add the corresponding explicit margin.
  Immediate admission bytes are
  `MemAvailable-host_reserve`, finite
  `cgroup_limit-cgroup_current-cgroup_floor`, and
  `free_vram-gpu_priority_reserve`, all clamped to zero; effective host bytes
  use the minimum available host/cgroup term.
- Initial CUDA selection uses the minimum of three fresh exact-device samples
  five seconds apart. Immediately before every automatic load/reload, fresh
  host, cgroup, and exact-device reads must satisfy required
  incremental-peak-plus-margin bytes
  inside the gate. If no candidate including `tiny` fits, enter
  `recovering(reason=no_safe_model)` without loading, failing, or marking.
- Samples are throttled to five seconds; host/cgroup/PSI/GPU pressure requires
  two sustained samples, critical headroom/OOM yields immediately, and recovery
  requires three healthy admission-qualified samples. On canonical shared CUDA,
  one missing/stale sample closes admission, two unload at a safe boundary, and
  unknown never counts healthy. Waits use cancellation-aware 5–60 second
  backoff.
- A five-second resident-idle observer applies the same GPU rules and unloads
  cached weights even when no inference callback is active.
- Yield does not count as a media failure. Two healthy minimum-chunk allocation
  failures become `resource_exhaustion`; model-load failures are profile-level
  and never mark/delete a file.
- The model runtime has one single-flight release state. New admissions wait
  while state is `yielding` or `recovering`; concurrent release requests join
  the active release rather than draining separate permit subsets.
- Missing pre-load GPU telemetry warns and cannot promote beyond `small` in the
  public fallback. Canonical shared CUDA fails closed; insufficient or stale
  load capacity consumes no failure attempt. GPU headroom below half its
  resident floor is critical.

### Segmentation and output

- Plan one adaptive half-open core at a time with five seconds of clamped
  overlap. Do not precompute fixed windows.
- Extract only the selected stream interval as mono 16 kHz WAV and process
  sequentially through the existing gate.
- Callback combines progress and pressure; its private exception must escape
  the installed backend callback path.
- A yield discards the uncommitted chunk, releases model/caches after callback
  unwind, waits, halves to a five-minute floor, and retries the same cursor.
  Three healthy completed chunks grow one step toward the baseline.
- Shift structured segments/words to source time, assign by midpoint, and clip
  retained timestamps to their owning core before merge; final core owns the
  exact end. Preserve ambiguous matching seam text rather than risk deleting
  legitimate repeated speech. Failed/yielded chunks append nothing.
- Produce one real `WhisperResult`; append attribution once when non-empty;
  render one same-directory private temp, fsync, `os.replace`, fsync directory,
  and clean the temp on failure. Webhook and task result occur once.

### Media and deletion

- FFprobe and PyAV are bounded, independent, no-shell probes. PyAV runs in an
  isolated child; timeout/crash/malformed reply is indeterminate.
- Validator results are `audio_present`, `no_audio`, `invalid_format`, or
  `indeterminate`. Aggregation order is exact: `invalid_format +
  invalid_format` is `invalid_media`; otherwise any `audio_present` is
  `valid_audio`; otherwise any `no_audio` is `no_audio` (including
  `no_audio + invalid_format`); all remaining combinations are
  `probe_indeterminate`. Timeouts, crashes, permission/I/O failures, or an
  identity change are indeterminate validator evidence. PyAV attempts at most
  the bounded first audio frame before reporting audio.
- Capture exact identity before/between/after probes and include it in the
  bounded structured terminal event. Monitor must match it to current host
  identity before marker/delete.
- Extend `emit_subgen_event(..., *, failure_class=None, source_identity=None)`
  with an allowlist. Typed admission failures emit once and cannot fall through
  to a duplicate generic worker error.
- Monitor persists private `failure_class`; missing/unknown loads as generic
  retained. Only dedicated event + exact invalid class + unchanged identity +
  enabled marker + durable marker permits deletion.
- SIGSEGV, OOM, regex, ordinary worker/file errors, resource exhaustion,
  permission/timeout/indeterminate, and legacy delete intents always retain.
- Repair `delete` is report-only and old intents stay policy-blocked.

## Tasks

### Task 1: Open the public v0.5 traceability issue

**Files:** none.

**Steps:** search all issues, then create one privacy-safe issue covering
bounded segmentation, automatic highest-safe model, cooperative pressure,
first-failure marker/skip, and invalid-media-only opt-in deletion. Do not name
private hosts, paths, titles, credentials, or diagnostics.

```powershell
gh issue list --repo Herbertmt978/Subgen-English-Plex --state all --limit 100
$issueBody = @'
## Problem
Long media and competing services can exhaust a fixed Subgen memory budget,
while generic parser or inference failures cannot safely authorize deletion.

## Proposed behavior
- Select the highest-safe Whisper model when `WHISPER_MODEL=auto`.
- Transcribe long local media as bounded sequential overlapping chunks.
- Cooperatively release and retry a chunk under sustained memory pressure.
- Mark and skip the exact generation after the first terminal failure.
- Keep deletion off publicly and permit it only when independent FFprobe and
  isolated PyAV validation both conclusively classify unchanged media invalid.

## Acceptance
- 4 GiB, 6 GiB, and 9 GiB constrained simulator profiles complete without OOM.
- A Frigate-only 12 GiB profiling cgroup can record an envelope but cannot
  authorize selection by itself; the exact auto image must independently pass
  fresh measured-peak-plus-margin admission under the final 10 GiB hard/no-swap
  cap, otherwise a profiled lower model is selected.
- Pressure yield retries the same source interval and produces one atomic file.
- Silent, indeterminate, inference, resource, and native-crash failures remain.
- Replacement generations process normally.
- Local and simulator checks run without GitHub-hosted runners.
'@
$issueUrl = gh issue create --repo Herbertmt978/Subgen-English-Plex --title "Add memory-aware segmented transcription and safe media validation" --body $issueBody
$issueNumber = [int]($issueUrl -replace '.*/', '')
gh issue view --repo Herbertmt978/Subgen-English-Plex $issueNumber --json title,state,url,body
```

**Expected:** one open issue with the approved acceptance boundary. Delete the
temporary body after creation. No source commit.

### Task 1B: Retire the superseded Plex-hosted Subgen runtime

**Status:** completed 2026-08-31 after the user selected Frigate as canonical.

**Systems:** Plex VM Subgen container and monitor only.

**Action/evidence:** resolve the exact Compose owner, verify Plex is a separate
active service, record an owner-only recovery manifest outside the media tree,
disable `subgen-monitor.service`, gracefully stop and remove only the `subgen`
container, and verify no Subgen process or port 9000 listener remains. Preserve
Compose/config, the 4.4 GiB model cache, marker/state data, prior image, Plex,
and every media mount. Evidence: container absent, monitor inactive/disabled,
repair timer inactive, Plex HTTP 200, and recovery manifest under the private
Plex backup root. Do not recreate this instance during v0.5 rollout.

### Task 2A: Implement ModelEnvelope catalog and identity artifacts

**Files:** create `subgen_core/model_envelope_catalog.py` and
`tests/test_model_envelope_catalog.py`.

**Steps:** implement catalog-v1 and identity-v1 dataclasses,
duplicate-key-rejecting JSON loading, the exact stdlib catalog canonical
serializer, SHA-256 verification, strict identity-to-catalog/current-runtime/
policy matching, regular-file/owner-only mode and symlink checks, and atomic
mode-0600 writers. Keep writer APIs out of the ordinary runtime path. The
identity loader accepts only the exact schema, canonical config digest, and
nonempty ordered layer diff IDs; it performs no Docker/tag discovery.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q tests/test_model_envelope_catalog.py
python -m compileall -q subgen_core/model_envelope_catalog.py
git diff --check
```

**Expected:** canonical round trip; duplicate/NaN/unknown/bool/mode/integrity/
identity rejection; identity artifact-to-catalog and current runtime/policy
match; no-secret schemas; atomic owner-only writes; public fallback and
canonical shared-CUDA fail-closed behavior. Commit only these two files as
`Add model envelope catalog and identity artifacts`.

### Task 2B: Implement pure resource and adaptive policy

**Files:** create `subgen_core/resource_management.py`,
`subgen_core/resource_probes.py`, `tests/test_resource_management.py`, and
`tests/test_resource_probes.py`.

**Steps:** implement bounded injected platform/cgroup/GPU readers in the leaf
probe module; implement capacity/model/chunk/reserve functions,
`MemoryPressureYield`, allocation recognition, `PressureController`, and
`AdaptiveChunkState` in the canonical resource owner. Consume only validated
immutable catalog entries. Re-export the public sampling surface from the
resource owner without duplicating policy in the probe leaf.
Cover cgroup v2/v1/unbounded/physical fallback, every generic fallback boundary
and nonzero per-model incremental budget, exact-entry and fallback margins,
paired per-run peak-minus-preload aggregation, host/cgroup/device
incremental-peak-plus-margin formulas, exact 4 GiB/1 GiB-current `small` and
9 GiB/2 GiB-current `medium` feasibility, `large-v3`-down enumeration, `tiny`
rejection and no-safe-model recovery, explicit model, total/free VRAM,
automatic/explicit/audit reserve inputs, three-sample stabilization, immediate
fresh load/reload admission, unknown/stale telemetry, pressure hysteresis,
cancellation, shrink/grow, and false-positive OOM text.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTEST_PLUGINS='requests_mock.contrib._pytest_plugin'
python -m pytest -q tests/test_resource_management.py tests/test_resource_probes.py
git diff --check
```

**Expected:** focused tests pass. Commit only these four files as
`Add memory-aware resource policy`.

### Task 2C: Implement the isolated ModelEnvelope profiler

**Files:** create `profile_model_envelopes.py` and
`tests/test_model_envelope_profiler.py`.

**Steps:** implement the owner-operated CLI with injected measurement adapters.
Load/validate identity and catalog input only through Task 2A, request every
explicit-model pre-admission decision and formula from Task 2B, run three or
more repeated cold cycles, calculate paired incremental peaks through the
resource owner, and call the canonical staged catalog writer. Profile
`large-v3`, then clean-process `medium`, `small`, `base`, and `tiny` only after
safe failure. The profiler contains no admission arithmetic, image build path,
ordinary scanner/worker entry point, or direct canonical-catalog replacement.
Test that a successful measurement under a 12 GiB profiling capacity produces
evidence only: a separate fresh 10 GiB auto-admission decision can reject that
entry and select a separately profiled lower candidate.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest -q tests/test_model_envelope_catalog.py tests/test_resource_management.py tests/test_model_envelope_profiler.py
python -m compileall -q profile_model_envelopes.py
git diff --check
```

**Expected:** explicit `large-v3` bootstrap; exact identity consumption;
three-run paired incremental peaks; resource-owner admission calls; staged
owner-only output; clean lower-candidate fallback; no duplicated math or
rebuild; unchanged candidate identity; and no profiling-cap-to-production
authorization leak. Commit only these two files as
`Add isolated model envelope profiler`.

### Task 3: Integrate model selection and safe pressure release

**Files:** modify `subgen_core/model_runtime.py`, `subgen_override.py`,
`tests/test_custom_reliability.py`, and `tests/test_module_boundaries.py`.

**Steps:** parse settings once, including `MODEL_ENVELOPE_CATALOG` and
`MODEL_ENVELOPE_IDENTITY`, and reject invalid chunk/host-reserve/GPU-reserve
values at startup; load/validate/match both read-only artifacts through their
canonical owner against each other and the current runtime/policy; query bounded NVIDIA
total/free VRAM before model load and
in pressure samples for the exact configured CUDA index/UUID without summing
devices; log/expose total, stabilized free, reserve, allocatable, envelope key,
selected model, and decision provenance; preserve explicit model choices; add
cache cleanup and a single-flight release coordinator that closes inference
admission, owns every permit, and then takes `model_load_lock`; recheck/load the
model only after fresh in-gate host/device bytes cover the selected envelope's
incremental peaks plus explicit margins; compose the
pressure callback with progress; and add the five-second resident-idle observer.
Canonical shared CUDA closes admission on one missing/stale sample, unloads at
a safe boundary after two, and requires three fresh capacity-qualified recovery
samples. Insufficient capacity waits without consuming a load-failure attempt.
Concurrent release requests join the active transition; new work waits through
`yielding` and `recovering`.

```powershell
python -m pytest -q tests/test_model_envelope_catalog.py tests/test_resource_management.py tests/test_custom_reliability.py -k "catalog or identity or model or admission or pressure or release or reload"
python -m pytest -q tests/test_module_boundaries.py
python -m compileall -q subgen_override.py subgen_core/model_runtime.py subgen_core/model_envelope_catalog.py subgen_core/resource_management.py
git diff --check
```

**Expected:** explicit/auto decisions; exact identity/catalog/runtime/policy
match, envelope promotion, and public fallback; canonical missing/mismatch
fail-closed; stabilized shared-GPU selection; selection-to-load capacity drop;
stale/missing telemetry fail-closed; idle-resident unload;
insufficient-capacity reload wait; GPU pressure/recovery; invalid-setting
startup failures; all-permit lock order;
two simultaneous yields plus one queued worker; preflight admission blocking;
reload race; and callback composition pass.
Commit as `Integrate adaptive model runtime`.

### Task 4: Implement chunk planning, ownership, and structured assembly

**Files:** create `subgen_core/segmentation.py` and
`tests/test_segmentation.py`; modify `tests/conftest.py` only for faithful
stable-ts test doubles.

**Steps:** implement `ChunkWindow`, adaptive next-window planning, structured
copy/offset/midpoint ownership, wordless ownership, fresh IDs, monotonic
validation, aggregate language, progress callback, and real result construction
seam. Cover exact boundaries, final ownership, changing chunk sizes, empty
results, yield propagation, unchanged-cursor retry, and no append on failure.

```powershell
python -m pytest -q tests/test_segmentation.py
python -m compileall -q subgen_core/segmentation.py
git diff --check
```

**Expected:** all pure merge/coordinator tests pass. Commit as
`Add bounded transcription segmentation`.

### Task 5: Integrate segmented inference and atomic final output

**Files:** modify `subgen_core/transcription.py`, `subgen_override.py`,
`subgen_core/model_runtime.py`, `tests/test_custom_reliability.py`, and narrowly
affected integration/audio-track tests.

**Steps:** preserve the short whole-file path; route long inputs to adaptive
sequential extraction; bypass whole-track selected-audio materialization;
fallback from recognized whole-file pressure/allocation failure; keep model,
language, task, regroup, kwargs, gate, cancellation, and attribution stable;
release model/wait/retry same cursor; write segmented output atomically and
complete webhook/task exactly once.
When `SEGMENTATION_ENABLED=False`, retain validation/model/pressure behavior,
retry a yielded whole-file request as a whole file with a rate-limited
limitation warning, and never invoke a segment extractor. Local segmentation
must never be invoked by uploaded `/asr` or OpenAI-compatible byte-buffer APIs.

```powershell
python -m pytest -q tests/test_segmentation.py tests/test_custom_reliability.py -k "segment or pressure or atomic or output or audio_track"
python -m pytest -q tests/test_audio_tracks.py tests/test_integration.py tests/test_standalone_mode.py
python -m compileall -q subgen_override.py subgen_core
git diff --check
```

**Expected:** no whole-track extraction in segmented mode, no partial output,
one final result/webhook, explicit opt-out whole-file retry behavior, unchanged
upload APIs, and stable legacy behavior. Commit as
`Integrate adaptive segmented transcription`.

### Task 6: Add conservative dual-validator media admission

**Files:** modify `subgen_core/media.py` and `subgen_override.py`; create
`tests/test_media_validation.py`; narrowly update queue/event test fixtures.

**Steps:** add typed outcomes; bounded FFprobe JSON; isolated bounded PyAV child
entry point; generation snapshots across probes; conservative aggregation;
duration/track handoff; marker-before-classifier; exact one terminal typed event;
compatibility `has_audio` facade; no inference/queue for terminal outcomes.
Test the complete ordered truth table, including `no_audio + invalid_format`
as retained `no_audio`, and prove PyAV stops after its bounded first-frame
audio determination.

```powershell
python -m pytest -q tests/test_media_validation.py tests/test_failure_markers.py tests/test_custom_reliability.py -k "media or validation or marker or event"
python -m pytest -q tests/test_audio_tracks.py tests/test_integration.py tests/test_standalone_mode.py
python -m compileall -q subgen_override.py subgen_core/media.py
git diff --check
```

**Expected:** truth table, timeout/crash/permission/change, silent retention,
dual-invalid, and exactly-once event tests pass. Commit as
`Classify invalid media conservatively`.

### Task 7: Enforce invalid-media-only deletion and migrate repair

**Files:** modify `monitor_subgen_failures.py`, `repair_subgen_failures.py`,
`tests/test_monitor_subgen_failures.py`, `tests/test_repair_subgen_failures.py`,
`tests/test_failure_markers.py`, and `tests/test_custom_reliability.py`.

**Steps:** parse canonical and legacy deletion switches; add one warning;
persist validated private class/source identity; map markers without schema
change; gate every live and recovery unlink path on dedicated invalid event,
exact class, identity, and durable marker; block generic/SIGSEGV/resource/OOM
paths; make repair delete report-only; preserve old intents as policy-blocked.

```powershell
python -m pytest -q tests/test_monitor_subgen_failures.py tests/test_repair_subgen_failures.py tests/test_failure_markers.py tests/test_custom_reliability.py
python -m compileall -q monitor_subgen_failures.py repair_subgen_failures.py
git diff --check
```

**Expected:** legacy untyped intents, SIGSEGV, inference, OOM, timeout,
permission, indeterminate, and replacements cannot delete; dual-invalid marks
before exact unlink only when enabled. Commit as
`Restrict deletion to invalid media`.

### Task 8: Package, document, govern, and version v0.5.0

**Files:** modify `Dockerfile`, `tests/test_packaging.py`, examples, all Compose
profiles, systemd repair text, module tests, public docs, `CHANGELOG.md`,
`VERSION`, release notes, ADR, work record, and Aegis index/baseline evidence.

**Steps:** unconditionally add
`COPY profile_model_envelopes.py /subgen/profile_model_envelopes.py` to the
`Dockerfile` and make the packaging test assert that exact in-image path. Set
public model/segmentation/host-and-GPU-pressure defaults; add
`MODEL_ENVELOPE_CATALOG`, `MODEL_ENVELOPE_IDENTITY`, both exact external
host/container paths, both read-only mounts, both schema/mode/match failure
contracts, and the owner-only profiler procedure; set
marker/delete defaults; move the packaged image to v0.5.0 while retaining the
public 10 GiB memory default; document 4/6/9 GiB CPU fallback tiers,
fallback-only generic GPU tiers, immutable exact-runtime `ModelEnvelope`
provenance, `large-v3`-down enumeration, stabilized allocatable-VRAM selection,
shared-CUDA explicit reserve/fail-closed behavior, and conservative deletion.
Document stable runtime status version versus project version; record
repair/legacy migration, public v0.4.1 rollback, and deployed Frigate v0.3.0
config/cache/OCI-identity rollback as distinct paths; update local/simulator-only
policy. Document
`SEGMENTATION_ENABLED=False`, unchanged upload APIs, and startup rejection of
invalid chunk/host-reserve/GPU-reserve settings, the retired Plex instance,
and Frigate ownership boundary. Create ADR 0002 as proposed here and accept it
only in Task 11 after complete local/simulator evidence.

Write `docs/RELEASE_NOTES_0.5.0.md` as human-facing prose, not an automated
commit dump. Include a concise “Compared with earlier releases” section that
explains what v0.4.0 introduced, what v0.4.1 corrected, and what v0.5.0 changes
for ordinary users. Separate public defaults from the operator-specific
Frigate deployment, explain why each behavior matters, and include practical
upgrade, configuration, rollback, deletion-safety, and compatibility guidance.
Before publication, compare the complete GitHub release body byte-for-byte with
this reviewed file and block release creation on any generated or omitted
replacement text.

```powershell
python -m pytest -q tests/test_packaging.py tests/test_module_boundaries.py
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core profile_model_envelopes.py
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
docker build -t subgen-english-plex:v0.5.0-package-test .
docker run --rm --entrypoint /bin/sh subgen-english-plex:v0.5.0-package-test -c 'test -f /subgen/profile_model_envelopes.py'
docker run --rm --entrypoint python subgen-english-plex:v0.5.0-package-test -m py_compile /subgen/profile_model_envelopes.py
rg -n "0\.4\.1|v0\.4\.1|WHISPER_MODEL|MODEL_ENVELOPE_(CATALOG|IDENTITY)|GPU_MEMORY_RESERVE_GIB|AUTO_DELETE_FAILED_FILES|AUTO_DELETE_INVALID_MEDIA|SUBGEN_REPAIR_ACTION" README.md docs .env.example monitor.env.example docker-compose*.yml systemd tests VERSION CHANGELOG.md
git diff --check
```

**Expected:** package parity including the exact executable profiler path,
manual-only workflows, human-written release comparison, docs/version/identity
contracts, Compose, and compilation pass. Commit as
`Prepare Subgen English Plex 0.5.0`.

### Task 9: Run complete local verification

**Files:** no intended edits. Failures return to the owning task and focused
checks run before a corrective commit.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTEST_PLUGINS='requests_mock.contrib._pytest_plugin'
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

**Expected:** all checks pass and intended history/diff only.

### Task 10: Verify the exact candidate on the idle simulator

**Files/systems:** simulator temporary work area on a verified fixed local
drive, not a Git worktree/cloud/session directory.

**Steps:** check online state, sessions, pytest/build processes, containers,
and task markers. Wake only if offline and record ownership. Never compete with
another task. Transfer exact commit with checksum; run full Linux suite,
compile, Compose, build labeled candidate, source/package HTTP smokes, and
installed stable-ts callback propagation.

Before any transfer, capture immutable candidate identity as the OCI image
configuration digest plus ordered rootfs layer diff IDs. A tag, local image ID,
label, or registry manifest digest alone is insufficient. Docker Desktop's
containerd image store can report the OCI index as `.Id`, so derive the runtime
configuration digest from the saved archive's exact configuration bytes, bind
those bytes to the archive manifest, and cross-check the tag's ordered diff IDs.
Transfer the identity record, archive, and checksum together.

Generate only disposable synthetic media. Run constrained real CPU inference:
4 GiB -> small/10m; 6 GiB -> small/10m; 9 GiB -> medium/20m. Under a separately
capped pressure helper prove yield, unload, same-core smaller retry, recovery,
completion, zero restart/OOM, no partial output, and monotonic overlap merge.
Prove silent retain; dual-invalid classify; disagreement/timeout/permission
retain; isolated marker-before-delete and replacement; cross-bind identity.
When NVIDIA is available, measure repeated exact packaged-runtime cold-load,
first-inference, long-translation, unload/reload, fragmentation, host, and
device peaks for every automatic candidate; bind the resulting
`ModelEnvelope` to the exact candidate digest and runtime parameters. Exercise
stabilized selection, in-gate reload checks, telemetry loss, and idle unload.

```bash
set -euo pipefail
python -m pytest -q
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
RUNTIME_COMMIT="$(git rev-parse HEAD)"
candidate_tag="subgen-english-plex:v0.5.0-candidate-${RUNTIME_COMMIT:0:8}"
docker build --pull --label org.opencontainers.image.version=0.5.0 --label org.opencontainers.image.revision="$RUNTIME_COMMIT" -t "$candidate_tag" .
identity_root="$(mktemp -d /tmp/subgen-v050-identity.XXXXXX)"
identity_json="$identity_root/image-identity.json"
image_archive="$identity_root/subgen-v050-candidate.tar"
archive_manifest="$identity_root/docker-save-manifest.json"
image_config="$identity_root/image-config.json"
docker save --output "$image_archive" "$candidate_tag"
tar -xOf "$image_archive" manifest.json >"$archive_manifest"
config_member="$(python -c 'import json,re,sys; v=json.load(open(sys.argv[1],encoding="ascii")); assert len(v)==1; n=v[0]["Config"]; assert re.fullmatch(r"[0-9a-f]{64}\.json",n); print(n)' "$archive_manifest")"
case "$config_member" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f].json) ;;
  *) exit 1 ;;
esac
tar -xOf "$image_archive" "$config_member" >"$image_config"
python - "$image_config" "$identity_json" "$config_member" "$RUNTIME_COMMIT" <<'PY'
import hashlib, json, re, sys
raw = open(sys.argv[1], "rb").read()
config = json.loads(raw)
digest = "sha256:" + hashlib.sha256(raw).hexdigest()
assert sys.argv[3] == digest.removeprefix("sha256:") + ".json"
assert config["architecture"] == "amd64" and config["os"] == "linux"
assert config["config"]["Labels"]["org.opencontainers.image.revision"] == sys.argv[4]
layers = config["rootfs"]["diff_ids"]
assert layers and all(re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in layers)
value = {"schema": "subgen.model-envelope.identity/v1", "image_identity": {"config_digest": digest, "layer_diff_ids": layers}}
with open(sys.argv[2], "w", encoding="ascii", newline="\n") as stream:
    json.dump(value, stream, indent=2)
    stream.write("\n")
PY
chmod 0600 "$identity_json"
expected_layers="$(python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["image_identity"]["layer_diff_ids"],separators=(",",":")))' "$identity_json")"
actual_layers="$(docker image inspect --format '{{json .RootFS.Layers}}' "$candidate_tag")"
test "$actual_layers" = "$expected_layers"
(cd "$identity_root" && sha256sum image-identity.json subgen-v050-candidate.tar > transfer.sha256)
docker inspect --format '{{.RestartCount}}' subgen
docker exec subgen cat /sys/fs/cgroup/memory.peak
docker exec subgen cat /sys/fs/cgroup/memory.events
docker exec subgen cat /sys/fs/cgroup/memory.pressure
```

**Expected:** all gates pass. If NVIDIA is unavailable, record GPU behavior as
mocked/Compose-only and leave real envelope authorization to the mandatory
Frigate candidate gate. Shut down only if this task woke the simulator and a
final activity check is clear.

### Task 11: Reconcile simulator evidence and prepare governance

**Files:** source/tests only if simulator disproves a contract; otherwise ADR,
baseline/work verification, index, and plan status.

**Steps:** adjust any evidence-bound fallback, envelope, or threshold through
the owning task; rerun focused/full local and simulator checks; prepare ADR 0002
and record commands/results/digests without private paths, media names, or
credentials. Do not accept the ADR or mark release evidence complete until the
Frigate candidate gate passes. Run the Aegis structural checker and record
pre-existing workspace-format drift separately rather than rewriting unrelated
v0.4 history.

```powershell
python C:\Users\Ashby\.codex\aegis\scripts\aegis-workspace.py check --root .
git diff --check
```

**Expected:** local/simulator evidence is ready for the Frigate gate; any
checker failure is only the already identified pre-existing governance-format
drift.

### Task 11A: Add host-owned shared-GPU priority pressure and refreeze

**Files:** create `subgen_core/priority_pressure.py`,
`monitor_frigate_priority.py`, `priority-monitor.env.example`,
`docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/unloaded_gpu_envelope.py`,
`docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_unloaded_gpu_envelope.py`,
`systemd/subgen-priority-monitor.service`,
`tests/test_priority_pressure.py`, and
`tests/test_monitor_frigate_priority.py`; modify
`subgen_core/resource_probes.py`, `subgen_core/resource_management.py`,
`subgen_core/model_runtime.py`, `subgen_override.py`,
`profile_model_envelopes.py`, `tests/test_model_envelope_profiler.py`, relevant resource/runtime/
package tests, all packaged Compose profiles, configuration/install/migration/
release documentation, ADR 0002, the Task 11B sampler/observer and their tests,
and this plan/work evidence.

**Why:** the sealed candidate-absent 900-second control disproved the assumption
that a positive free-VRAM reserve makes Subgen a last-priority workload on the
shared RTX 3090. Frigate crossed the camera-health boundary for a continuous 75
seconds while roughly 17.5 GiB of VRAM remained free. Global utilization cannot
repair that class because Subgen's own work raises the same counter. The
higher-priority service must own the decision and the existing pressure
controller must remain the sole admission/yield/recovery owner.

**Change Necessity:** scheduling-only cannot react after inference starts,
stopping/pausing a container does not preserve same-core retry and either
retains VRAM or causes restart/startup-scan churn, and a free-VRAM-only release
would not satisfy the approved last-priority behavior. The minimum code-change
boundary is a generic bounded priority-signal reader, one new Frigate-specific
host producer, and wiring into the existing controller/callback/status owners.
Do not add a second admission policy, per-process GPU attribution, CUDA
preemption claim, model downgrade, Frigate mutation API, or Ollama coordinator.

**Existence/architecture decision:** a new host producer is justified because
`monitor_subgen_failures.py` owns failure attribution/deletion and must not also
own higher-priority workload health. `gate_health_sampler.py` is owner-operated
release evidence rather than a production daemon. The new producer publishes
only a coarse file; `priority_pressure.py` owns parsing, `resource_probes.py`
owns observation composition, and `PressureController` remains the only policy
owner. The public default is unset. A configured path is required and fail
closed; there is no companion boolean or caller-side fallback. Strict startup
configuration rejects a non-empty path when `MEMORY_PRESSURE_YIELD=False`, so a
configured signal can never be accepted without an active cooperative consumer.

**Compatibility:** an unset `PRIORITY_PRESSURE_FILE` preserves public v0.5
memory behavior and all v0.4 routes/queue/marker/output behavior. A configured
path cannot create a media failure or marker, authorize deletion, change the
chosen model, publish a partial subtitle, or reveal its path/reason payload in
`/status`. Ashby's canonical Frigate deployment requires the signal and keeps
the explicit five-minute floor, highest-qualified fixed model, invalid-media-
only deletion policy, and no Frigate/Ollama configuration mutation.

**Implementation and regression steps:**

1. Preserve the failed control seal and contention diagnostic as aggregate-only
   evidence. Do not run another candidate while the frozen gate is blocked.
2. Implement the exact maximum-4-KiB schema from the amended design in
   `subgen_core/priority_pressure.py`: absolute configured path, `O_NOFOLLOW`,
   exact regular/mode/owner checks, duplicate/extra/missing-key rejection,
   matching SHA-256 host boot identity using the design's exact canonical UUID
   ASCII preimage, bounded heartbeat/source monotonic ages,
   producer epoch, publication sequence, Frigate source generation, opaque
   observation identity, an exact `pressure`/`clear_eligible` asserted/clear-
   candidate/neutral state, exact policy SHA-256, and bounded allowlisted reason
   codes. Use the exact ASCII observation-ID digest preimage. Require equal
   source generation to retain its first-seen monotonic timestamp and greater
   generation to increase it. Before the first
   valid source, publish no file so the required consumer remains unavailable;
   do not invent a null/sentinel generation. Return typed
    `clear`, `neutral`, `asserted`, or `unavailable` observations without logging input
    values. Reject sequence/source regression and same-sequence mutation; allow
    an exact duplicate read without advancing any counter. Accept only exact +1
    as a normal next publication. A fully validated greater sequence with a gap
    advances the seen sequence/source checkpoint but is itself unavailable/
    critical because an overwritten assertion cannot be excluded; only the next
     exact +1 publication may begin ordinary three-clear recovery. Replay of old
     accepted bytes cannot clear fail-closed state. Keep an exact process-local,
     no-eviction history of at most 4,096 accepted producer epochs; a replay is
     invalid and a 4,097th distinct epoch latches unavailable until process
     restart. Poll at least once per second
     from both the idle observer and active progress path.
3. Add the typed raw observation to `PressureSample` and compose it in
   `read_pressure_sample`. Keep all interpretation in `PressureController`:
   fresh asserted or required-unavailable is critical, closes admission, and
   yields/unloads immediately; recovery needs three strictly increasing clear
   source generations plus every existing resource/model admission check.
   Asserted, unavailable, invalid, incomplete, regressed, or producer-epoch-
   change input resets the count; every producer-epoch change is critical even
   from normal, closes admission, and yields/unloads a resident model. Neutral resets recovery and remains admission-
   closed while recovering without triggering a normal-state yield; duplicate
   source generations never advance it. The first publication in a new epoch
   is not a clear. Reuse the
   canonical control exception, release coordinator, and same-cursor segmented
   retry. Refresh generic host/cgroup/PSI/GPU terms at most once per five seconds
   and cache them only through their existing freshness boundary; when priority
   is configured, both active and idle paths poll/sequence-validate that file at
   least once per second and immediately feed any new priority observation plus
   the still-fresh generic snapshot to this same sole controller.
4. Wire strict environment parsing and public status in `subgen_override.py` /
   `model_runtime.py`. Before scanner, queue worker, profiler, or model setup,
   reject a non-empty `PRIORITY_PRESSURE_FILE` paired with
   `MEMORY_PRESSURE_YIELD=False`; add an exact focused configuration test for
   that rejection and positive controls for an empty path with either boolean.
   One atomic snapshot exposes only configured/state/bounded
   ages, policy hash, latest consumed-observation digest, latched transition-
   observation digest, transition sequence, controller phase,
   normalized coarse recovery reason, distinct-clear count, model residency,
   and monotonic load/unload generations. Add the exact privacy-safe atomic
   sibling workload object (`active`, `chunk_uncommitted`,
   `completion_generation`) from the design; no cursor or duration enters public
   status. Implement the normally-unset owner-only Task 11B receipt
   path/token-digest plus distinct Phase-A/Phase-B workload-digest configuration
   and exact canonical runtime-receipt schema so
   the frozen gate—not an HTTP client—can prove same-cursor yield and full
   completion. Add the exact process-local `runtime_identity` sibling (`epoch`,
   `started_monotonic_ns`) so evidence reset cannot conceal a container/process
   restart. Generate both runtime identity fields in-process once before any
   worker/model activity; never accept them from environment or supervisor.
   Focused tests prove format, atomic snapshot consistency, restart changes the
   epoch, in-process stability, and rejection of injected/invalid values. Add
   the exact process-local `failure_counters` sibling
   (`cuda_oom_generation`, `media_failure_generation`) from the design. Both
   start at zero before worker/model activity, never reset or accept injected
   values, and are read in the same atomic snapshot. Test one increment per
   classified caught CUDA OOM, one increment per accepted terminal media
   failure, no increment for cooperative pressure yield, stability through
   evidence reset, and strict non-boolean integer serialization. Define
   load/unload counters as exact
   successful single-flight residency transitions; failed/no-op/stale/joined
   operations never increment them. Preserve internal `model_release_generation`
   as an admission epoch and never accept it as unload proof. Canonical shared
   CUDA gate tests require the receipt path, token digest, and both distinct
   workload digests to be either all empty or all valid; reject every proper
   subset, equal/invalid hashes, gate concurrency other than one, a foreign or
   out-of-order workload, and Phase B before Phase-A durable completion.
   Canonical shared
   CUDA on Frigate must not start without the path; public non-shared profiles
   remain unset. When unset, report `configured=false,state=disabled`, null
   ages/hashes/digest, and zero priority transition/clear counters. When
   configured before a first valid publication, report `state=unavailable`,
   null ages/hashes/digest, and zero clear count; never fabricate clear.
5. Implement `monitor_frigate_priority.py` as a standalone low-priority host
   service. Before probing, safely invalidate an old same-boot file through a
   verified owner/mode-matching parent directory descriptor and fsync the
   directory; unsafe targets stop the service untouched. It starts with no
   output file so the required consumer is unavailable/fail-closed until the
   first valid source. Poll exact configurable literal-127.0.0.1 origins, fixed
   Frigate `/api/stats` and `/api/version`, Ollama `/api/ps` (never `/api/tags`),
   and local NVIDIA telemetry every five seconds with the design's exact 1/2/3-
   second connect/read/total deadlines, body/subprocess caps, and redirect
   rejection. Require absolute
   `FRIGATE_PRIORITY_POLICY_FILE` and `FRIGATE_CONFIG_FILE`, plus an exact
   lowercase-64-hex `FRIGATE_PRIORITY_POLICY_SHA256`; refuse silent policy-file
   replacement and enforce the exact
   canonical maximum-32-KiB policy schema, owner/mode/symlink checks, exact
   config-file SHA-256, Frigate version, camera/detector sets, required positive-
   finite embeddings, and conditional-idle pair semantics. Publish and status-
   bind the canonical policy SHA-256. The policy binds the expected Frigate
   version, camera/config fingerprints, and Ashby's private total-detection-FPS
   limit of 80. Compute each ratio exactly as process FPS divided by that
   camera's expected process FPS. Count decisions only when
   `service.last_updated` strictly increases. A same-generation normalized
   Frigate/NVIDIA decision-input mutation, including any bound source identity,
   is unavailable/fail-closed; an exact Frigate/NVIDIA duplicate may refresh the
   heartbeat. Ollama is deliberately outside that equality tuple: union the
   current exact `/api/ps` decision on every poll, so a newly loaded model
   asserts `higher_priority_busy` immediately even while the Frigate generation
   repeats. Assert immediately on unavailable or invalid required telemetry,
   topology/policy drift, any loaded Ollama model, or distinct skipped FPS above
   zero; assert after the single global detection-high or any-camera-low streak
   reaches two distinct generations. Reset the low streak only when every ratio
   is at least 0.95. Every distinct complete source generation
   below 80 detection FPS, with zero skips, every ratio at least 0.98, valid
   detector/conditional-idle embedding/NVIDIA telemetry, and no Ollama model
   emits one clear candidate immediately. A first high/low sample or a ratio in
   the 0.95-through-below-0.98 deadband emits neutral/pending; it never counts
   clear or reopens admission. Reset each assertion streak on its healthy-side
   boundary and reset both on unavailable/invalid/epoch/policy drift. The
   producer does not count clear recovery; PressureController alone requires
   three consecutive distinct clear candidates. Duplicate source generations
   refresh only the heartbeat. Write a mode-0600 temporary file, fsync, atomic replace, and
   directory fsync. Map busy/degraded/unavailable/policy-drift reasons exactly
   as the design specifies and emit their sorted union. Never emit camera names,
   raw endpoint data, URLs, or credentials.
6. Add the disabled-by-default systemd/example/Compose/docs surfaces. Mount the
   parent signal directory read-only rather than the file. The Frigate operator
   profile uses an explicit path; public examples leave it empty.
7. Extend the sampler and runtime observer to attest the exact read-only mount,
   configured environment, signal/source freshness, private-policy identity,
   producer unit, and causal cooperative-yield sequence. First require a real
   valid-telemetry busy/degraded assertion with neither unavailable nor policy-
   drift reason. Before N, bind a resident selected model and an active
   uncommitted disposable workload/cursor with no output or marker. Establish
   the normally-disabled owner-only Task 11B append-only runtime-receipt journal
   and validate its runtime epoch, boundary-token hash, workload hash, strict
   sequence, exclusive-create/append/fsync inode safety, exact model-identity
   digest, failure generations, and state/cursor rules; no cursor enters HTTP
   status or logs. Tail complete records while Phase A is active, reject a
   partial record, inode replacement, size regression, duplicate, mutation, or
   sequence gap, and preserve every consecutive publication from sequence one's
   initial null-workload gate receipt through Phase-A completion in one
   canonical owner-only trace for strict
   release verification. Tests prove that multiple publications between
   observer polls remain recoverable in order; polling cadence is never accepted
   as a losslessness argument.
   Establish
   creation watches on the owner-only disposable output and marker roots before
   starting that workload and preserve monotonic creation counters that deletion
   cannot reduce. The candidate consumes observation N. Define one T0 as the
   host supervisor monotonic timestamp immediately after
   opening and validating N from the final atomically replaced path. Relative to
   that T0, require the first atomic status binding N/transition/phase/reason by
   15 seconds, and `model_unload_generation=prior+1` plus non-resident by 30
   seconds.
   Implement and test the exact `subgen.unloaded-gpu-envelope/v1` generator;
   Task 11B generates the artifact only after it chooses the highest-qualified
   model. Bind runtime/image/layers/device/driver/backend/tool identities plus
   exact model ID/revision, compute type, task/language, device index, and chunk/
   overlap policy. Measure three clean exact-policy load/inference/unload cycles
   and ten one-second exact-cgroup-PID/exact-GPU-UUID process-memory samples per
   cycle, binding each load/unload generation transition. Set the exact bound
   to the 30-sample maximum plus 128 MiB; treat a verified absent candidate PID
   as zero and any attribution/query ambiguity as invalid. Require current
   candidate-attributed bytes at or below that matching bound within 45 seconds.
   From T0 through that proof, sample the original camera/detector/embedding
   contract continuously with no gap over two seconds and seal cumulative sample,
   blind-interval, and threshold-failure counts. Do not mask a camera threshold
   until both runtime unload and independent GPU proof exist; masking is eligible
   only while the candidate remains nonresident. Keep load generation unchanged until three distinct clear
   source generations, then prove that same workload retries the same cursor and
   completes after post-recovery admission/reload with no partial output,
   marker, restart, or OOM. An unavailable assertion does not satisfy
   this proof. Reset evidence after the episode and require a separate
   uninterrupted 900-second clear-signal candidate pass with every original
   health threshold enforced; any state other than exact clear, including
   neutral/asserted/unavailable/epoch change, resets the timer. Make the observer
   implement the exact canonical Phase-A, Phase-B, and final v2 seal verifier,
   including duplicate-key/type rejection and cryptographic binding of both
   phases, policy, producer, unloaded envelope, image, and all four gate programs.
   Wire `profile_model_envelopes.py` through the same typed observation reader,
   `PressureController`, control exception, progress callback, and canonical
   model-release owner as the automatic runtime. Before load it waits for normal
   admission; pressure arising during inference unwinds the uncommitted fixture,
   unloads, waits for the same three-distinct-clear recovery, and retries that
   fixture cursor without treating the event as allocation/capacity failure or
   writing/promoting a catalog entry. It must not implement a second signal
   policy. Focused tests inject assertion from inside the transcribe callback and
   prove prompt unload/wait, fixed-model preservation, same-cursor retry, and no
   capacity receipt/catalog promotion until one uninterrupted inference passes.
8. Run focused Windows and Linux regression, module-boundary, static, package,
   and complete suites. Build on the approved local/simulator route only. Repeat
   real signal assertion/recovery on disposable media without synthetic GPU
   load or real-media deletion.
9. Commit cohesive source, producer/package, gate, and governance slices only
   after their complete focused checks. Rebuild the immutable candidate from
   the new runtime commit, regenerate every config/layer/archive/identity hash,
   reprofile every required ModelEnvelope, refreeze sampler/observer hashes,
   and repeat the moved-bind `SIGKILL` lifecycle proof before Task 11B.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTEST_PLUGINS='requests_mock.contrib._pytest_plugin'
python -m pytest -q -p no:cacheprovider `
  tests/test_priority_pressure.py `
  tests/test_monitor_frigate_priority.py `
  tests/test_resource_probes.py `
  tests/test_resource_management.py `
  tests/test_model_runtime_resources.py `
  tests/test_model_envelope_profiler.py `
  tests/test_transcription_segmentation.py `
  tests/test_module_boundaries.py `
  tests/test_packaging.py
python -m pytest -q -p no:cacheprovider
python -m ruff check --select E9,F63,F7,F82 .
python -m compileall -q subgen_core subgen_override.py `
  monitor_frigate_priority.py profile_model_envelopes.py
git diff --check
```

Repeat the focused and complete tests inside the approved Linux environment
with an ext4 `/tmp` pytest base directory, render every Compose profile with
`docker compose config`, and run the existing simulator image/real-inference/
pressure/atomic-output/destructive-fixture smokes against only disposable
media. Do not use or trigger GitHub-hosted runners.

**Evidence reset:** runtime commit `4418b3c9`, its candidate image/OCI/archive/
identity hashes, all identity-bound ModelEnvelope entries, sampler commit
`86ac798`, its execution-boundary manifest and lifecycle proof, Gate1, Gate2,
and their automatic-runtime chain become diagnostic history only. The failed
candidate-absent control remains valid root-cause evidence but is not authority
for the new policy. Regenerate all publication and deployment evidence from the
new exact runtime and reviewed gate commits.

**Expected:** higher-priority Frigate/Ollama health owns a bounded stale-fail-
closed signal; Subgen cooperatively unloads/waits/retries without changing model
quality or media state; public unset behavior remains compatible; and a new
immutable candidate is ready to restart Task 11B from `large-v3`.

**Post-amendment identity barrier:** no Task 11B, 11C, 12, or 13 command is
executable until Task 11A records fresh, non-placeholder values for
`TASK11A_RUNTIME_COMMIT`, `TASK11A_SAMPLER_COMMIT`,
`TASK11A_CANDIDATE_TAG`, `TASK11A_OCI_INDEX`, `TASK11A_CONFIG_DIGEST`,
`TASK11A_LAYER_DIFF_IDS`, `TASK11A_IDENTITY_ROOT`, and the transferred
sampler/observer/policy hashes. Those values must bind one exact post-amendment
source commit and must differ from retired runtime `4418b3c9`, sampler
`86ac798`, OCI index `sha256:61dc0b...`, and config digest
`sha256:d87f84...`. Literal pre-amendment values later in historical evidence
or cleanup inventories are diagnostic/retirement references only. Every active
snippet below reads the fresh values from owner-only Task 11A evidence and
fails closed when any value is absent, malformed, uncommitted, or mismatched.

### Task 11B: Gate the exact candidate on Frigate before publication

**Systems:** Frigate-hosted Subgen v0.3.0, the exact v0.5.0 candidate image,
Docker/NVIDIA runtime, isolated disposable media, and read-only Frigate health.

**Entry boundary:** deployment is currently blocked. The passive snapshot (24
GiB VM maximum/20 GiB balloon floor, about 7.5 GiB `MemAvailable`, no loaded
Ollama model, about 18.1 GiB free VRAM) did not bound incremental
Frigate/Ollama demand. Future evidence must either bound that demand or choose
and demonstrate a conservative explicit reserve. The proposed envelope sets
`device_margin_bytes = 2,048 MiB` as the additional reaction margin beyond the
measured device incremental peak, so required device bytes are exactly
`device_incremental_peak_bytes + device_margin_bytes`. Live device admission
is separately `max(0, free_vram - gpu_priority_reserve)`; the priority reserve
is subtracted once and is neither included in nor added again to the envelope
margin. One idle snapshot is not authority.

**Steps:** after ownership is released, require either an ordinary empty queue
or a proven scan-only handover: the immutable legacy container has only its
launcher/scanner processes, no transcription/FFmpeg child, no active output,
and no post-restart worker. A scan-only handover may be stopped because v0.5
performs a fresh startup scan and reconstructs work; waiting for the restarted
legacy enumeration to reach its known crash candidate would add churn without
preserving an in-flight transcription. Capture the live baseline and preserve
the complete v0.3.0 Compose/config, model cache,
generation registry, application state, OCI index, inner config digest/ordered
diff IDs, and
the enabled/active states of `subgen-monitor.service`, `subgen-repair.timer`, and
`subgen-repair.service`. Stop/disable the legacy monitor and repair timer/service
before stopping the old Subgen container, and verify all three units inactive
and the monitor/timer disabled. Restore captured unit states only during a
deletion-off v0.3.0 rollback, never for the v0.5 candidate.

Before the first live sample, freeze the amended Task 11B protocol,
`gate_health_sampler.py`, `test_gate_health_sampler.py`,
`runtime_gate_observer.py`, and `test_runtime_gate_observer.py` at the fresh
full `TASK11A_SAMPLER_COMMIT` recorded after Task 11A. It must bind the new
signal/status/mount/policy contract and must not equal `86ac798...`. Commits
`da603ff`, `fd8af61`, `86ac798`, and `7254df3` remain historical verification
lineage and cannot authorize the new run.
These four Python
files are owner-operated host-side evidence
tooling, not Subgen runtime source, repository product tests, an image build
input, production configuration, or an installed release artifact. The
unchanged `.dockerignore` excludes `docs`, and the unchanged Dockerfile's
explicit `COPY` set contains neither file. Record the sampler commit, Git blob
IDs, and SHA-256 values of the exact bytes transferred to Frigate; never mount
them into the candidate. Any sampler change after sampling starts invalidates
and restarts Task 11B evidence, but it does not change the frozen runtime image.

Create each disposable candidate in the stopped state with restart policy
`no`, a unique gate token generated exactly as `secrets.token_hex(16)` (32
lowercase hexadecimal ASCII characters representing 128 random bits), the dedicated Task 11B, role,
runtime-commit, and token labels, the exact memory/no-extra-swap boundary, and
no Docker-socket mount or host network. On Frigate's Docker 29/containerd image
store, require both the local image `.Id` and stopped container `.Image` to be
the exact post-amendment `TASK11A_OCI_INDEX` from the owner-only identity
record;
pass that index to the sampler's historically named
`--expected-image-config` argument. Independently hash the exact saved inner
configuration bytes and require their digest to equal the post-amendment
`TASK11A_CONFIG_DIGEST`
and require its ordered rootfs diff IDs to equal the owner-only identity
artifact. The index is the container/image-store identity on this host; the
inner configuration digest and ordered layers are separate, jointly required
identity evidence.

The sampler must bind the full stopped container ID, OCI index,
intended-command SHA-256, labels, local Docker Engine ID, and host boot ID before
it opens evidence or starts the container. Create a profiler candidate with
`--network none`, then detach Docker 29's stopped-container `none` placeholder
by full ID with `docker network disconnect none "$full_id"` before creating the
boundary manifest; require `HostConfig.NetworkMode` to remain exactly `none`
while `NetworkSettings.Networks` becomes exactly empty.
This detach applies only to the no-network profiler. The automatic runtime uses
its exact `bridge` attachment and must not inherit the profiler detach step.
Use explicit bind syntax `-v "${source}:${destination}:rw"` or
`-v "${source}:${destination}:ro"` so every inspected bind has `Mode` exactly `rw`
or `ro`, matching `RW`, with propagation exactly `rprivate`. Request the GPU
with exactly `--gpus driver=nvidia,count=all`, yielding the single NVIDIA device
request with driver `nvidia`, count `-1`, null device IDs, capabilities
`[["gpu"]]`, and empty options. Use exactly `--log-driver=json-file --log-opt
mode=blocking`; `SAFE_LOG_CONFIG` is exactly
`{"Type":"json-file","Config":{"mode":"blocking"}}`, with no `max-size`,
`max-file`, or any other logging option.

The create-only execution-boundary manifest uses schema 3 and binds the complete
Docker execution boundary, including direct `/usr/bin/python3` execution as
UID/GID `1000:1000`, working directory `/subgen`, a read-only root filesystem,
all Linux capabilities dropped, `no-new-privileges`, the exact bounded `/tmp`
tmpfs, the role-specific network, exact NVIDIA device request, exact blocking
`json-file` configuration, every mount source/destination/mode, and the full
command and environment digests. Docker 29 may expose `OomKillDisable` as null
for a created container and false after start. Canonicalize only null and false
to false before hashing; reject a missing key, true, or every other type/value.
Every other host-config field remains exact and hash-bound. Hash the manifest
independently before asking the sampler to emit its transient-systemd wrapper.
Its `ownership_labels` object contains exactly the four required entries
`io.github.herbertmt978.subgen.task11b-gate="true"`,
`io.github.herbertmt978.subgen.gate-token=<the exact 32-lowercase-hex token>`,
`io.github.herbertmt978.subgen.gate-role=<the exact role>`, and
`io.github.herbertmt978.subgen.runtime-commit=<the exact runtime commit>`.
Other immutable OCI source labels remain bound by the candidate image identity
but are not copied into this ownership object; duplicate, missing, or unexpected
ownership entries are invalid.
The manifest includes the canonical lowercase full container ID and the same
runtime/image/model identity used by the candidate record. The frozen sampler
must regenerate it from a fresh `docker inspect` immediately before start and
again after the stopped/PID-zero cleanup; canonical schema-3 bytes must match
both times. The automatic-runtime Phase A, Phase B, candidate record, and final
seal each carry that one manifest SHA-256. A command digest, token digest, or
phase identity can never substitute for the manifest preimage.
The wrapper must register immutable-ID cleanup in `ExecStopPost` before starting
the sampler; the final caller then executes that already materialized wrapper
without reconstructing its arguments.

The sampler starts and addresses the candidate only by full ID. Every
non-sealed-pass path, including output/open/fsync failure, exception, `SIGINT`,
`SIGTERM`, `SIGHUP`, or failed profiler receipt/catalog validation, must stop
(escalating only for that reverified disposable ID) and prove it stopped before
writing abort evidence. The systemd cleanup path must remain able to stop the
same immutable container if a disposable bind source disappears after start;
that disappearance invalidates evidence but cannot veto cleanup. A name-reuse
race must stop the labelled original by ID and must never touch the
replacement. Neither profiler nor automatic runtime may be retained after a
passing gate: verified stopped state precedes the durable pass seal, and the
seal is verified before the final evidence filename becomes visible.

As historical supervisor evidence, sampler commit
`390b63c03e61e5da3ca58852a9da094310473543` passed the final supervisor
failure proof: the sampler was sent `SIGKILL` after its disposable candidate
started, the disposable bind source was moved, and systemd `ExecStopPost`
revalidated and stopped only the exact full container ID, then proved it
stopped. The moved source invalidated evidence but did not veto cleanup; no
container name or prefix was used to select a cleanup target. Commit `da603ff`
changed the sampler blob after that proof, so the historical result is not
authority for `SAMPLER_COMMIT`. The same statement now also applies to
`fd8af61618605025ef9f912de8317dc6e5182c8c` because the five-minute policy
changes the sampler bytes. Before any live sample, rerun the identical
`SIGKILL` plus moved-bind-source lifecycle proof at the new `SAMPLER_COMMIT`,
require the same exact-ID-only `ExecStopPost` outcome, and record the final
sampler/test blobs and hashes. Any failure blocks and restarts Task 11B rather
than inheriting an older result.

The now-historical `fd8af616` rerun completed on 2026-09-01 as
`integration6-large-v3`. With sampler SHA-256
`7caa9483f095c9aa565e094c3d653644282c143838d1c6e6caaf5a393808951b` and
test SHA-256
`77b494d59ef59bcb34534f8b2f622f6647dcbf6e00204396f99a4d45c8031784`, the
sampler was sent `SIGKILL` after the Docker 29 candidate was running and its
bind source had been moved. `ExecStopPost` emitted
`TASK11B_CLEANUP_OK verified_stopped=true`, the unit became inactive, and the
exact candidate was proved stopped before removal. The preserved journal and
wrapper-log SHA-256 values are
`1716764cb8350aa7357ade6411bf7bce412d42aa90b6d2bfd91f3263ca53d9fb` and
`bab7ce3ca99037cb090f3b6267698cd0d34727912677aa79d5357b29e7c99dfd`.
Frigate remained `running healthy 0`, Ollama had no loaded model, and GPU use
returned to the 6,134 MiB baseline. It proved the older sampler's cleanup path
but is not authority for the new `SAMPLER_COMMIT`.

The first `medium` profile at the automatic 20-minute chunk policy then
repeatedly failed the higher-priority workload boundary. Both the ordinary
four-CPU candidate and a one-CPU diagnostic candidate reached the immediate
`frigate_camera_skipped_fps_exceeded_threshold` abort at about 132 seconds.
The no-Subgen baseline had zero skipped FPS, both candidates remained far below
their 12 GiB cgroup limits, about 15 GiB of VRAM remained free, and neither
candidate nor Frigate restarted or reported OOM. The CPU-only mitigation is
therefore rejected: the evidence isolates a sustained shared-GPU compute burst,
not host memory, VRAM capacity, or CPU scheduling. CUDA MPS, signal pausing,
and stream-priority changes are also rejected for this release because they do
not provide a supported priority or preemption guarantee and would widen the
Frigate failure domain.

The bounded mitigation stays inside the frozen runtime image's existing public
configuration contract. Frigate alone uses an explicit five-minute core chunk
policy; public profiles retain `SEGMENTATION_CHUNK_MINUTES=auto`. The profiler
must measure the exact five-minute policy and the final automatic runtime must
use the same value, so the exact ModelEnvelope match remains fail closed. The
sampler must bind an explicit expected chunk policy and reject a profiler or
runtime command/configuration that differs. Because this changes the sampler
after sampling began, the `fd8af616` lifecycle and large-v3 results become
historical evidence only: freeze a new `SAMPLER_COMMIT`, rerun the moved-bind
`SIGKILL` lifecycle proof, and rerun the complete large-v3 and medium gates from
a clean candidate before using any result for publication.

The superseded pre-amendment five-minute sampler was frozen at
`86ac798bdbe26fcaf7116e40c0ae33d86f6bdfc1`, with sampler SHA-256
`9c1844ee252ec9d04bba704acbed9e1393dc96add7202d63866cb37f5558453f`
and test SHA-256
`3805f5815c4a823a634000750cb807625b9392e975e4ac6c94f0ffe7d775545e`.
Its focused suite passed all 61 tests on Linux and 54 tests plus seven expected
platform skips on Windows, with 106 subtests on both platforms; independent
review reported no remaining P0/P1/P2 findings for that historical contract.
These hashes cannot authorize a post-Task 11A run even though the historical
moved-bind lifecycle proof passed.

That new lifecycle proof passed on 2026-09-01 as
`integration8-large-v3-5m`. The five-minute fixture was revalidated immediately
before start, the exact Docker 29 candidate was running beneath transient unit
`subgen-task11b-d0201e4441655c71`, and the sampler's main process was sent
`SIGKILL` only after the disposable input bind source had been moved. The unit
journal records both the `SIGKILL` and
`TASK11B_CLEANUP_OK verified_stopped=true`; the exact full candidate ID was
then revalidated as stopped and removed. The owner-only wrapper log and journal
SHA-256 values are respectively
`dc69f3f2b987455fbae2f43d5ee88b8f9a0b2bb3891cc12141c0f8c54c534b79`
and
`00d97e7a150022338ba38e84fc192daf3dea856fd9792970eb4b41349e70614e`.
The copied journal is the bounded sanitized artifact: its ephemeral gate token
and private gate-root path were replaced before the file was fsynced; token and
host-path absence plus the single cleanup/SIGKILL receipts were revalidated;
and both earlier unsanitized copied files were retired. The protected system
journal remains the source record.
Frigate remained healthy with restart count zero, Ollama had no loaded model,
and GPU memory returned to 6,198 MiB used / 17,929 MiB free. This proves only
the retired `86ac798` cleanup mechanics; the amended sampler must repeat the
proof before any new five-minute gate.

The first fresh large-v3 attempt, `largev3-5m-gate1`, is preserved as a
non-authoritative environmental abort. It stopped immediately on
`frigate_camera_skipped_fps_exceeded_threshold`, before the profiler could
write a result receipt. The sampler verified cleanup, the candidate had no OOM
or restart and was removed by exact ID, Frigate remained healthy at restart
count zero, and Ollama remained unloaded. Its owner-only JSONL, seal, and
wrapper-log SHA-256 values are respectively
`588e34b96492cc5f082f1979ebac3352c88aaa94a93e1fe647ecfd14974adaf0`,
`c80f04eb0014a9787d90b7c0bfd38535678765c7145a7479bc95d998b96dc7f9`,
and
`5c08783217158713065920d92418d8383cb7462ee0ee34dc05ad22bfe6787fe5`.
The attribution is bounded by a direct Frigate-only baseline taken after the
candidate was absent: all seven samples across 30 seconds already breached the
skipped-FPS boundary, with a maximum 4.8 skipped FPS, a minimum 0.25 process
ratio, up to four cameras breaching skipped FPS, and up to seven below the
process-rate boundary. Do not weaken the safety gate or treat this as a Subgen
profile result. Rerun large-v3 from a new clean candidate only after Frigate
alone has a clean admission baseline.

The second fresh large-v3 attempt, `largev3-5m-gate2`, produced a create-once
profiler receipt recording the expected return code `3`; its retained stdout
described the expected `large-v3` safe-capacity failure and no catalog was
promoted. At about 75 seconds, however, the shared-health observation aborted
on `frigate_camera_skipped_fps_exceeded_threshold`. The candidate had no OOM or
restart increase, Frigate had no restart increase, and exact-ID cleanup was
verified. The owner-only profiler result and stdout SHA-256 values are
respectively
`40a3e7ffbd74d2b74feb6dd6bab6ab316fb27ffee12ed7f804185168bc45375e`
and
`6d7cc601c2f021a80685dd7489c226e1fe9bfbeb5fe046ddde268cdf527ab31d`;
the JSONL, seal, and wrapper-log SHA-256 values are respectively
`40002301d5fe9b33658934341b4b6a0e3ae4621f4c03ced292633b5fa776b589`,
`ae7c3ebc31dc8175a3219e25b5e7f9638edfdd47508b5e4873a940d8ae7ef573`,
and
`22200fbd46eb4231d04bb9a8a3cf73a168974705826efab3a862f29556ab4804`.

This is non-authoritative diagnostic capacity evidence only. The frozen
sampler validates profiler completion after the complete `t=900` sample and
final drain; this attempt therefore has an abort seal and no
`gate_observation_final`, `gate_pass`, or pass seal. Its return-code receipt
does not establish the trusted-host assertion required for
`medium --after-safe-failure large-v3`, so medium profiling remains blocked.

The following 900-second candidate-absent Frigate control used the same camera
map, five-second cadence, and unchanged health thresholds. Its 181 samples
covered 900.071 seconds and sealed `clean_for_gate=false`: 51 samples breached
the skipped-FPS boundary, 45 contained a low process ratio, both longest
continuous breach streaks lasted 75 seconds, maximum skipped FPS was 6.3, and
minimum process ratio was 0.2. Frigate remained running and healthy with no
restart or OOM change, Ollama remained unloaded, and every environment sample
was otherwise valid. The mode-0600 root-owned JSONL and seal SHA-256 values are
respectively
`68486f7d0159170737b616808549452b1da6a796bd599a65377402fb1b2c3d7d`
and
`163605cb9ac4806708febe96ee902046379c366e1223a8ce3194a35bee70f564`.

This control prevents attributing gate2's breach solely to Subgen, but it is
not a waiver and does not prove zero Subgen contribution. It is a failed
admission baseline: do not run another candidate, do not run `medium`, and do
not reuse gate2's receipt under the frozen policy. Read-only correlation during
the control showed camera-health breaches while roughly 17.5 GiB of VRAM was
still free and much higher GPU compute utilization during breach samples than
clean samples. This exposes a design gap: the runtime's pressure controller
sees memory headroom, memory PSI, OOM counters, and free VRAM, but not
higher-priority GPU compute demand. Close and re-review that gap before
refreezing the candidate and Task 11B evidence.

Keep the private 15-camera FPS map outside Git in a mode-0600 file under an
owner-only directory. Evidence contains only anonymized aggregates and hashes,
never camera names, GPU PIDs/process names, raw logs, endpoint URLs, gate
tokens, or host paths. All HTTP endpoints are exact `127.0.0.1` origins/paths
with redirects rejected and hard time/byte limits. Docker/kernel logs use
bounded incremental overlapping windows and include a final scan. Parse
`memory.events` with the complete expected key set and parse cgroup/host PSI;
PSI is explicitly observation-only because this gate has no approved external
PSI threshold. JSONL evidence is written through a held owner-only directory
FD to a create-once partial file, then finalized without overwrite and paired
with a mode-0600 fsynced checksum/record-count/outcome seal.

```bash
set -euo pipefail
systemctl is-active subgen-monitor.service subgen-repair.timer subgen-repair.service > legacy-unit-active.txt || true
systemctl is-enabled subgen-monitor.service subgen-repair.timer subgen-repair.service > legacy-unit-enabled.txt || true
sudo systemctl disable --now subgen-monitor.service subgen-repair.timer
sudo systemctl stop subgen-repair.service
sudo systemctl disable subgen-repair.service 2>/dev/null || true
test "$(systemctl is-active subgen-monitor.service || true)" = inactive
test "$(systemctl is-active subgen-repair.timer || true)" = inactive
test "$(systemctl is-active subgen-repair.service || true)" = inactive
test "$(systemctl is-enabled subgen-monitor.service || true)" = disabled
test "$(systemctl is-enabled subgen-repair.timer || true)" = disabled

sha256sum --check transfer.sha256
docker load --input subgen-v050-candidate.tar
runtime_commit="${SUBGEN_TASK11A_RUNTIME_COMMIT:?missing post-amendment runtime commit}"
candidate_tag="${SUBGEN_TASK11A_CANDIDATE_TAG:?missing post-amendment candidate tag}"
expected_index="${SUBGEN_TASK11A_OCI_INDEX:?missing post-amendment OCI index}"
expected_config="${SUBGEN_TASK11A_CONFIG_DIGEST:?missing post-amendment config digest}"
artifact_config="$(python -c "import json; print(json.load(open('image-identity.json'))['image_identity']['config_digest'])")"
expected_layers="$(python -c "import json; print(json.dumps(json.load(open('image-identity.json'))['image_identity']['layer_diff_ids'], separators=(',', ':')))")"
test "$artifact_config" = "$expected_config"
test "$runtime_commit" != "4418b3c97296a04b311d29d9ce52abefef64e108"
test "$expected_index" != "sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc"
archive_config_digest() {
  python - <<'PY'
import hashlib
import json
import tarfile

with tarfile.open("subgen-v050-candidate.tar", "r") as archive:
    manifest = json.load(archive.extractfile("manifest.json"))
    assert len(manifest) == 1
    config_name = manifest[0]["Config"]
    raw = archive.extractfile(config_name).read()
    print("sha256:" + hashlib.sha256(raw).hexdigest())
PY
}
verify_candidate_identity() {
  test "$(docker image inspect --format '{{.Id}}' "$candidate_tag")" = "$expected_index"
  test "$(archive_config_digest)" = "$expected_config"
  test "$(docker image inspect --format '{{json .RootFS.Layers}}' "$candidate_tag")" = "$expected_layers"
}
verify_candidate_identity
sudo install -d -m 0700 /var/lib/subgen/model-envelopes/v1
sudo install -m 0600 image-identity.json /var/lib/subgen/model-envelopes/v1/image-identity.json
```

Run the checksummed explicit profiler with separate model cache, state,
catalog-output, and disposable-media roots in a profiling-only cgroup created
with exactly `--memory=12g --memory-swap=12g`. This is a 12 GiB hard limit with
no extra swap and is never the automatic or production limit. Retain low CPU
priority, the same explicit priority reserve and fresh host/cgroup/GPU
admission, the already verified legacy-unit isolation, startup scan off,
monitor/notifications off, both deletion switches false, and every immediate
Frigate abort threshold. Use the Task 2A writer to create and validate an empty
canonical catalog for the first run; install it mode 0600 beside the identity.
Immediately before every profiler container start, run
`verify_candidate_identity` as the preceding command in the same `set -e`
shell, mount the canonical catalog and identity files read-only at their exact
`/opt/subgen/model-envelopes/` paths, mount the exact owner-only priority parent
read-only at `/run/subgen-priority`, set the required
`PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json`, verify the active
producer/private-policy hash, and mount a distinct owner-only staging directory
writable at `/profile-output`. The profiler must fail closed or wait under the
same asserted/neutral/unavailable policy as automatic runtime; public unset
fallback is prohibited on this host. Invoke the packaged profiler for
explicit `large-v3` only after Task 2B approves the generic
incremental-peak-plus-margin admission under the fresh 12 GiB cgroup state:

Replace the earlier 20-minute profiling fixture with a new disposable audio
fixture whose probed duration is 310 seconds (five minutes plus both five-second
overlap guards), within the profiler's one-second tolerance. Record its regular-
file identity, byte length, duration receipt, and SHA-256 in a separately
checksummed owner-only fixture record, then revalidate that record immediately
before boundary generation and again immediately before start. The schema-3
execution boundary binds the exact read-only input mount and container path; it
does not claim to carry fixture-content fields. Never reuse the old 20m10s media
checksum for the five-minute policy.

The profiler command runs beneath a PID-1 hold protocol. The hold process
captures a bounded stdout artifact and a create-once return-code receipt, then
keeps the container alive for the complete 900-second shared-health observation
so the sampler can supervise Frigate while validating the durable profiler
result. Pass and bind `--chunk-minutes 5` for every Frigate profiler candidate.
Start with `large-v3 --runs 3 --chunk-minutes 5` under the regenerated exact
image. If it succeeds, validate its staged catalog and still require fresh 10
GiB automatic admission. If it returns the profiler's documented safe capacity
code `3`, require no catalog promotion, destroy and verify that container
stopped, then run `medium --runs 30 --chunk-minutes 5 --after-safe-failure
large-v3` in a clean container. Continue downward only after an exact safe
failure. No pre-amendment result predetermines the selected model. The sampler
validates each exact expected return code, receipt, bounded stdout, staged
catalog presence/integrity when successful, model identity, and five-minute policy; the
host must additionally run Task 2A's complete catalog loader/matcher before
installing the successful staged catalog. That external full validation remains
mandatory because sampler-side catalog inspection is deliberately only a
bounded gate check.

On safe admission/allocation failure, destroy that profiler container, verify
model/cache release, and repeat in clean explicit 12 GiB profiling processes
for `medium`, `small`, `base`, then `tiny` only as needed. Do not rebuild. After
each successful run, validate the staged artifact through Task 2A and atomically
install it mode 0600 as the next canonical catalog, then destroy the profiler
and verify model/cache release. Before the automatic container start, run
`verify_candidate_identity` again as the immediately preceding command. Mount
both canonical files and the owner-only `/run/subgen-priority` parent directory
read-only, set `MODEL_ENVELOPE_CATALOG`, `MODEL_ENVELOPE_IDENTITY`, and
`PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json` to their exact
container paths, require the active producer/private-policy identity to match
the frozen Task 11A boundary, and restart the exact
image with auto in a new cgroup created with exactly `--memory=10g
--memory-swap=10g` and `SEGMENTATION_CHUNK_MINUTES=5`. Require strict
identity-to-catalog/current-runtime/policy
matching, three fresh stabilized samples, and immediate host/cgroup/GPU checks
showing that measured incremental peaks plus explicit margins and the separate
reserves fit the fresh 10 GiB boundary. The 12 GiB profiling cap is evidence
only and cannot qualify `large-v3`; if it fails fresh 10 GiB admission, do not
load it, profile `medium` and lower as needed in clean 12 GiB profiler
containers, and repeat the fresh 10 GiB auto start until the highest qualified
entry is selected or no-safe-model recovery is required.

After the highest-qualified model is known, stop and remove that qualification
container by exact full ID, then invoke the checksummed Task 11A
`unloaded_gpu_envelope.py` owner tool for three clean disposable 10 GiB/no-swap
cycles using the exact image, selected immutable model policy, five-minute
fixture, GPU UUID/driver/backend identities, priority-policy hash, and read-only
priority mount. Each cycle must prove exactly one load, completed inference,
exactly one canonical unload, non-residency, and ten valid one-second candidate-
attributed GPU samples. Validate the literal
`subgen.unloaded-gpu-envelope/v1` schema and arithmetic, write it create-once
mode 0600 with file/directory fsync, record its SHA-256, destroy every cycle
container, and prove no candidate PID remains. Start the final Phase-A/Phase-B
automatic gate only after mounting that exact artifact read-only into the owner
observer; bind its hash, selected policy, runtime/image/layers, and producer
identity into the final gate seal. For that final candidate only, create a
distinct owner-only receipt parent beneath the disposable gate root, mount it
writable at `/run/subgen-task11b`, set
`TASK11B_GATE_RECEIPT_FILE=/run/subgen-task11b/runtime-receipts.jsonl`, and set
`TASK11B_GATE_TOKEN_SHA256` to the canonical ASCII/no-newline digest of the exact
32-lowercase-hex ownership-label token. Set distinct exact lowercase-64-hex
`TASK11B_PHASE_A_WORKLOAD_SHA256` and `TASK11B_PHASE_B_WORKLOAD_SHA256` values
from the two prevalidated private fixture identities and require
`CONCURRENT_TRANSCRIPTIONS=1`. The schema-3 boundary manifest binds the mount,
all four gate environment entries, both fixture-record hashes, their exact
read-only mounts, and token label/hash equality; qualification, public, and
production containers leave all four gate variables unset. Any model/policy/runtime/image/device/tool
change discards the artifact and repeats all three cycles.

Across every 12 GiB profiler and the final 10 GiB auto run, measure cold load,
first inference, long disposable translation, unload/reload, idle-resident
unloading, cgroup/device peaks, and identity continuity. The selected model is
the highest regenerated catalog entry that passes fresh 10 GiB admission; never
hard-code `medium`. Abort immediately on NVIDIA Xid, cgroup/CUDA OOM, container
restart increase, invalid/unavailable producer or policy identity, or any
camera/detector/embedding threshold outside the protected episode defined
below. Do not inject synthetic GPU pressure. Test v0.3.0 marker compatibility
in isolated state. On any failure, set deletion off and restore the exact
v0.3.0 rollback identity/cache/config and captured unit states.

Run the final automatic gate as two separately sealed phases. Phase A waits for
one naturally occurring, valid-telemetry `higher_priority_busy` or
`higher_priority_degraded` observation; a missing/stale/unavailable signal does
not qualify. N must contain at least one busy/degraded code, contain neither
`higher_priority_unavailable` nor `policy_drift`, and pass every required
telemetry/policy validity check. Before N, require the exact selected model resident and one bound
disposable workload actively inside an uncommitted chunk; record its privacy-
safe workload digest, source cursor, load/unload generations, and absence of a
published output or marker. Bind raw observation N from the owner-only host file
to the candidate's latched transition-observation digest and transition sequence. Require the
atomic candidate status to report controller phase `yielding|recovering` and
normalized reason `priority_pressure` within 15 seconds,
`model_unload_generation=prior+1` and `model_resident=false` within 30 seconds,
and candidate-attributed
GPU memory to reach the exact-image unloaded envelope within 45 seconds. The
original camera/detector/embedding thresholds remain immediate aborts until
both runtime and independent GPU unload proofs pass; only then may an intrinsic
Frigate breach be masked while the candidate remains unloaded. Require load
generation unchanged until PressureController consumes three consecutive,
strictly increasing clear source generations, then require that same already-
bound workload to prove admission, reload/load-generation increment,
retry from its recorded cursor, no partial output/marker, no restart/OOM, and successful
completion. Seal Phase A, then reset every evidence timer, baseline, log cursor,
status-observation cursor, and output assertion; never reset a runtime transition
sequence or model generation. None of Phase A's elapsed time counts toward Phase B.

Phase B begins the 900-second clock only after readiness, a complete valid
baseline, a fresh clear signal, and normal/open/no-recovery status. It performs
complete samples at `t=0,5,...,900`, never catches up with burst samples, and
passes only after the full `t=900` status/telemetry/log sample and durable seal.
The exact candidate must remain running, its only disposable workload active,
and the selected model resident throughout. Every original camera/detector/
embedding threshold is enforced with no masking, and every sampled producer
snapshot must report total detection FPS strictly below 80. Any signal state
other than exact clear (including neutral,
asserted, unavailable, or epoch change), stale heartbeat/source age, blind
status interval, controller transition, model
identity change, restart/OOM, or producer/policy drift aborts the current Phase
B evidence and resets it to a new baseline; it never resumes a partial clock.
An otherwise valid fresh heartbeat may repeat the same clear Frigate source
generation because the gate polls faster than Frigate stats; that duplicate is
allowed for the wall-clock health observation but never advances a distinct-
source counter. A duplicate is invalid only where the protocol specifically
requires a new source generation, such as Phase-A recovery.
Camera low-FPS duration uses `time.monotonic()` and aborts only when strictly
greater than 30 seconds; sample counts never stand in for elapsed time. Every
status response must prove requested `auto`, the actual highest-qualified
selected model, `exact_match` envelope provenance, the final explicit reserve,
configured/fresh priority state, latest and latched-transition observation
digests, transition sequence,
controller phase/reason, distinct-clear count, residency, and monotonic load/
unload generations. A profiler has no status endpoint, so its explicitly
labelled role and intended-command checksum are paired with mandatory external
profiler exit/catalog/result validation; shared-health sampling alone cannot
qualify a model.

The sampler writes three separate create-once, owner-only mode-0600 canonical
ASCII JSON documents (sorted keys, compact separators, no NaN, exactly one
trailing newline, file and parent fsync). Duplicate or extra keys, noncanonical
bytes, a bool where an integer is required, or a missing file is invalid. The
Phase A document has schema `subgen.task11b.phase-a/v1`, `outcome=pass`, and
exactly these remaining keys: `policy_sha256`,
`unloaded_gpu_envelope_sha256`, `workload_sha256`,
`workload_identity`, `candidate_identity_sha256`,
`execution_boundary_manifest_sha256`, `gate_receipt_trace_sha256`, `runtime_epoch`,
`runtime_started_monotonic_ns`, `assertion_reason_codes`,
`assertion_observation_digest`, `assertion_observation_sha256`,
`assertion_observed_monotonic_ns`, `t0_monotonic_ns`, `sealed_monotonic_ns`,
`allowed_unloaded_bytes`, `events`, `final_output_sha256`,
`protected_first_sample_monotonic_ns`, `protected_last_sample_monotonic_ns`,
`protected_sample_count`, `protected_blind_interval_count`,
`protected_threshold_failure_count`, `candidate_restart_delta`,
`candidate_oom_killed`, `cgroup_oom_delta`, `cgroup_oom_kill_delta`,
`cgroup_oom_group_kill_delta`, `runtime_cuda_oom_generation_delta`,
`runtime_media_failure_generation_delta`,
`candidate_cuda_oom_log_match_delta`, and `nvidia_xid_log_match_delta`.
Hashes are lowercase 64-hex; `runtime_epoch` is lowercase
32-hex; runtime-start/assertion/T0/seal/protected times,
allowed bytes, and counters are non-boolean integers in `0..2^63-1`, with all
six times positive and the sample count positive. `candidate_oom_killed` is an
exact JSON boolean and must be false.
Immediately after validating N from the final atomically replaced signal path,
the supervisor copies those exact canonical bytes to a create-once owner-only
mode-0600 assertion file and fsyncs file and parent. Phase A hashes that file as
`assertion_observation_sha256`. The strict verifier receives the file, rechecks
the full signal schema/canonical bytes, recomputes the status observation digest
from its raw observation ID, requires exact policy/reason/pressure equality with
Phase A, and requires `assertion_observed_monotonic_ns` to equal N's exact
`observed_monotonic_ns`. It never prints the raw observation ID or file.
`allowed_unloaded_bytes` must equal the validated canonical unloaded-envelope
file's `measurement.allowed_unloaded_bytes` exactly. The strict verifier parses
that file, revalidates its schema and canonical hash, recomputes its recorded
maximum plus 134217728-byte margin, and rejects a Phase-A value that differs;
the envelope hash and a self-asserted Phase-A integer cannot pass independently.
`assertion_reason_codes` is an ordinally sorted unique array of one or both of
`higher_priority_busy|higher_priority_degraded` and no other value.
`workload_sha256` hashes canonical ASCII JSON with exactly `fixture_sha256`,
`task`, `language`, `cursor_start_ms`, and `total_duration_ms` plus one newline;
the two millisecond fields are non-boolean nonnegative integers and the other
values match the frozen selected-model fixture policy, with
`total_duration_ms > cursor_start_ms`. `workload_identity` is
that exact object, so the verifier canonicalizes it and recomputes the digest.
The candidate and execution-boundary hashes are lowercase 64-hex and must equal
the candidate record, Phase B, and final seal.

`events` contains exactly ten objects in this exact `event_index`/`kind` order:
`0/pre_assertion`, `1/assertion_consumed`, `2/yielded`, `3/unloaded`,
`4/unloaded_gpu`, `5/clear_1`, `6/clear_2`, `7/clear_3`, `8/reloaded`, and
`9/completed`. Every event has exactly these remaining keys:
`monotonic_ns`, `source_generation`, `observation_digest`,
`runtime_epoch`, `runtime_started_monotonic_ns`,
`gate_receipt_sha256`,
`transition_observation_digest`, `transition_sequence`, `heartbeat_age_ms`,
`source_age_ms`, `policy_sha256`, `priority_state`, `controller_phase`,
`recovery_reason`, `admission_open`, `distinct_clear_count`, `model_resident`,
`model_load_generation`, `model_unload_generation`, `cursor_ms`,
`last_completed_cursor_ms`, `completion_generation`, `workload_active`,
`chunk_uncommitted`, `output_count`, `marker_count`,
`output_create_count`, `marker_create_count`, `threshold_masking_allowed`, and
`candidate_bytes`, `model_identity_sha256`, `cuda_oom_generation`, and
`media_failure_generation`. Integers are non-boolean in
`0..2^63-1`; monotonic/source generation are positive; ages are at most
10000/30000; each event runtime epoch/start exactly equals the Phase-A top-level
values, with lowercase-32-hex epoch and positive non-boolean start time;
hashes/policy equal the bound canonical values; admission,
residency, workload, chunk, and masking values are exact JSON booleans;
`cursor_ms` is a non-boolean integer for events 0..8 and null for event 9;
`last_completed_cursor_ms` is null for events 0..8 and a non-boolean integer for
event 9; `completion_generation` is a non-boolean integer in `0..2^63-1`;
states/phases/reasons use the status schema; creation counters are monotonic
supervisor-lifetime counts of successful creations at the watched final-output or
marker paths and cannot decrement after deletion; and event times strictly
increase. `model_identity_sha256` is null exactly when `model_resident=false`
and otherwise is lowercase 64-hex; both failure generations are non-boolean
process-lifetime integers in `0..2^63-1`.

The separate owner-only receipt trace is canonical ASCII JSON with schema
`subgen.task11b.runtime-receipt-trace/v1` and exactly `runtime_epoch`,
`gate_token_sha256`, `workload_sha256`, and `receipts` in addition to `schema`.
`receipts` is a nonempty array of exact validated runtime-receipt objects from
the design in strict `previous+1` sequence and increasing monotonic-time order,
beginning with sequence one's initial gate-setup publication before the bound
workload starts and ending at the event-9 completion publication;
it is reconstructed from the exclusively created append-only journal rather
than an overwrite-prone current-state file. Any partial record, inode
replacement, truncation, overwrite/gap, duplicate sequence, mutation, duplicate
canonical receipt SHA-256, or journal over 8 MiB invalidates the phase. Phase A hashes the complete
trace as `gate_receipt_trace_sha256`; every event's lowercase-64-hex
`gate_receipt_sha256` must hash the unique latest/highest-sequence trace member
whose `observed_monotonic_ns <= event.monotonic_ns`. If a next receipt exists,
the strict verifier also requires
`event.monotonic_ns < next_receipt.observed_monotonic_ns`; an event cannot cite a
stale earlier receipt after runtime state changed. Every receipt's runtime epoch
and token hash equal the Phase-A/boundary values, and the trace's top-level
workload hash equals `workload_sha256`. Before the first Phase-A workload-
admission receipt, every receipt has `workload_sha256=null`, `active=false`,
`chunk_uncommitted=false`, both cursors null, and the initial completion/failure
generations; at least one such receipt exists. The first non-null workload hash
must equal the Phase-A `workload_sha256`, must be the one workload admission,
and every later receipt through event 9 retains that exact hash. Any other
workload hash or a return to null invalidates the trace. For events other than host-owned event 4,
`event.monotonic_ns` exactly equals its referenced receipt's
`observed_monotonic_ns`; event 4 uses the independently defined post-query time
below. Each event's source generation, observation/transition digests,
transition sequence, ages, policy, priority/controller/recovery/admission state,
clear count, residency, load/unload generations, model identity, failure
generations, and workload active/chunk/cursor/completed-cursor/completion-
generation fields exactly equal its referenced atomic receipt, including
`event.workload_active=receipt.active`,
`event.chunk_uncommitted=receipt.chunk_uncommitted`,
`event.cursor_ms=receipt.active_cursor_ms`,
`event.last_completed_cursor_ms=receipt.completed_cursor_ms`, and equal
`completion_generation`. Candidate bytes, artifact counters, and masking state
remain independently host-observed fields. The
trace is mode 0600, create-once, file-and-parent fsynced, never committed, and
never printed.

Event 0 is normal/admission-open/resident inside the uncommitted bound workload,
so `workload_active=true` and `chunk_uncommitted=true`, with point-in-time
output/marker counts zero. Its priority state/count pair is exactly either
`clear/3` or `neutral/0`; no other pair is valid. It occurs strictly before T0; event 1 occurs at or
after T0. More exactly, `events[0].monotonic_ns <
assertion_observed_monotonic_ns <= t0_monotonic_ns <=
events[1].monotonic_ns`, and `runtime_started_monotonic_ns <
events[0].monotonic_ns`. Events 0 and 1 have `cursor_ms` exactly equal to
`workload_identity.cursor_start_ms`. Event 1 is the first durably fsynced
append-journal receipt after the controller consumes N and before it permits the
callback unwind or any later transition. Its `source_generation` exactly equals
N's `source_generation`; both its latest `observation_digest` and latched
`transition_observation_digest` exactly equal
`assertion_observation_digest`; it has asserted priority, incremented transition
sequence, `controller_phase=yielding|recovering`,
`recovery_reason=priority_pressure`, admission closed, and
`chunk_uncommitted=true`. This gate-only publication barrier makes the required
pre-unwind state lossless even when no HTTP poll could observe it. Events 1..4 have
`priority_state=asserted` and `distinct_clear_count=0`; events 2..4 retain N as their
`transition_observation_digest`; events
1 and 2 are no later than T0+15 seconds. Event 1 transition sequence is exactly
event 0 plus one; events 2..4 retain it. Event 3 is nonresident with unload
generation exactly event-0 plus one and no load-generation change by T0+30
seconds. Event 4 is the first qualifying independent host-attribution query:
the frozen supervisor resolves the exact full candidate ID to its current cgroup
PID/descendant set, runs the unloaded-envelope's exact GPU-UUID process-memory
query, parses and sums only that stable set, re-resolves the unchanged set, and
then immediately captures `events[4].monotonic_ns` after validation. Its
`candidate_bytes` is that same query's sum and is at most the bound, with
`t0_monotonic_ns <= events[4].monotonic_ns <= t0_monotonic_ns + 45000000000`;
a stale/pre-unload or differently attributed value cannot be paired with event
4. Missing, ambiguous, changed-PID, wrong-GPU, malformed, or timed-out query
evidence aborts rather than emitting event 4.
Events 0..2 have load and unload generations exactly equal to event 0. Events
3..7 have load generation exactly event 0 and unload generation exactly event 0
plus one. Events 8 and 9 have load and unload generations exactly event 0 plus
one. These equations prohibit any hidden early reload/second-unload cycle and
bind event 8 to the one intended recovery reload. The exact runtime model-
identity digest at events 0..2 is non-null and equals the verifier's
recomputation from the selected candidate, frozen catalog entry, immutable
revision, and matching unloaded-envelope model policy. Events 3..7 are
nonresident and therefore carry null model identity. Events 8 and 9 carry the
same non-null digest as event 0. This proves the one recovery load restored the
fixed highest-qualified model and exact policy rather than a substitute.
`workload_active` remains true through event 8 and is false at completed event 9;
`chunk_uncommitted` is true only for events 0 and 1 and is false after the yield
unwinds it. `threshold_masking_allowed` is false for events 0..3, true for
events 4..7 only after both unload proofs and while nonresident, and false again
for resident events 8 and 9. Output and marker creation counts are exactly zero
at events 0..8, proving no transient creation/deletion; at event 9 output
creation is exactly one and marker creation remains zero.
Point-in-time `output_count` and `marker_count` are both exactly zero for every
event 0..8; event 9 has output count exactly one and marker count exactly zero.
Events 0..8 keep `last_completed_cursor_ms=null` and completion generation
exactly equal to event 0. Events 2..8 also retain that exact active start cursor.
Both failure generations exactly equal event 0 at every event 0..9. Source
generations are nondecreasing across all ten events; events 5..7 additionally
obey the strict clear-generation ordering below.
Events 5..7 have strictly increasing clear source generations beginning
strictly above event 1, distinct-clear counts 1,2,3, remain nonresident, keep load/unload generations exactly at event
4, retain the event-0 cursor, and have no output/marker. Event 5 transition
sequence is event 1 plus one; events 6..9 retain it. Events 5..9 have
`priority_state=clear` and distinct-clear counts exactly `1,2,3,3,3` in order.
The three clear observation
digests are distinct. Event 5's `transition_observation_digest` exactly equals
its own clear-1 `observation_digest`; events 6..9 retain that clear-1 transition
digest. Events 2..6 are recovering with
`recovery_reason=priority_pressure` and admission closed. Event 7 is the atomic
post-third-clear normal state with null recovery reason and admission open, but
remains nonresident. Event 8 occurs only
after event 7, is normal/null-recovery, resident/admission-open with load
generation event-0 plus one, unchanged unload generation, and the same cursor.
Event 9 remains normal/null-recovery/admission-open and retains those
generations, has exactly one final output, zero markers, and a later completed
status with `cursor_ms=null`, `last_completed_cursor_ms` exactly equal to
`workload_identity.total_duration_ms`, and completion generation exactly event
0 plus one.

Before Phase-A workload admission, the frozen supervisor establishes fresh,
independent baselines for all failure sources: exact-ID Docker
`RestartCount`/`State.OOMKilled`; the candidate's exact cgroup-v2
`memory.events` fields `oom`, `oom_kill`, and `oom_group_kill`; both atomic
runtime failure generations; an already-attached, uninterrupted exact-ID
candidate stdout/stderr stream capped at 16 MiB; and a host kernel-journal
cursor for `NVRM:\s*Xid`. The cgroup path is rederived from the exact container
PID and revalidated on every read. The log stream must be attached before
admission and remain continuous through the durable Phase-A seal; EOF, dropped
bytes, overflow, rotation ambiguity, or inability to prove the exact container
source aborts. The kernel journal is read from the saved cursor through the
seal with cursor continuity; unavailable, vacuumed, malformed, or permission-
denied journal evidence aborts. Candidate-log CUDA OOM matching uses only the
design's two exact case-insensitive alternatives.

At seal time, `candidate_restart_delta`, all three cgroup deltas, both runtime
failure-generation deltas, `candidate_cuda_oom_log_match_delta`, and
`nvidia_xid_log_match_delta` are each exactly zero, and
`candidate_oom_killed=false`. Runtime deltas are exactly event 9 minus event 0,
so the event-level equality independently proves that the protected yield did
not consume a CUDA-OOM or media-failure generation. Docker/cgroup/log/journal
values are independently recomputed from their saved baselines through the
durable seal; a generic combined OOM flag cannot substitute for any source.
The verifier tests a mutation of each counter, Docker OOMKilled, each CUDA log
alternative, an Xid line, a missing field, and every blind-source condition.
The release verifier receives the
owner-only disposable SRT path and requires its exact bytes to hash to
`final_output_sha256`; the Phase-A file cannot substitute a self-asserted digest.
`sealed_monotonic_ns` is captured after event 9 and immediately before the
create-once Phase-A write, so it is at least event 9's time.
The protected sampler is already active at event 0. Its first complete valid
sample is no later than T0 and no more than two seconds old at T0; its last is at
or after event 4 and no more than two seconds after event 4. Formally,
`protected_first_sample_monotonic_ns <= t0_monotonic_ns <=
protected_first_sample_monotonic_ns + 2000000000` and
`events[4].monotonic_ns <= protected_last_sample_monotonic_ns <=
events[4].monotonic_ns + 2000000000`. It samples at least
once every two seconds from the first through the last sample. The frozen sampler
increments `protected_blind_interval_count` on any missing, invalid, stale, or
over-two-second interval and `protected_threshold_failure_count` on any original
camera/detector/embedding threshold violation. Both counts must be zero; the
strict verifier rejects any other value. Post-event-4 threshold masking is never
allowed to repair either protected counter.

The Phase B document has schema `subgen.task11b.phase-b/v1`, `outcome=pass`, and
exactly these remaining keys: `started_monotonic_ns`, `ended_monotonic_ns`,
`phase_a_seal_sha256`, `phase_a_durable_monotonic_ns`,
`reset_completed_monotonic_ns`, `runtime_epoch`, `runtime_started_monotonic_ns`,
`sample_interval_seconds`, `policy_sha256`, `producer_epoch_digest`,
`producer_epoch`, `candidate_identity_sha256`, `candidate_identity`,
`execution_boundary_manifest_sha256`, `workload_sha256`, `workload_identity`,
`gate_receipt_trace_sha256`, `model_identity_sha256`, and `samples`. The eight hashes are lowercase
64-hex; runtime/producer epochs are lowercase 32-hex; times/interval are exact
non-boolean integers, all five top-level times
are positive, and interval equals five.
`phase_a_seal_sha256` hashes the exact validated Phase-A file. The frozen
supervisor records `phase_a_durable_monotonic_ns` only after that file and its
parent directory have been fsynced, resets every Phase-B evidence timer,
baseline, log/status/output cursor, and assertion state, then records
`reset_completed_monotonic_ns`. The strict verifier requires
`phase_a.events[9].monotonic_ns <= phase_a.sealed_monotonic_ns <=
phase_a_durable_monotonic_ns <= reset_completed_monotonic_ns <
started_monotonic_ns`; an older or overlapping Phase B cannot be paired with a
later Phase A.
After Phase A is durable and before recording `reset_completed_monotonic_ns`,
the supervisor records new evidence baselines—not resets—for exact-ID Docker
`RestartCount`/`State.OOMKilled`, the same exact candidate cgroup's three
`memory.events` OOM fields, both process-lifetime runtime failure generations,
the uninterrupted candidate-log byte/match cursor, the Frigate restart count,
and a new continuous kernel-journal Xid cursor. The Phase-A log attachment stays
open without a gap through the Phase-B durable seal. All Phase-A source-
identity, 16-MiB cap, exact match, cursor-continuity, cgroup rederivation, and
blind-source abort rules continue to apply. A source cannot be reset, reopened
past unread bytes, or replaced merely because the evidence clock was reset.
Phase-B runtime epoch/start exactly equal Phase A. Sample 0's transition
observation digest, transition sequence, model load/unload generations,
completion generation, both failure generations, and model-identity digest
exactly equal Phase-A event 9; evidence-only reset cannot reset or reconstruct
any runtime counter.
`producer_epoch` is the exact 32-lowercase-hex value and
`producer_epoch_digest` hashes its ASCII bytes with no newline.
`candidate_identity` contains exactly `container_id`, `runtime_commit`,
`oci_index`, `config_digest`, `layer_diff_ids`, `selected_model`, and
`model_revision`: respectively the lowercase full container ID, runtime commit,
OCI index, config digest, ordered diff-ID array, selected model, and immutable model revision.
`candidate_identity_sha256` hashes its canonical ASCII JSON plus one newline;
those values must match the final gate identity. The execution-boundary hash
must equal Phase A, the create-once candidate record, and the final gate seal.
`workload_identity` contains exactly the same five typed keys and canonical
serialization rules as Phase A's object, but identifies a separate prevalidated
Phase-B fixture, its `workload_sha256` differs from Phase A, and both its fixture
record and expected workload hash are bound into the schema-3 boundary. The
fixture must remain active beyond the complete t=900 sample; early completion
invalidates the attempt and requires a newly prevalidated longer disposable
fixture and a regenerated boundary rather than reusing the evidence.
`model_identity_sha256` is the exact non-null digest independently recomputed
from the selected candidate, immutable revision, frozen catalog entry, and
matching unloaded-envelope policy; it equals Phase-A event 9.

The separate Phase-B owner-only trace has schema
`subgen.task11b.phase-b-runtime-receipt-trace/v1` and exactly
`runtime_epoch`, `gate_token_sha256`, `phase_a_trace_sha256`,
`phase_a_last_sequence`, `workload_sha256`, and `receipts` in addition to
`schema`. Its hash is `gate_receipt_trace_sha256`.
`phase_a_trace_sha256` equals Phase A's `gate_receipt_trace_sha256`, and
`phase_a_last_sequence` equals that validated trace's final receipt sequence.
The nonempty receipts array contains every append-journal record beginning at
exactly `phase_a_last_sequence+1` and continuing without a gap through the first
complete receipt strictly after `ended_monotonic_ns`. Records before Phase-B
admission retain the Phase-A hash, are inactive with both workload cursors in
their exact completed/idle forms (`active_cursor_ms=null` and
`completed_cursor_ms=phase_a.workload_identity.total_duration_ms`), and keep
Phase-A event-9 controller/model/failure/
completion values. Exactly one admission receipt then changes the workload hash
to the Phase-B value with `active=true`; it occurs after reset and no later than
`started_monotonic_ns`. Every receipt from that admission through the last one
at or before `ended_monotonic_ns` retains that hash, `active=true`, exact clear/
normal/open priority state, the top-level model identity, stable load/unload/
completion/failure generations, and no foreign workload transition. The first
post-end sentinel proves the journal prefix was not truncated; its post-end
workload state does not extend the acceptance interval. The trace is canonical,
create-once mode 0600, file-and-parent fsynced, never committed or printed, and
the verifier rejects any missing, duplicate, reordered, mutated, partial, or
noncanonical journal record.
`samples` contains exactly 181 objects. Every object has exactly
`sample_index`, `scheduled_offset_seconds`, `captured_monotonic_ns`,
`source_generation`, `policy_sha256`, `producer_epoch`, `runtime_epoch`,
`runtime_started_monotonic_ns`,
`candidate_identity_sha256`, `gate_receipt_sha256`, `model_identity_sha256`,
`observation_digest`,
`transition_observation_digest`, `transition_sequence`, `heartbeat_age_ms`,
`source_age_ms`, `priority_state`, `controller_phase`, `recovery_reason`,
`admission_open`, `candidate_running`, `workload_active`,
`distinct_clear_count`, `model_resident`,
`model_load_generation`, `model_unload_generation`, `completion_generation`,
`cuda_oom_generation`, `media_failure_generation`,
`detection_fps`,
`camera_min_process_ratio`, `camera_max_skipped_fps`,
`camera_low_ratio_elapsed_ms`, `detector_count`, `detector_stalled_count`,
`embedding_metric_count`, `embedding_invalid_count`, `candidate_oom_killed`,
`cgroup_oom_delta`, `cgroup_oom_kill_delta`,
`cgroup_oom_group_kill_delta`, `runtime_cuda_oom_generation_delta`,
`runtime_media_failure_generation_delta`,
`candidate_cuda_oom_log_match_delta`, `nvidia_xid_log_match_delta`,
`candidate_restart_delta`,
`frigate_restart_delta`, and `ollama_loaded`.

Indices are exactly 0..180 and each offset is `index*5`. Captures are ordered,
and for every sample `scheduled_ns = started_monotonic_ns +
scheduled_offset_seconds * 1000000000`, with `scheduled_ns <=
captured_monotonic_ns <= scheduled_ns + 2000000000`. Captures therefore occur
from their exact scheduled monotonic instant through at most two seconds late,
are separated from the previous capture by at least three seconds, and are never
burst/catch-up samples. The final capture is offset 900 and end is
not before it: `ended_monotonic_ns >= samples[180].captured_monotonic_ns`, so
end-start is at least 900,000,000,000 ns.
`started_monotonic_ns`, `ended_monotonic_ns`, `sample_index`,
`scheduled_offset_seconds`, `captured_monotonic_ns`, `source_generation`,
`runtime_started_monotonic_ns`, `transition_sequence`, `heartbeat_age_ms`, `source_age_ms`,
`distinct_clear_count`, `model_load_generation`, `model_unload_generation`,
`completion_generation`, `cuda_oom_generation`, `media_failure_generation`,
`camera_low_ratio_elapsed_ms`, `detector_count`, `detector_stalled_count`,
`embedding_metric_count`, `embedding_invalid_count`,
`cgroup_oom_delta`, `cgroup_oom_kill_delta`,
`cgroup_oom_group_kill_delta`, `runtime_cuda_oom_generation_delta`,
`runtime_media_failure_generation_delta`,
`candidate_cuda_oom_log_match_delta`, `nvidia_xid_log_match_delta`,
`candidate_restart_delta`, and
`frigate_restart_delta` are JSON integers, never booleans, in `0..2^63-1`;
started/ended/captured times and source generations are positive.
The per-sample policy, producer epoch, candidate identity, runtime epoch, and
runtime-start fields exactly equal their top-level values; policy/candidate/
digest values, `gate_receipt_sha256`, and `model_identity_sha256` are lowercase
64-hex and both epochs are lowercase 32-hex. `candidate_oom_killed` is an exact
JSON boolean. Source generations are nondecreasing. Every observation and
latched transition digest is lowercase 64-hex. The transition digest and
transition sequence exactly equal sample 0 for all 181 samples.

Every sample's `gate_receipt_sha256` hashes the unique latest/highest-sequence
Phase-B trace receipt whose `observed_monotonic_ns <=
captured_monotonic_ns`; when a next receipt exists, capture time is strictly
before it. The sample's source generation, observation/transition digests,
transition sequence, ages, policy, priority/controller/recovery/admission state,
clear count, residency, load/unload generations, model identity, failure
generations, workload-active value, and completion generation exactly equal
that one atomic receipt. The strict verifier also validates every intervening
trace receipt, not only the 181 cited samples, so a workload failure,
cancellation, replacement, pressure transition, unload/reload, or failure-
generation increment that reverses before the next five-second capture remains
fatal.

Every sample carries fresh ages within 10000/30000 ms and exact
`clear`/`normal`/null-recovery/admission-open status, with
`candidate_running=true`, `workload_active=true`, distinct-clear count three,
and `model_resident=true`. Candidate running is verified against the exact full
container ID, running state, PID, and expected cgroup; workload active is the
atomic runtime status for the one boundary-bound Phase-B fixture and the private
trace proves its exact digest continuously. Load/unload generations,
`completion_generation`, both failure generations, and model identity remain
equal to sample 0 for all 181 samples. Every sample has
`candidate_oom_killed=false`; all three cgroup OOM deltas, both runtime failure-
generation deltas, both candidate-log/Xid match deltas, candidate restart delta,
and Frigate restart delta are exactly zero. Each delta is cumulative from the
post-Phase-A baseline and is recomputed from its separately named source; no
combined `xid_or_oom` field or log-only proxy is accepted. Any completion,
failure, cancellation, replacement workload, source blindness, OOM/Xid match,
restart, or model/controller transition during the interval invalidates the
phase. The three FPS/ratio fields are finite
non-boolean JSON numbers in `0..1000000`; detection FPS is strictly below 80.
Because the same normalized producer snapshot is sealed as clear, maximum
skipped FPS must equal exactly zero and minimum process ratio must be at least
0.98 in every sealed sample. The original looser health aborts (skipped FPS over
0.5, or a process ratio below 0.9 for more than 30000 monotonic milliseconds)
remain enforced continuously but can never relax those stricter clear-sample
equations. Detector and
embedding counts are positive while stalled/invalid counts are zero.
`ollama_loaded` is false. Any missing
sample, non-clear state, blind interval, epoch/policy/identity/controller/model-
generation change, original threshold breach, or final-sample failure prevents
the Phase-B file from existing. Focused verifier tests independently mutate
each of the three cgroup fields, Docker OOMKilled, both runtime failure
generations, each candidate-log CUDA alternative, the Xid journal line, each
restart delta, the workload hash/sequence, and every blind-source path; no one
source can stand in for another.

Only after both documents validate and the exact candidate is stopped with no
remaining candidate PID may the sampler write the final seal. It has schema
`subgen.task11b.shared-gpu-gate/v2`, `outcome=pass`, and exactly these remaining
top-level keys: `runtime_commit`, `candidate_oci_index`,
`candidate_config_digest`, `container_id_sha256`,
`candidate_identity_record_sha256`, `layer_diff_ids_sha256`, `sampler_sha256`,
`sampler_test_sha256`, `observer_sha256`, `observer_test_sha256`,
`producer_sha256`, `policy_sha256`, `unloaded_gpu_envelope_sha256`,
`execution_boundary_manifest_sha256`, `phase_a_seal_sha256`,
`phase_b_seal_sha256`, and `cleanup`. Runtime commit is
lowercase 40-hex; OCI/config values are `sha256:` plus lowercase 64-hex; all
other hashes are lowercase 64-hex. `layer_diff_ids_sha256` hashes the canonical
ASCII JSON array of ordered diff-ID strings plus one newline. `cleanup` contains
exactly `verified_stopped=true`, `candidate_pid_count=0`, and
`execution_boundary_revalidated=true`. The latter may be true only after the
frozen sampler regenerated canonical schema-3 bytes from the stopped exact-ID
candidate and proved byte equality with the pre-start manifest. The two phase hashes
are of the exact subordinate files, so replacing either phase cannot preserve a
valid final seal.

Before first start, the supervisor writes one create-once canonical owner-only
candidate record with schema `subgen.task11b.candidate-identity/v1` and exactly
`candidate_identity`, `execution_boundary_manifest_sha256`,
`gate_token_sha256`, `intended_command_sha256`, and `created_stopped=true` in
addition to schema. Its candidate object is the exact Phase-B object; all three
hash values are lowercase 64-hex. `intended_command_sha256` must equal the full
command digest carried inside the validated schema-3 manifest rather than an
independent caller assertion. `gate_token_sha256` is SHA-256 of the exact
32-lowercase-hex token's ASCII bytes with no newline. The strict verifier reads
that raw token only from the manifest's exact
`io.github.herbertmt978.subgen.gate-token` label, validates its format,
recomputes the digest, and requires equality with the candidate record; it never
prints or commits the raw token. The final seal's
record hash is of this exact file and `container_id_sha256` is SHA-256 of the
canonical lowercase full-ID ASCII bytes with no newline. The release verifier
receives the record path, rehashes it, recomputes both identity digests, and
matches all runtime/image/model values and the execution-boundary hash rather
than trusting the Phase-B copy.

The amended `runtime_gate_observer.py` must provide a `verify-release` command
that reads all three seals, the committed binding line, the private policy, the
unloaded envelope, preserved canonical assertion observation N, owner-only
Phase-A and Phase-B runtime-receipt traces, canonical
schema-3 execution-boundary manifest, and
`monitor_frigate_priority.py`; rejects duplicate/extra
keys and noncanonical bytes before parsing; enforces every exact type, range,
equation, deadline, hash, and identity above; reconstructs and validates the
manifest's full security-boundary preimage rather than accepting only its opaque
command/token hashes; cross-binds its full container ID and runtime/image/model
identity to the candidate record, both phases, and final seal; and exits nonzero without printing
private policy contents on any mismatch. Task 11A tests malformed, duplicate-
key, old-schema, coerced-type, missing-phase, hash-swap, and deadline bypasses.
Publication invokes this frozen observer command successfully before it trusts
any PowerShell object projection or performs a remote mutation.

**Expected:** exact candidate OCI identity, valid owner-only identity and
catalog artifacts with read-only runtime mounts, immediate pre-start host
identity verification, selected model and strict envelope, explicit reserve
evidence, isolated 12 GiB profiling evidence, independent 10 GiB automatic
qualification, proof that the profiling cap cannot authorize selection,
15-minute health evidence, verified legacy-unit isolation, and an
evidence-backed v0.3.0 marker-compatibility result.

### Task 11C: Finalize governance after the Frigate gate

**Files:** ADR, baseline/work verification, index, and plan status only unless
the candidate gate disproves a source contract and returns execution to its
owning task.

**Steps:** `RUNTIME_COMMIT` is exactly the fresh post-amendment
`TASK11A_RUNTIME_COMMIT` sealed before Task 11B, not the retired `4418b3c9`
revision. It is the source revision recorded in the candidate's OCI revision
label and bound to its verified OCI index, inner configuration digest, and
ordered rootfs diff IDs. Never recompute it from a later `HEAD`.
Record the privacy-safe candidate evidence, accept ADR 0002 only after every
gate passes, rerun the structural checker and `git diff --check`, and commit
the completed evidence as `Record v0.5.0 verification evidence`.

Before that commit, add exactly one single-line, canonical JSON record to
`90-evidence.md`, prefixed by `Task-11B-Sampler-Binding: `. The object contains
exactly fifteen keys: `schema` with value
`subgen.task11b.sampler-binding/v1`, plus `sampler_commit`, `sampler_blob`,
`sampler_sha256`, `test_blob`, `test_sha256`, `observer_blob`,
`observer_sha256`, `observer_test_blob`, `observer_test_sha256`, and
`gate_seal_sha256`, `producer_sha256`, `policy_sha256`, and
`unloaded_gpu_envelope_sha256`, and `execution_boundary_manifest_sha256`.
Derive all four Git blob IDs and file hashes from
`SAMPLER_COMMIT`; derive the sampler, observer, and seal hashes from the
owner-only final 10 GiB automatic-runtime pass seal actually returned by Task
11B; derive the producer hash from the exact `RUNTIME_COMMIT` blob and live
transferred bytes; derive the policy/envelope hashes from their canonical
owner-only files and the final seal; and compare both transferred test hashes with their binding values before
recording them. Derive the boundary hash from the exact canonical schema-3 file
and require equality with both phases, candidate record, and final seal. Record
the large-v3 safe-failure and lower-model profiler pass seals separately
in the privacy-safe Task 11B evidence so the final automatic-runtime seal cannot
be mistaken for the complete profiling chain. All hashes are
lowercase, the Git object IDs are the repository's full object IDs, and the
record contains no host path. This committed record, rather than a caller-set
environment variable, is the release-side source of truth for sampler identity.

`RELEASE_COMMIT` is the resulting lowercase full 40-character commit. Require
`RUNTIME_COMMIT`, `SAMPLER_COMMIT`, and `RELEASE_COMMIT` to be three distinct
commits in strict runtime -> sampler -> release order. Require the sampler,
sampler-test, runtime-observer, and observer-test blobs at `RELEASE_COMMIT` to
equal the recorded blobs at `SAMPLER_COMMIT`, and require all four exact SHA-256
values to equal the committed binding and the Task 11B seal/transfer evidence. Product/runtime source, existing tests,
release notes, packaging, workflows, Dockerfile, `.dockerignore`, and every
image/runtime input must remain byte-for-byte those at `RUNTIME_COMMIT`. The
sampler, runtime observer, and their tests are the sole permitted executable
post-runtime delta.

Before publication, use `git diff --name-status --no-renames` and require exact
equality with this status/path manifest; a directory wildcard is insufficient:

```text
M	docs/aegis/INDEX.md
M	docs/aegis/adr/0002-memory-aware-segmented-transcription.md
A	docs/aegis/baseline/2026-09-01-v0.5.0-release-baseline.md
M	docs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/20-checkpoint.md
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/90-evidence.md
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/drift-check-draft.json
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/gate_health_sampler.py
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/resume-state-hint.json
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/runtime_gate_observer.py
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_gate_health_sampler.py
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_runtime_gate_observer.py
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/todo-checkpoint-draft.json
```

Keep `99-reflection.md` incomplete and outside the tagged release commit until
post-release closeout. Recheck the unchanged candidate's OCI index, inner config
digest, ordered diff IDs, and revision label against Task 11B immediately before
any remote mutation. A sampler change restarts Task 11B. A runtime/build-input
change, image rebuild or relabel, index/config/diff-ID change, or revision-label
change returns to the package, profiler, and Frigate identity-bound gates.

**Expected:** the exact publishable runtime digest, its immutable source
revision, the governance-only release revision, and all release gates are
complete without claiming that an evidence-only commit rebuilt the image.

### Task 12: Publish v0.5.0 without hosted runners

**Systems:** GitHub repository/release and GHCR.

**Steps:** prove workflows remain manual-only and snapshot run history. Resolve
all three commit identities as lowercase full SHAs; prove runtime -> sampler ->
release ancestry, the exact status/path manifest, a clean worktree, and zero
remote-side divergence. Before any remote mutation, require an existing GitHub
release to be absent by an authoritative HTTP 404 or already exact in tag,
title, draft/prerelease state, and canonical body derived from the immutable
`RELEASE_COMMIT` blob; an authentication, transport, or other HTTP failure is
not absence. Recheck the idle simulator's candidate label, Docker
Desktop/containerd OCI index, inner config digest, and ordered diff IDs against
Task 11B. Treat a prior interrupted publication as resumable only when every existing
local/remote Git ref, GitHub release, and GHCR tag is absent or resolves exactly
to the recorded commit, release body, or digest; any mismatched partial state
blocks without overwriting it. Only
then fast-forward verified history to `main`, create or accept the exact
annotated tag at `RELEASE_COMMIT`, and prove both remote refs. On the idle
simulator, securely push the already verified image as `v0.5.0` and `latest`
using private task-scoped Docker configurations with ordinary and restart-safe
identity-bound cleanup;
require the same manifest digest and a clean anonymous pull-smoke on a distinct
empty local Docker Engine. Capture and prove the prior `latest` digest before
changing it. On a mutation failure, accept only four stable observations of
the recorded prior or expected final digest; never issue a compensating
mutable-tag push.
Create or accept the exact release after the version smoke, then mutate `latest`
as the final public write.
The Docker Desktop local candidate `.Id` must equal Task 11B's OCI index. The
anonymous classic Docker Engine pull resolves that published index to the exact
inner OCI config digest; both stores must expose the same ordered layer diff IDs
and OCI revision label. The label must equal `RUNTIME_COMMIT`; main and the
annotated tag must equal `RELEASE_COMMIT`.
Publication blocks unless the runtime-to-release delta exactly equals Task
11C's status/path manifest.
Registry manifest equality between version/latest is an additional tag check,
not the candidate identity test.

#### Normative publication amendment (supersedes older Task-12 mutation sketches)

This amendment is fail-closed and is the only active Task-12 publication
contract. Any later command fragment that does not call the exact primitives
defined here is explanatory recovery history, not an executable release step.
In particular, a normal `docker push`/`buildx imagetools create --tag v0.5.0`,
an unbounded or recent-only Actions query, `gh release view` as an absence
test, newline normalization, a rebaseline, or a repository lock acquired only
for `latest` is forbidden. Remove or replace such a fragment before running
Task 12; its mere presence in an older recovery sketch does not authorize it.

The release publisher must satisfy this single ordered state machine:

1. Materialize the committed evidence, observer, sampler, priority producer,
   and release notes from their exact Git blobs into an owner-only directory.
   Verify object IDs and SHA-256 first, then run only the materialized observer
   with isolated Python (`python -I`). Working-tree source is never executable
   release input.
2. Before the first public write, fetch the complete paginated Actions run-ID
   set twice, require identical snapshots, and durably bind its canonical hash
   and exact IDs to the owner-only publication intent. No step may replace that
   baseline. After *every* public write, including lock-object/ref creation,
   main, annotated version Git tag, registry tag, release, `latest`, and exact
   lock removal, refetch all pages and require the exact same ID set and hash.
   A write whose response is lost still executes this assertion in `finally`.
3. Acquire one fixed, atomic repository publication lock before changing
   `main`, the Git version tag, or either registry tag. The lock object binds
   `RELEASE_COMMIT`, candidate OCI digest, release-body SHA-256, hosted-run
   baseline-file SHA-256, and a unique persisted run token. Hold the same lock
   through version smoke, release creation/verification, `latest`, and cleanup.
   Any pre-existing, replaced, or missing lock is a recovery blocker, never a
   reason to start a second lock or continue unlocked.
4. Before touching `v0.5.0`, prove create-if-absent behavior against a unique
   disposable GHCR probe tag. Race two different harmless manifests using the
   registry HTTP conditional-create request (`If-None-Match: *`): exactly one
   request must create the tag, exactly one must receive the registry's
   documented precondition failure, and the final digest must be the winner.
   Repeat with independently named probe tags, bind requests/responses/digests
   to the intent, and remove only the exact probe versions after revalidating
   their IDs and exclusive tags. If GHCR does not demonstrate that atomic
   behavior, if cleanup is ambiguous, or if the client cannot preserve the
   conditional header end to end, publication is blocked. A cooperating Git
   lock is not a substitute for registry atomicity.
5. Publish `v0.5.0` only by the proven conditional create-if-absent path using
   the exact sealed manifest bytes. A precondition failure is resumable only
   when an authoritative registry read returns the expected OCI digest; a
   foreign digest is never overwritten. A normal Docker tag push is forbidden.
6. Query the release-tag REST endpoint with status and stderr captured
   separately. Absence means exactly HTTP 404. HTTP 200 is accepted only when
   tag, title, draft/prerelease flags, and the decoded `body` encoded as strict
   BOM-less UTF-8 are byte-for-byte equal to the exact release-notes blob.
   Authentication, transport, rate-limit, 3xx, and 5xx outcomes block. Query
   again immediately before create; create only after a fresh 404; then require
   a fresh exact HTTP 200. Never trim or normalize CRLF or final newlines.
7. Update mutable `latest` only after the immutable version smoke and exact
   release. It remains protected by the same broad lock and immutable intent;
   observe the prior digest, write once, and accept only the prior or expected
   digest during recovery. Finally remove the exact lock and prove the hosted
   run baseline one last time. Preserve only the non-secret intent and evidence
   on ambiguity so recovery can fail closed. Registry tokens and task-scoped
   credential material are always logged out and removed in guaranteed cleanup;
   recovery reacquires authorization through the approved credential path.

The local publisher and its failure-injection tests must prove each transition,
including response loss, concurrent foreign writes, pagination over more than
100 runs, a run created or deleted after baseline, release 401/403/404/409/5xx,
CRLF/final-newline differences, unsupported conditional requests, two-winner
registry races, and lock replacement. Until those tests and the disposable
registry capability probe pass, Task 12 has no authorized public-mutation path.

**Current execution status:** Task 12 is deliberately blocked while Task 11
produces the final immutable candidate identity. The command fragments below are
fail-closed implementation scaffolding and recovery requirements, not a runnable
publisher: the broad-lock and registry-create primitives are intentionally
absent. Task 12 must replace them with one locally tested, owner-only publisher
and recovery script before any GitHub ref, release, or GHCR mutation. This block
does not prevent Task 11 source implementation, local/simulator verification, or
candidate gating; it prevents publication only.

```powershell
$ErrorActionPreference = 'Stop'
$RuntimeCommit = [string]$env:SUBGEN_TASK11A_RUNTIME_COMMIT
$ExpectedConfig = [string]$env:SUBGEN_TASK11A_CONFIG_DIGEST
$ExpectedIndex = [string]$env:SUBGEN_TASK11A_OCI_INDEX
$EvidencePath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/90-evidence.md'
$SamplerPath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/gate_health_sampler.py'
$SamplerTestPath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_gate_health_sampler.py'
$ObserverPath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/runtime_gate_observer.py'
$ObserverTestPath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_runtime_gate_observer.py'
$ProducerPath = 'monitor_frigate_priority.py'
$ReleaseNotesPath = 'docs/RELEASE_NOTES_0.5.0.md'
$GateSealPath = $env:SUBGEN_TASK11B_GATE_SEAL
$PhaseASealPath = $env:SUBGEN_TASK11B_PHASE_A_SEAL
$PhaseBSealPath = $env:SUBGEN_TASK11B_PHASE_B_SEAL
$PhaseAOutputPath = $env:SUBGEN_TASK11B_PHASE_A_OUTPUT
$AssertionObservationPath = $env:SUBGEN_TASK11B_ASSERTION_OBSERVATION
$PhaseAReceiptTracePath = $env:SUBGEN_TASK11B_PHASE_A_RECEIPT_TRACE
$PhaseBReceiptTracePath = $env:SUBGEN_TASK11B_PHASE_B_RECEIPT_TRACE
$CandidateIdentityPath = $env:SUBGEN_TASK11B_CANDIDATE_IDENTITY
$ExecutionBoundaryManifestPath = $env:SUBGEN_TASK11B_EXECUTION_BOUNDARY_MANIFEST
$PriorityPolicyPath = $env:SUBGEN_TASK11A_PRIORITY_POLICY_FILE
$UnloadedEnvelopePath = $env:SUBGEN_TASK11B_UNLOADED_GPU_ENVELOPE
$releaseLines = @(& git rev-parse --verify 'HEAD^{commit}')
if ($LASTEXITCODE -ne 0 -or $releaseLines.Count -ne 1) {
  throw 'Unable to resolve RELEASE_COMMIT'
}
$ReleaseCommit = $releaseLines[0].Trim()
if ($RuntimeCommit -cnotmatch '^[0-9a-f]{40}$' -or
    $ReleaseCommit -cnotmatch '^[0-9a-f]{40}$') {
  throw 'Runtime and release identities must be lowercase full SHAs'
}
if ($RuntimeCommit -ceq '4418b3c97296a04b311d29d9ce52abefef64e108' -or
    $ExpectedConfig -ceq 'sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae' -or
    $ExpectedIndex -ceq 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc') {
  throw 'Pre-amendment candidate identity is retired'
}
$evidenceLines = @(& git show "$ReleaseCommit`:$EvidencePath")
if ($LASTEXITCODE -ne 0) { throw 'Unable to read committed Task 11B evidence' }
$bindingPrefix = 'Task-11B-Sampler-Binding: '
$bindingLines = @($evidenceLines | Where-Object { $_.StartsWith($bindingPrefix, [StringComparison]::Ordinal) })
if ($bindingLines.Count -ne 1) { throw 'Expected exactly one committed sampler binding' }
foreach ($Path in @($GateSealPath,$PhaseASealPath,$PhaseBSealPath,$PhaseAOutputPath,
    $AssertionObservationPath,$PhaseAReceiptTracePath,$PhaseBReceiptTracePath,
    $CandidateIdentityPath,
    $ExecutionBoundaryManifestPath,
    $PriorityPolicyPath,$UnloadedEnvelopePath)) {
  if ([string]::IsNullOrWhiteSpace($Path) -or
      -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw 'A required owner evidence file is missing'
  }
}
$binding = $bindingLines[0].Substring($bindingPrefix.Length) | ConvertFrom-Json
$expectedBindingKeys = @('gate_seal_sha256','observer_blob','observer_sha256',
  'observer_test_blob','observer_test_sha256','sampler_blob','sampler_commit',
  'sampler_sha256','schema','test_blob','test_sha256','producer_sha256',
  'policy_sha256','unloaded_gpu_envelope_sha256',
  'execution_boundary_manifest_sha256')
$actualBindingKeys = @($binding.PSObject.Properties.Name | Sort-Object)
if (@(Compare-Object -CaseSensitive ($expectedBindingKeys | Sort-Object) $actualBindingKeys).Count -ne 0 -or
    $binding.schema -cne 'subgen.task11b.sampler-binding/v1') {
  throw 'Sampler binding schema mismatch'
}
$SamplerCommit = [string]$binding.sampler_commit
foreach ($Value in @($binding.sampler_blob,$binding.test_blob,
    $binding.observer_blob,$binding.observer_test_blob)) {
  if ([string]$Value -cnotmatch '^[0-9a-f]{40}$') { throw 'Sampler Git blob identity is invalid' }
}
foreach ($Value in @($binding.sampler_sha256,$binding.test_sha256,
    $binding.observer_sha256,$binding.observer_test_sha256,
    $binding.gate_seal_sha256,$binding.producer_sha256,$binding.policy_sha256,
    $binding.unloaded_gpu_envelope_sha256,
    $binding.execution_boundary_manifest_sha256)) {
  if ([string]$Value -cnotmatch '^[0-9a-f]{64}$') { throw 'Sampler SHA-256 identity is invalid' }
}
if ($SamplerCommit -cnotmatch '^[0-9a-f]{40}$' -or
    $SamplerCommit -ceq $RuntimeCommit -or $SamplerCommit -ceq $ReleaseCommit -or
    $RuntimeCommit -ceq $ReleaseCommit) {
  throw 'Runtime, sampler, and release commits must be distinct full SHAs'
}
foreach ($Commit in @($RuntimeCommit, $SamplerCommit, $ReleaseCommit)) {
  $resolved = @(& git rev-parse --verify "$Commit^{commit}")
  if ($LASTEXITCODE -ne 0 -or $resolved.Count -ne 1 -or
      $resolved[0].Trim() -cne $Commit) {
    throw 'Commit identity did not resolve exactly'
  }
}
& git merge-base --is-ancestor $RuntimeCommit $SamplerCommit
if ($LASTEXITCODE -ne 0) { throw 'Runtime is not an ancestor of sampler' }
& git merge-base --is-ancestor $SamplerCommit $ReleaseCommit
if ($LASTEXITCODE -ne 0) { throw 'Sampler is not an ancestor of release' }

function Resolve-ExactBlob([string]$Commit, [string]$Path) {
  $lines = @(& git rev-parse --verify "$Commit`:$Path")
  if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1 -or
      $lines[0].Trim() -cnotmatch '^[0-9a-f]{40}$') {
    throw "Unable to resolve exact Git blob: $Path"
  }
  return $lines[0].Trim()
}
$samplerBlob = Resolve-ExactBlob $SamplerCommit $SamplerPath
$testBlob = Resolve-ExactBlob $SamplerCommit $SamplerTestPath
$observerBlob = Resolve-ExactBlob $SamplerCommit $ObserverPath
$observerTestBlob = Resolve-ExactBlob $SamplerCommit $ObserverTestPath
$releaseSamplerBlob = Resolve-ExactBlob $ReleaseCommit $SamplerPath
$releaseTestBlob = Resolve-ExactBlob $ReleaseCommit $SamplerTestPath
$releaseObserverBlob = Resolve-ExactBlob $ReleaseCommit $ObserverPath
$releaseObserverTestBlob = Resolve-ExactBlob $ReleaseCommit $ObserverTestPath
$evidenceBlob = Resolve-ExactBlob $ReleaseCommit $EvidencePath
$runtimeProducerBlob = Resolve-ExactBlob $RuntimeCommit $ProducerPath
$releaseProducerBlob = Resolve-ExactBlob $ReleaseCommit $ProducerPath
if ($samplerBlob -cne $binding.sampler_blob -or
    $testBlob -cne $binding.test_blob -or
    $observerBlob -cne $binding.observer_blob -or
    $observerTestBlob -cne $binding.observer_test_blob -or
    $releaseSamplerBlob -cne $binding.sampler_blob -or
    $releaseTestBlob -cne $binding.test_blob -or
    $releaseObserverBlob -cne $binding.observer_blob -or
    $releaseObserverTestBlob -cne $binding.observer_test_blob -or
    $runtimeProducerBlob -cne $releaseProducerBlob) {
  throw 'Sampler/observer/test Git blob binding mismatch'
}
$blobHashes = @(& python -I -c 'import hashlib,json,subprocess,sys; print(json.dumps([hashlib.sha256(subprocess.check_output(["git","cat-file","blob",oid])).hexdigest() for oid in sys.argv[1:]],separators=(",",":")))' $binding.sampler_blob $binding.test_blob $binding.observer_blob $binding.observer_test_blob $evidenceBlob $runtimeProducerBlob)
if ($LASTEXITCODE -ne 0 -or $blobHashes.Count -ne 1) { throw 'Unable to hash sampler Git blobs' }
$blobHashValues = @($blobHashes[0] | ConvertFrom-Json)
if ($blobHashValues.Count -ne 6 -or
    $blobHashValues[0] -cne $binding.sampler_sha256 -or
    $blobHashValues[1] -cne $binding.test_sha256 -or
    $blobHashValues[2] -cne $binding.observer_sha256 -or
    $blobHashValues[3] -cne $binding.observer_test_sha256 -or
    $blobHashValues[5] -cne $binding.producer_sha256) {
  throw 'Sampler/observer/test SHA-256 binding mismatch'
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $PriorityPolicyPath).Hash.ToLower() -cne
      $binding.policy_sha256 -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $UnloadedEnvelopePath).Hash.ToLower() -cne
      $binding.unloaded_gpu_envelope_sha256) {
  throw 'Producer/policy/unloaded-envelope binding mismatch'
}

# The verifier is executable release logic. Never execute its working-tree
# pathname, and never give it working-tree evidence or producer source. Export
# only blobs whose Git object IDs and SHA-256 values were proved above, protect
# the containing directory, rehash every exported file, and run isolated Python.
$verifierTempParent = [IO.Path]::GetFullPath($env:TEMP)
$VerifierRoot = [IO.Path]::GetFullPath((Join-Path $verifierTempParent `
  ('subgen-v050-release-verifier-' + [Guid]::NewGuid().ToString('N'))))
$verifierPrefix = $verifierTempParent.TrimEnd('\') + '\'
if (-not $VerifierRoot.StartsWith($verifierPrefix,
      [StringComparison]::OrdinalIgnoreCase) -or
    -not [IO.Path]::GetFileName($VerifierRoot).StartsWith(
      'subgen-v050-release-verifier-',[StringComparison]::Ordinal)) {
  throw 'Unsafe verifier materialization root'
}
New-Item -ItemType Directory -Path $VerifierRoot | Out-Null
$verifierOwner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $VerifierRoot /inheritance:r /grant:r `
  "${verifierOwner}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect verifier materialization root' }
function Export-ExactBlob {
  param(
    [Parameter(Mandatory)][string]$Blob,
    [Parameter(Mandatory)][string]$Destination,
    [Parameter(Mandatory)][string]$ExpectedSha256
  )
  $written = @(& python -I -c 'import hashlib,os,subprocess,sys; raw=subprocess.check_output(["git","cat-file","blob",sys.argv[1]]); fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); f=os.fdopen(fd,"wb"); f.write(raw); f.flush(); os.fsync(f.fileno()); f.close(); print(hashlib.sha256(raw).hexdigest())' `
    $Blob $Destination)
  if ($LASTEXITCODE -ne 0 -or $written.Count -ne 1 -or
      $written[0].Trim() -cne $ExpectedSha256 -or
      -not (Test-Path -LiteralPath $Destination -PathType Leaf) -or
      ((Get-Item -LiteralPath $Destination -Force).Attributes -band
        [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      (Get-FileHash -Algorithm SHA256 -LiteralPath $Destination).Hash.ToLowerInvariant() -cne
        $ExpectedSha256) {
    throw 'Exact Git blob materialization failed'
  }
}
$VerifierEvidence = Join-Path $VerifierRoot '90-evidence.md'
$VerifierObserver = Join-Path $VerifierRoot 'runtime_gate_observer.py'
$VerifierSampler = Join-Path $VerifierRoot 'gate_health_sampler.py'
$VerifierProducer = Join-Path $VerifierRoot 'monitor_frigate_priority.py'
try {
  Export-ExactBlob -Blob $evidenceBlob -Destination $VerifierEvidence `
    -ExpectedSha256 $blobHashValues[4]
  Export-ExactBlob -Blob $releaseObserverBlob -Destination $VerifierObserver `
    -ExpectedSha256 $binding.observer_sha256
  Export-ExactBlob -Blob $releaseSamplerBlob -Destination $VerifierSampler `
    -ExpectedSha256 $binding.sampler_sha256
  Export-ExactBlob -Blob $runtimeProducerBlob -Destination $VerifierProducer `
    -ExpectedSha256 $binding.producer_sha256
  & python -I $VerifierObserver verify-release `
    --evidence $VerifierEvidence --binding-prefix $bindingPrefix `
    --gate-seal $GateSealPath --phase-a-seal $PhaseASealPath `
    --phase-a-output $PhaseAOutputPath --phase-b-seal $PhaseBSealPath `
    --assertion-observation $AssertionObservationPath `
    --phase-a-receipt-trace $PhaseAReceiptTracePath `
    --phase-b-receipt-trace $PhaseBReceiptTracePath `
    --candidate-identity $CandidateIdentityPath `
    --execution-boundary-manifest $ExecutionBoundaryManifestPath `
    --priority-policy $PriorityPolicyPath `
    --unloaded-gpu-envelope $UnloadedEnvelopePath `
    --producer-source $VerifierProducer --sampler-source $VerifierSampler `
    --runtime-commit $RuntimeCommit --candidate-oci-index $ExpectedIndex `
    --candidate-config-digest $ExpectedConfig
  if ($LASTEXITCODE -ne 0) {
    throw 'Strict amended Task 11B release-seal verification failed'
  }
}
finally {
  if ([IO.Path]::GetFullPath($VerifierRoot) -cne $VerifierRoot -or
      -not $VerifierRoot.StartsWith($verifierPrefix,
        [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Verifier cleanup target changed'
  }
  Remove-Item -LiteralPath $VerifierRoot -Recurse -Force -ErrorAction Stop
  if (Test-Path -LiteralPath $VerifierRoot) {
    throw 'Verifier materialization cleanup failed'
  }
}
if ([string]::IsNullOrWhiteSpace($GateSealPath) -or
    -not (Test-Path -LiteralPath $GateSealPath -PathType Leaf) -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $GateSealPath).Hash.ToLower() -cne
      $binding.gate_seal_sha256) { throw 'Task 11B gate seal binding mismatch' }
$gateSeal = Get-Content -Raw -LiteralPath $GateSealPath | ConvertFrom-Json
if ($gateSeal.schema -cne 'subgen.task11b.shared-gpu-gate/v2' -or
    $gateSeal.outcome -cne 'pass' -or
    $gateSeal.sampler_sha256 -cne $binding.sampler_sha256 -or
    $gateSeal.sampler_test_sha256 -cne $binding.test_sha256 -or
    $gateSeal.observer_sha256 -cne $binding.observer_sha256 -or
    $gateSeal.observer_test_sha256 -cne $binding.observer_test_sha256 -or
    $gateSeal.producer_sha256 -cne $binding.producer_sha256 -or
    $gateSeal.policy_sha256 -cne $binding.policy_sha256 -or
    $gateSeal.unloaded_gpu_envelope_sha256 -cne $binding.unloaded_gpu_envelope_sha256 -or
    $gateSeal.candidate_oci_index -cne $ExpectedIndex -or
    $gateSeal.candidate_config_digest -cne $ExpectedConfig -or
    $gateSeal.cleanup.verified_stopped -ne $true -or
    $gateSeal.cleanup.candidate_pid_count -ne 0) {
  throw 'Task 11B seal did not prove the exact stopped passing candidate'
}

$actualDelta = @(& git diff --name-status --no-renames $RuntimeCommit $ReleaseCommit)
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect release delta' }
$expectedDelta = @(
  "M`tdocs/aegis/INDEX.md"
  "M`tdocs/aegis/adr/0002-memory-aware-segmented-transcription.md"
  "A`tdocs/aegis/baseline/2026-09-01-v0.5.0-release-baseline.md"
  "M`tdocs/aegis/plans/2026-08-31-memory-aware-segmented-transcription-v0.5.0.md"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/20-checkpoint.md"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/90-evidence.md"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/drift-check-draft.json"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/gate_health_sampler.py"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/resume-state-hint.json"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/runtime_gate_observer.py"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_gate_health_sampler.py"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_runtime_gate_observer.py"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/todo-checkpoint-draft.json"
)
$mismatch = @(Compare-Object -CaseSensitive `
  ($expectedDelta | Sort-Object) ($actualDelta | Sort-Object))
if ($mismatch.Count -ne 0) { throw 'Release delta manifest mismatch' }
$worktree = @(& git status --porcelain=v1 --untracked-files=all)
if ($LASTEXITCODE -ne 0 -or $worktree.Count -ne 0) {
  throw 'Release worktree is not clean'
}
$workflowFiles = @(Get-ChildItem -LiteralPath .github/workflows -File)
if ($workflowFiles.Count -eq 0) { throw 'No workflows found' }
$forbiddenTriggers = @(& rg -n '^\s*(push|pull_request|pull_request_target|release|schedule):' .github/workflows)
if ($LASTEXITCODE -eq 0 -or $forbiddenTriggers.Count -ne 0) {
  throw 'An automatic hosted-run trigger is present'
}
if ($LASTEXITCODE -ne 1) { throw 'Unable to inspect workflow triggers' }
$manualWorkflows = @(& rg -l '^\s*workflow_dispatch:\s*$' .github/workflows)
if ($LASTEXITCODE -ne 0 -or $manualWorkflows.Count -ne $workflowFiles.Count) {
  throw 'Every workflow must remain manual-only'
}
& gh repo view Herbertmt978/Subgen-English-Plex --json nameWithOwner --jq .nameWithOwner
if ($LASTEXITCODE -ne 0) { throw 'Unable to verify GitHub repository access' }

# Capture every hosted run, not an arbitrary recent window. The baseline is
# taken twice before the first public mutation, persisted owner-only, and then
# treated as immutable input. Every public-write wrapper below calls the
# assertion in a finally block, so a failed or response-lost write cannot evade
# the no-hosted-run gate. There is no rebaseline operation.
$PublicationStateRoot = [IO.Path]::GetFullPath(
  'D:\CodexTemp\subgen-v050-publication-contract')
$HostedBaselinePath = Join-Path $PublicationStateRoot 'hosted-runs-baseline.json'
if (Test-Path -LiteralPath $PublicationStateRoot) {
  throw 'Existing publication-contract state requires exact recovery; do not rebaseline'
}
New-Item -ItemType Directory -Path $PublicationStateRoot | Out-Null
$publicationOwner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $PublicationStateRoot /inheritance:r /grant:r `
  "${publicationOwner}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect publication-contract state' }
function Get-CompleteHostedRunSet {
  $pagesJson = @(& gh api --paginate --slurp `
    '/repos/Herbertmt978/Subgen-English-Plex/actions/runs?per_page=100')
  if ($LASTEXITCODE -ne 0 -or $pagesJson.Count -eq 0) {
    throw 'Unable to enumerate complete hosted-run history'
  }
  $pages = @(($pagesJson -join "`n") | ConvertFrom-Json)
  if ($pages.Count -eq 0) { throw 'Hosted-run pagination returned no page' }
  $authoritativeTotal = $null
  $ids = @($pages | ForEach-Object {
    if ($null -eq $_.workflow_runs) {
      throw 'Hosted-run page schema is invalid'
    }
    if ($null -eq $authoritativeTotal) {
      if ($_.total_count -is [bool] -or $_.total_count -isnot [int] -and
          $_.total_count -isnot [long] -or [int64]$_.total_count -lt 0) {
        throw 'Hosted-run authoritative total_count is invalid'
      }
      $authoritativeTotal = [int64]$_.total_count
    } elseif ([int64]$_.total_count -ne $authoritativeTotal) {
      throw 'Hosted-run total_count changed during pagination'
    }
    @($_.workflow_runs) | ForEach-Object {
      $id = [int64]$_.id
      if ($id -le 0) { throw 'Hosted-run ID is invalid' }
      if ([string]$_.status -cne 'completed') {
        throw 'A hosted repository run is not completed'
      }
      $id
    }
  } | Sort-Object)
  if ($null -eq $authoritativeTotal -or $ids.Count -ne $authoritativeTotal) {
    throw 'Hosted-run pagination count differs from authoritative total_count'
  }
  if (@($ids | Select-Object -Unique).Count -ne $ids.Count) {
    throw 'Hosted-run pagination returned duplicate IDs'
  }
  $canonical = [Text.UTF8Encoding]::new($false).GetBytes(
    (($ids | ForEach-Object { $_.ToString([Globalization.CultureInfo]::InvariantCulture) }) `
      -join "`n"))
  $sha = [Security.Cryptography.SHA256]::HashData($canonical)
  [pscustomobject]@{
    ids = $ids
    sha256 = ([BitConverter]::ToString($sha)).Replace('-','').ToLowerInvariant()
  }
}
function Test-SameHostedRunSet($Left, $Right) {
  if ($Left.sha256 -cne $Right.sha256 -or
      $Left.ids.Count -ne $Right.ids.Count) { return $false }
  return @(Compare-Object -CaseSensitive $Left.ids $Right.ids).Count -eq 0
}
$HostedRunBaseline = Get-CompleteHostedRunSet
$hostedConfirm = Get-CompleteHostedRunSet
if (-not (Test-SameHostedRunSet $HostedRunBaseline $hostedConfirm)) {
  throw 'Hosted-run history changed during baseline capture'
}
$hostedRecord = [ordered]@{
  schema = 'subgen.v050.hosted-run-baseline/v1'
  repository = 'Herbertmt978/Subgen-English-Plex'
  release_commit = $ReleaseCommit
  run_ids = @($HostedRunBaseline.ids)
  run_ids_sha256 = $HostedRunBaseline.sha256
}
$hostedBytes = [Text.UTF8Encoding]::new($false).GetBytes(
  ($hostedRecord | ConvertTo-Json -Depth 4 -Compress))
$hostedStream = [IO.FileStream]::new(
  $HostedBaselinePath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,
  [IO.FileShare]::None,4096,[IO.FileOptions]::WriteThrough)
try {
  $hostedStream.Write($hostedBytes,0,$hostedBytes.Length)
  $hostedStream.Flush($true)
}
finally { $hostedStream.Dispose() }
& icacls.exe $HostedBaselinePath /inheritance:r /grant:r `
  "${publicationOwner}:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect hosted-run baseline' }
$HostedBaselineFileSha256 =
  (Get-FileHash -Algorithm SHA256 -LiteralPath $HostedBaselinePath).Hash.ToLowerInvariant()
function Assert-HostedRunSetUnchanged([string]$AfterPublicWrite) {
  if ((Get-FileHash -Algorithm SHA256 -LiteralPath $HostedBaselinePath).Hash.ToLowerInvariant() -cne
      $HostedBaselineFileSha256) {
    throw 'Persisted hosted-run baseline changed'
  }
  $current = Get-CompleteHostedRunSet
  if (-not (Test-SameHostedRunSet $HostedRunBaseline $current)) {
    throw "Hosted-run set changed after public write: $AfterPublicWrite"
  }
}
& git fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Unable to fetch remote state' }
$existingTag = @(& git tag --list 'v0.5.0')
if ($LASTEXITCODE -ne 0 -or $existingTag.Count -gt 1) {
  throw 'Unable to inspect local v0.5.0 tag'
}
if ($existingTag.Count -eq 1) {
  $localTagType = @(& git cat-file -t 'refs/tags/v0.5.0')
  $localTagCommit = @(& git rev-parse --verify 'v0.5.0^{commit}')
  if ($LASTEXITCODE -ne 0 -or $localTagType.Count -ne 1 -or
      $localTagType[0].Trim() -cne 'tag' -or $localTagCommit.Count -ne 1 -or
      $localTagCommit[0].Trim() -cne $ReleaseCommit) {
    throw 'Existing local v0.5.0 tag is not the exact annotated release tag'
  }
}
$remoteTagLines = @(& git ls-remote --tags origin `
  'refs/tags/v0.5.0' 'refs/tags/v0.5.0^{}')
if ($LASTEXITCODE -ne 0 -or $remoteTagLines.Count -notin @(0,2)) {
  throw 'Remote v0.5.0 tag state is neither absent nor exact-annotated'
}
if ($remoteTagLines.Count -eq 2) {
  $remoteTagRef = @($remoteTagLines | Where-Object { $_ -match "`trefs/tags/v0\.5\.0$" })
  $remotePeeledRef = @($remoteTagLines | Where-Object { $_ -match "`trefs/tags/v0\.5\.0\^\{\}$" })
  if ($remoteTagRef.Count -ne 1 -or $remotePeeledRef.Count -ne 1 -or
      ($remotePeeledRef[0] -split "`t",2)[0] -cne $ReleaseCommit) {
    throw 'Existing remote v0.5.0 tag does not peel to RELEASE_COMMIT'
  }
}
$divergence = @(& git rev-list --left-right --count origin/main...HEAD)
if ($LASTEXITCODE -ne 0 -or $divergence.Count -ne 1 -or
    $divergence[0] -notmatch '^0\s+[0-9]+$') {
  throw 'Remote divergence is not a safe fast-forward'
}
$packageJson = @(& gh api --paginate --slurp `
  '/users/Herbertmt978/packages/container/subgen-english-plex/versions?per_page=100')
if ($LASTEXITCODE -ne 0 -or $packageJson.Count -eq 0) {
  throw 'Unable to inspect existing GHCR package versions'
}
$packagePages = @(($packageJson -join "`n") | ConvertFrom-Json)
$packageVersions = @($packagePages | ForEach-Object { @($_) } | ForEach-Object { $_ })
function Get-PackageTagDigest([string]$Tag) {
  $matches = @($packageVersions | Where-Object {
    @($_.metadata.container.tags) -ccontains $Tag
  })
  if ($matches.Count -gt 1) { throw "GHCR tag $Tag resolves to multiple versions" }
  if ($matches.Count -eq 0) { return $null }
  $digest = [string]$matches[0].name
  if ($digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
    throw "GHCR tag $Tag has an invalid manifest digest"
  }
  $digest
}
$VersionBeforeDigest = Get-PackageTagDigest 'v0.5.0'
$PriorLatestDigest = Get-PackageTagDigest 'latest'
if ($null -ne $VersionBeforeDigest -and $VersionBeforeDigest -cne $ExpectedIndex) {
  throw 'Existing GHCR v0.5.0 tag does not equal the gated candidate'
}
if ($null -eq $PriorLatestDigest) {
  throw 'Existing latest digest is required for exact rollback before mutation'
}
$ReleaseBodyPath = Join-Path $PublicationStateRoot 'release-body.md'
$releaseBodyResult = @(& python -I -c 'import hashlib,os,subprocess,sys; raw=subprocess.check_output(["git","cat-file","blob",sys.argv[1]]); raw.decode("utf-8","strict"); assert not raw.startswith(b"\xef\xbb\xbf"); fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); f=os.fdopen(fd,"wb"); f.write(raw); f.flush(); os.fsync(f.fileno()); f.close(); print(hashlib.sha256(raw).hexdigest())' `
  (Resolve-ExactBlob $ReleaseCommit $ReleaseNotesPath) $ReleaseBodyPath)
if ($LASTEXITCODE -ne 0 -or $releaseBodyResult.Count -ne 1 -or
    $releaseBodyResult[0].Trim() -cnotmatch '^[0-9a-f]{64}$' -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $ReleaseBodyPath).Hash.ToLowerInvariant() -cne
      $releaseBodyResult[0].Trim()) {
  throw 'Unable to materialize exact BOM-less UTF-8 release body'
}
& icacls.exe $ReleaseBodyPath /inheritance:r /grant:r `
  "${publicationOwner}:F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect immutable release body' }
$ReleaseBodySha256 = $releaseBodyResult[0].Trim()
$ExpectedReleaseBodyBytes = [IO.File]::ReadAllBytes($ReleaseBodyPath)
$StrictUtf8 = [Text.UTF8Encoding]::new($false,$true)
$roundTripBody = $StrictUtf8.GetBytes($StrictUtf8.GetString($ExpectedReleaseBodyBytes))
if ($roundTripBody.Length -ne $ExpectedReleaseBodyBytes.Length) {
  throw 'Release body is not canonical BOM-less UTF-8'
}
for ($bodyIndex = 0; $bodyIndex -lt $ExpectedReleaseBodyBytes.Length; $bodyIndex++) {
  if ($roundTripBody[$bodyIndex] -ne $ExpectedReleaseBodyBytes[$bodyIndex]) {
    throw 'Release body UTF-8 round trip changed bytes'
  }
}
function Get-AuthoritativeReleaseState {
  $probeOut = Join-Path $PublicationStateRoot `
    ('release-probe-' + [Guid]::NewGuid().ToString('N') + '.out')
  $probeErr = $probeOut + '.err'
  try {
    $ghPath = (Get-Command gh -ErrorAction Stop).Source
    $probe = Start-Process -FilePath $ghPath -ArgumentList @(
      'api','--include',
      '/repos/Herbertmt978/Subgen-English-Plex/releases/tags/v0.5.0'
    ) -NoNewWindow -Wait -PassThru -RedirectStandardOutput $probeOut `
      -RedirectStandardError $probeErr
    $stdout = [IO.File]::ReadAllText($probeOut,$StrictUtf8)
    $stderr = [IO.File]::ReadAllText($probeErr,$StrictUtf8)
    $statuses = @([regex]::Matches(
      $stdout, '(?m)^HTTP/\S+\s+(\d{3})\b') |
      ForEach-Object { [int]$_.Groups[1].Value })
    if ($statuses.Count -ne 1) {
      throw 'GitHub release probe did not return exactly one HTTP status'
    }
    $status = $statuses[0]
    if ($status -eq 404 -and $probe.ExitCode -ne 0) {
      return [pscustomobject]@{ kind = 'absent'; status = 404; release = $null }
    }
    if ($status -ne 200 -or $probe.ExitCode -ne 0) {
      throw "GitHub release probe failed with HTTP $status; absence is only HTTP 404"
    }
    $parts = [regex]::Split($stdout,'\r?\n\r?\n',2)
    if ($parts.Count -ne 2 -or [string]::IsNullOrWhiteSpace($parts[1])) {
      throw 'GitHub release HTTP 200 response body is missing'
    }
    $release = $parts[1] | ConvertFrom-Json
    return [pscustomobject]@{ kind = 'exact-candidate'; status = 200; release = $release }
  }
  finally {
    foreach ($probePath in @($probeOut,$probeErr)) {
      if (Test-Path -LiteralPath $probePath) {
        Remove-Item -LiteralPath $probePath -Force
      }
    }
  }
}
function Assert-ExactRelease200($State) {
  if ($State.status -ne 200 -or $State.kind -cne 'exact-candidate' -or
      $State.release.tag_name -cne 'v0.5.0' -or
      $State.release.name -cne 'Subgen English Plex v0.5.0' -or
      [bool]$State.release.draft -or [bool]$State.release.prerelease) {
    throw 'GitHub release HTTP 200 identity/state is not exact'
  }
  $actualBytes = $StrictUtf8.GetBytes([string]$State.release.body)
  if ($actualBytes.Length -ne $ExpectedReleaseBodyBytes.Length) {
    throw 'GitHub release body byte length differs from RELEASE_COMMIT'
  }
  for ($bodyIndex = 0; $bodyIndex -lt $actualBytes.Length; $bodyIndex++) {
    if ($actualBytes[$bodyIndex] -ne $ExpectedReleaseBodyBytes[$bodyIndex]) {
      throw 'GitHub release body bytes differ from RELEASE_COMMIT'
    }
  }
}
$releaseBefore = Get-AuthoritativeReleaseState
if ($releaseBefore.status -eq 200) {
  Assert-ExactRelease200 $releaseBefore
  $ReleaseBeforeState = 'exact'
} elseif ($releaseBefore.status -eq 404) {
  $ReleaseBeforeState = 'absent'
} else {
  throw 'GitHub release state is neither authoritative HTTP 404 nor exact HTTP 200'
}
```

On the Windows simulator, first run this identity-only preflight before pushing
main or the tag. It uses the already sealed build-history/config/manifest
artifacts and does not assume a current checkout. Docker Desktop's `.Id` is the
OCI index here; the runtime configuration digest is independently recomputed
from the exact configuration bytes and cross-bound to the platform manifest.

```powershell
$RuntimeCommit = [string]$env:SUBGEN_TASK11A_RUNTIME_COMMIT
$Candidate = [string]$env:SUBGEN_TASK11A_CANDIDATE_TAG
$IdentityRoot = [string]$env:SUBGEN_TASK11A_IDENTITY_ROOT
$ExpectedIdentityHash = [string]$env:SUBGEN_TASK11A_IDENTITY_SHA256
$ExpectedConfig = [string]$env:SUBGEN_TASK11A_CONFIG_DIGEST
$ExpectedPlatform = [string]$env:SUBGEN_TASK11A_PLATFORM_MANIFEST_DIGEST
$ExpectedIndex = [string]$env:SUBGEN_TASK11A_OCI_INDEX
if ($RuntimeCommit -ceq '4418b3c97296a04b311d29d9ce52abefef64e108' -or
    $ExpectedIndex -ceq 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc' -or
    [string]::IsNullOrWhiteSpace($Candidate) -or
    [string]::IsNullOrWhiteSpace($IdentityRoot)) {
  throw 'Fresh Task 11A candidate identity is missing or retired'
}
$identityPath = Join-Path $IdentityRoot 'image-identity.json'
$configPath = Join-Path $IdentityRoot 'image-config-final.json'
$manifestPath = Join-Path $IdentityRoot 'platform-manifest-final.json'
$historyPath = Join-Path $IdentityRoot 'build-history-final.json'
foreach ($Path in @($identityPath, $configPath, $manifestPath, $historyPath)) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Missing sealed candidate artifact: $Path"
  }
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $identityPath).Hash.ToLower() -cne
    $ExpectedIdentityHash) { throw 'Identity artifact hash mismatch' }
$identity = Get-Content -Raw -LiteralPath $identityPath | ConvertFrom-Json
if ($identity.image_identity.config_digest -cne $ExpectedConfig) {
  throw 'Identity config mismatch'
}
$configHash = 'sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $configPath).Hash.ToLower()
if ($configHash -cne $ExpectedConfig) { throw 'Config bytes mismatch' }
$imageConfig = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
$platform = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$history = Get-Content -Raw -LiteralPath $historyPath | ConvertFrom-Json
if ($platform.config.digest -cne $ExpectedConfig -or
    $imageConfig.config.Labels.'org.opencontainers.image.revision' -cne $RuntimeCommit) {
  throw 'Platform/config provenance mismatch'
}
$attachments = @($history.Attachments | ForEach-Object Digest)
if ($ExpectedPlatform -cnotin $attachments -or $ExpectedIndex -cnotin $attachments) {
  throw 'Build attachment mismatch'
}
$actualIndex = @(& docker.exe image inspect $Candidate --format '{{.Id}}')
if ($LASTEXITCODE -ne 0 -or $actualIndex.Count -ne 1 -or
    $actualIndex[0].Trim() -cne $ExpectedIndex) { throw 'Candidate index mismatch' }
$labelsRaw = @(& docker.exe image inspect $Candidate --format '{{json .Config.Labels}}')
if ($LASTEXITCODE -ne 0 -or $labelsRaw.Count -ne 1) { throw 'Label inspect failed' }
$labels = $labelsRaw[0] | ConvertFrom-Json
if ($labels.'org.opencontainers.image.revision' -cne $RuntimeCommit) {
  throw 'Candidate revision mismatch'
}
$layersRaw = @(& docker.exe image inspect $Candidate --format '{{json .RootFS.Layers}}')
if ($LASTEXITCODE -ne 0 -or $layersRaw.Count -ne 1) { throw 'Layer inspect failed' }
$actualLayers = @($layersRaw[0] | ConvertFrom-Json)
$expectedLayers = @($identity.image_identity.layer_diff_ids)
if ($actualLayers.Count -ne $expectedLayers.Count) { throw 'Layer count mismatch' }
for ($i = 0; $i -lt $expectedLayers.Count; $i++) {
  if ($actualLayers[$i] -cne $expectedLayers[$i] -or
      $imageConfig.rootfs.diff_ids[$i] -cne $expectedLayers[$i]) {
    throw "Ordered layer mismatch at index $i"
  }
}
```

Only after that preflight succeeds, push and prove main/tag locally. Target the
annotated tag explicitly at `RELEASE_COMMIT`, never at a mutable remote ref:

```powershell
foreach ($requiredFunction in @('Assert-BroadRepositoryPublicationLock',
    'Assert-HostedRunSetUnchanged')) {
  if ($null -eq (Get-Command $requiredFunction -CommandType Function `
        -ErrorAction SilentlyContinue)) {
    throw 'Broad repository lock/hosted baseline publisher contract is not active'
  }
}
Assert-BroadRepositoryPublicationLock
try {
  & git push origin "${ReleaseCommit}:main"
  if ($LASTEXITCODE -ne 0) { throw 'Main push failed' }
}
finally { Assert-HostedRunSetUnchanged 'main fast-forward' }
& git fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Main verification fetch failed' }
$remoteMain = @(& git rev-parse --verify 'origin/main^{commit}')
if ($LASTEXITCODE -ne 0 -or $remoteMain.Count -ne 1 -or
    $remoteMain[0].Trim() -cne $ReleaseCommit) { throw 'Remote main mismatch' }
if ($remoteTagLines.Count -eq 0) {
  if ($existingTag.Count -eq 0) {
    & git tag -a v0.5.0 $ReleaseCommit -m 'Subgen English Plex 0.5.0'
    if ($LASTEXITCODE -ne 0) { throw 'Annotated tag creation failed' }
  }
  try {
    & git push origin 'refs/tags/v0.5.0:refs/tags/v0.5.0'
    if ($LASTEXITCODE -ne 0) { throw 'Tag push failed' }
  }
  finally { Assert-HostedRunSetUnchanged 'annotated Git v0.5.0 tag' }
} elseif ($existingTag.Count -eq 1) {
  $localTagObject = @(& git rev-parse --verify 'refs/tags/v0.5.0')
  $remoteTagObject = ($remoteTagRef[0] -split "`t",2)[0]
  if ($LASTEXITCODE -ne 0 -or $localTagObject.Count -ne 1 -or
      $localTagObject[0].Trim() -cne $remoteTagObject) {
    throw 'Local and remote annotated tag objects differ'
  }
}
$provedRemoteTag = @(& git ls-remote --tags origin `
  'refs/tags/v0.5.0' 'refs/tags/v0.5.0^{}')
if ($LASTEXITCODE -ne 0 -or $provedRemoteTag.Count -ne 2 -or
    @($provedRemoteTag | Where-Object {
      $_ -match "`trefs/tags/v0\.5\.0\^\{\}$" -and
      ($_ -split "`t",2)[0] -ceq $ReleaseCommit
    }).Count -ne 1) { throw 'Remote annotated tag proof failed' }
```

Then use the already authorized local `gh` credential through SSH stdin to a
pre-transferred, SHA-256-verified PowerShell script on the simulator. The
script first repeats the complete identity preflight above, reads exactly one
token line from stdin, and uses an owner-only task-scoped `DOCKER_CONFIG`;
never print the token, put it in an argument, use the normal Docker
configuration, or retain it. The script accepts the already proved
`VersionBeforeDigest` and `PriorLatestDigest` as non-secret inputs and refuses
any registry state that changed after the GitHub Packages preflight. It tags
only the immutable expected image ID, never the mutable candidate tag. Publish
and verify `v0.5.0` first; do not mutate `latest` in this phase.

The anonymous pull-smoke must use the verified rootful Docker `29.1.3` daemon
inside the `Ubuntu-24.04` WSL distribution. The Windows `default` and
`desktop-linux` contexts share one Engine ID and therefore cannot provide this
isolation. Every anonymous Docker command must instead cross the exact boundary
`wsl.exe -d Ubuntu-24.04 -u root -- /usr/bin/env -i ... /usr/bin/docker
--host unix:///var/run/docker.sock`; the clean environment carries only a fixed
`HOME`, fixed `PATH`, and the new owner-only WSL `DOCKER_CONFIG`. Require the
socket to be a real local Unix socket, no `dockerd` TCP listener, Docker server
`29.1.3`, an Engine ID distinct from Docker Desktop, and zero starting
containers, images, and volumes. Pull by digest, validate config/diff IDs and
HTTP through the exact stopped-then-started container, then remove each owned
object and prove all three object classes empty again.

Before creating new task state, invoke this same pre-transferred,
SHA-256-verified script with `-Mode recover` when the fixed owner-only lock,
pending state, or `D:\CodexTemp\subgen-v050-release-state.json` exists. The
fixed lock is created exclusively before state work and binds one valid private
token plus inactive PID. Recovery promotes an exact token-bound temp-only intent
or exact pending replacement, discards only a lock-owned incomplete pending
write, and handles a lock-only pre-intent without invoking WSL. It accepts only
the exact v2 state schema and keys, the lock's token/PID, the
recorded primary Engine ID, WSL distribution/socket/server version and pre-task
running state, the recorded anonymous Engine ID, confined Windows paths, and a
token-derived POSIX run/config/cidfile path whose `realpath`, type, UID/GID, and
mode are exact. The smoke ID is null or full 64-hex. The record also carries the
exact smoke name, phase, published digest reference, derived/created volume
names, and the exact process baseline captured after the WSL Engine is bound.
Recovery binds a live crash-window container through the owner-confined cidfile
and exactly one full ID returned for the token-derived exact name. Live name and
full-container inventories must agree; when both are authoritatively empty, a
stale recorded/cidfile ID is cleanup history and does not invent a live target.
It revalidates the exact image plus both private labels before stopping/removing the container,
derives each fixed volume name from the token and validates its label before
removal. Before pull, publication durably records the exact digest reference and
`image_pull_intent` phase. Recovery or normal cleanup may remove that image only
when this task-owned durable reference/phase proof and the expected config all
agree; digest equality alone is never deletion authority. It uses only the recorded Windows `DOCKER_CONFIG` to log out, and the
same exact WSL boundary to remove the verified POSIX config/run root. It then
removes the two recorded Windows task directories and state file and proves
every object absent. It may terminate an initially stopped distribution only
when the current normalized process multiset exactly equals the recorded
post-bind baseline and no other task run or Docker object exists. An active
recorded process, anything other than one non-inherited owner Full-Control ACE
on a Windows lock/state/cleanup root, a reparse-point or POSIX path error, Engine change,
unknown object/process, unrecognized registry state, or cleanup failure blocks
without deleting by prefix. Invoke `-Mode publish` only after recovery proves
the state absent; publication still refuses stale state, so the two modes cannot
race. Durably create the intent record before the first WSL invocation;
atomically persist the phase and exact owned-object set before or immediately
after every mutation, never the registry credential. If package-write authority
or safe recovery is absent, stop.

```powershell
param(
  [ValidateSet('publish','recover')]
  [string]$Mode = 'publish'
)
$ErrorActionPreference = 'Stop'
$TaskParent = [IO.Path]::GetFullPath('D:\CodexTemp')
$Candidate = [string]$env:SUBGEN_TASK11A_CANDIDATE_TAG
$VersionRef = 'ghcr.io/herbertmt978/subgen-english-plex:v0.5.0'
$RuntimeCommit = [string]$env:SUBGEN_TASK11A_RUNTIME_COMMIT
$ExpectedConfig = [string]$env:SUBGEN_TASK11A_CONFIG_DIGEST
$ExpectedIndex = [string]$env:SUBGEN_TASK11A_OCI_INDEX
$VersionBeforeDigest = $env:SUBGEN_VERSION_BEFORE_DIGEST
$PriorLatestDigest = $env:SUBGEN_PRIOR_LATEST_DIGEST
$AnonymousDistro = 'Ubuntu-24.04'
$AnonymousSocket = 'unix:///var/run/docker.sock'
$AnonymousServerVersion = '29.1.3'
$AnonymousStateParent = '/root/.local/state/subgen-v050-release'
$StatePath = Join-Path $TaskParent 'subgen-v050-release-state.json'
$StateTempPrefix = 'subgen-v050-release-state-'
$LockPath = Join-Path $TaskParent 'subgen-v050-release.lock.json'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($RuntimeCommit -ceq '4418b3c97296a04b311d29d9ce52abefef64e108' -or
    $ExpectedIndex -ceq 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc' -or
    [string]::IsNullOrWhiteSpace($Candidate)) {
  throw 'Fresh Task 11A candidate identity is missing or retired'
}

function Assert-OwnerOnlyAcl {
  param([string]$Path, [switch]$Directory)
  $item = Get-Item -Force -LiteralPath $Path
  $acl = Get-Acl -LiteralPath $Path
  $rules = @($acl.Access)
  $expectedInheritance = if ($Directory) {
    [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
  } else { [Security.AccessControl.InheritanceFlags]::None }
  if ($acl.Owner -ine $owner -or -not $acl.AreAccessRulesProtected -or
      $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ine $owner -or
      $rules[0].AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
      $rules[0].FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or
      $rules[0].InheritanceFlags -ne $expectedInheritance -or
      $rules[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None -or
      $rules[0].IsInherited -or
      (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) -or
      ($Directory -and -not $item.PSIsContainer) -or
      (-not $Directory -and $item.PSIsContainer)) {
    throw 'Task path does not have the exact owner-only ACL/type'
  }
}

function Set-OwnerOnlyAcl {
  param([string]$Path, [switch]$Directory)
  $acl = Get-Acl -LiteralPath $Path
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($acl.Access)) {
    [void]$acl.RemoveAccessRuleSpecific($rule)
  }
  $inheritance = if ($Directory) {
    [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
  } else { [Security.AccessControl.InheritanceFlags]::None }
  $ownerRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $owner, [Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance, [Security.AccessControl.PropagationFlags]::None,
    [Security.AccessControl.AccessControlType]::Allow)
  [void]$acl.AddAccessRule($ownerRule)
  Set-Acl -LiteralPath $Path -AclObject $acl
  Assert-OwnerOnlyAcl -Path $Path -Directory:$Directory
}

function Get-WslProcessSnapshot {
  $raw = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/ps `
    -eo 'uid=,comm=,args=')
  if ($LASTEXITCODE -ne 0 -or $raw.Count -eq 0) {
    throw 'Unable to capture exact WSL process baseline'
  }
  @($raw | ForEach-Object {
    ([string]$_ -replace '\s+', ' ').Trim()
  } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Sort-Object)
}

function Assert-RecoveryWindowsDirectory {
  param([string]$Path, [string]$Expected)
  $full = [IO.Path]::GetFullPath($Path)
  if ($full -cne [IO.Path]::GetFullPath($Expected)) {
    throw 'Recorded Windows task path is not exact'
  }
  if (-not (Test-Path -LiteralPath $full)) { return $false }
  Assert-OwnerOnlyAcl -Path $full -Directory
  $true
}

function Invoke-ReleaseRecovery {
  $expectedStateKeys = @(
    'anonymous_config_root','anonymous_distro','anonymous_engine_id',
    'anonymous_run_root','anonymous_server_version','anonymous_socket',
    'anonymous_transport','anonymous_was_running','created_volumes',
    'docker_config_root','image_pull_ref','lock_path','phase','primary_engine_id',
    'process_id','published_ref','run_token','schema','smoke_cidfile','smoke_id',
    'smoke_name','smoke_root','state_temp_path','volume_names','wsl_process_baseline'
  )
  if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
    throw 'Recovery mode requires the fixed release lock'
  }
  Assert-OwnerOnlyAcl -Path $LockPath
  $releaseLock = Get-Content -Raw -LiteralPath $LockPath | ConvertFrom-Json
  $lockKeys = @($releaseLock.PSObject.Properties.Name | Sort-Object)
  if (@(Compare-Object -CaseSensitive @('process_id','run_token','schema') `
        $lockKeys).Count -ne 0 -or
      $releaseLock.schema -cne 'subgen.release-lock/v1' -or
      [string]$releaseLock.run_token -cnotmatch '^[0-9a-f]{32}$') {
    throw 'Release lock schema/token is invalid'
  }
  $StateTempPath = Join-Path $TaskParent `
    "$StateTempPrefix$($releaseLock.run_token).pending.json"
  $lockPid = 0
  if (-not [int]::TryParse([string]$releaseLock.process_id, [ref]$lockPid) -or
      $lockPid -le 0 -or
      $null -ne (Get-Process -Id $lockPid -ErrorAction SilentlyContinue)) {
    throw 'Recorded publication lock is active or invalid'
  }
  $stateExists = Test-Path -LiteralPath $StatePath -PathType Leaf
  $pendingExists = Test-Path -LiteralPath $StateTempPath -PathType Leaf
  if (-not $stateExists -and -not $pendingExists) {
    Remove-Item -LiteralPath $LockPath -Force
    if ((Test-Path -LiteralPath $LockPath) -or
        (Test-Path -LiteralPath $StatePath) -or
        (Test-Path -LiteralPath $StateTempPath)) {
      throw 'Lock-only initial-intent recovery failed'
    }
    return
  }
  if ($stateExists) {
    Assert-OwnerOnlyAcl -Path $StatePath
    $existingState = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
    $existingKeys = @($existingState.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object -CaseSensitive ($expectedStateKeys | Sort-Object) `
          $existingKeys).Count -ne 0 -or
        $existingState.schema -cne 'subgen.release-state/v2' -or
        [string]$existingState.run_token -cne [string]$releaseLock.run_token -or
        [string]$existingState.process_id -cne [string]$releaseLock.process_id -or
        $existingState.lock_path -cne $LockPath -or
        $existingState.state_temp_path -cne $StateTempPath) {
      throw 'Existing release state does not belong to the fixed lock'
    }
  }
  if ($pendingExists) {
    Assert-OwnerOnlyAcl -Path $StateTempPath
    $pending = $null
    try { $pending = Get-Content -Raw -LiteralPath $StateTempPath | ConvertFrom-Json }
    catch {
      Remove-Item -LiteralPath $StateTempPath -Force
      if (-not $stateExists) {
        Remove-Item -LiteralPath $LockPath -Force
        if ((Test-Path -LiteralPath $StateTempPath) -or
            (Test-Path -LiteralPath $LockPath)) {
          throw 'Partial temp-only initial-intent cleanup failed'
        }
        return
      }
      $pendingExists = $false
    }
    if ($pendingExists) {
      $pendingKeys = @($pending.PSObject.Properties.Name | Sort-Object)
      if ([string]$pending.run_token -cne [string]$releaseLock.run_token) {
        throw 'Pending state does not belong to the fixed release lock'
      }
      $pendingIsExact = (
        @(Compare-Object -CaseSensitive ($expectedStateKeys | Sort-Object) `
          $pendingKeys).Count -eq 0 -and
        $pending.schema -ceq 'subgen.release-state/v2' -and
        $pending.lock_path -ceq $LockPath -and
        $pending.state_temp_path -ceq $StateTempPath -and
        [string]$pending.process_id -ceq [string]$releaseLock.process_id)
      if (-not $pendingIsExact) {
        Remove-Item -LiteralPath $StateTempPath -Force
        if (-not $stateExists) {
          Remove-Item -LiteralPath $LockPath -Force
          if ((Test-Path -LiteralPath $StateTempPath) -or
              (Test-Path -LiteralPath $LockPath)) {
            throw 'Inexact temp-only initial-intent cleanup failed'
          }
          return
        }
      } else {
        [IO.File]::Move($StateTempPath, $StatePath, $true)
        Assert-OwnerOnlyAcl -Path $StatePath
        $stateExists = $true
      }
    }
  }
  if (-not $stateExists) { throw 'Recovery could not materialize release state' }
  Assert-OwnerOnlyAcl -Path $StatePath
  $recovery = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
  $actualStateKeys = @($recovery.PSObject.Properties.Name | Sort-Object)
  if (@(Compare-Object -CaseSensitive ($expectedStateKeys | Sort-Object) `
        $actualStateKeys).Count -ne 0 -or
      $recovery.schema -cne 'subgen.release-state/v2') {
    throw 'Release recovery state schema/keys mismatch'
  }
  $RunToken = [string]$recovery.run_token
  if ($RunToken -cnotmatch '^[0-9a-f]{32}$' -or
      $RunToken -cne [string]$releaseLock.run_token -or
      [string]$recovery.process_id -cne [string]$releaseLock.process_id -or
      $recovery.lock_path -cne $LockPath -or
      $recovery.state_temp_path -cne $StateTempPath) {
    throw 'Recovery state does not belong to the fixed release lock'
  }
  $recordedPid = 0
  if (-not [int]::TryParse([string]$recovery.process_id, [ref]$recordedPid) -or
      $recordedPid -le 0 -or
      $null -ne (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)) {
    throw 'Recorded publication process is active or invalid'
  }
  $DockerConfigRoot = Join-Path $TaskParent "subgen-v050-docker-config-$RunToken"
  $SmokeRoot = Join-Path $TaskParent "subgen-v050-release-$RunToken"
  $AnonymousRunRoot = "$AnonymousStateParent/$RunToken"
  $AnonymousConfigRoot = "$AnonymousRunRoot/docker-config"
  $SmokeCidFile = "$AnonymousRunRoot/smoke.cid"
  $SmokeName = "subgen-v050-release-$RunToken"
  $MediaVolume = "subgen-v050-media-$RunToken"
  $ModelsVolume = "subgen-v050-models-$RunToken"
  $MonitorVolume = "subgen-v050-monitor-$RunToken"
  $expectedVolumes = @($MediaVolume,$ModelsVolume,$MonitorVolume)
  $volumeKeys = @($recovery.volume_names.PSObject.Properties.Name | Sort-Object)
  $recordedVolumes = @($recovery.created_volumes)
  if ($recovery.anonymous_transport -cne 'wsl' -or
      $recovery.anonymous_distro -cne $AnonymousDistro -or
      $recovery.anonymous_socket -cne $AnonymousSocket -or
      $recovery.anonymous_server_version -cne $AnonymousServerVersion -or
      $recovery.anonymous_was_running -isnot [bool] -or
      $recovery.docker_config_root -cne $DockerConfigRoot -or
      $recovery.smoke_root -cne $SmokeRoot -or
      $recovery.anonymous_run_root -cne $AnonymousRunRoot -or
      $recovery.anonymous_config_root -cne $AnonymousConfigRoot -or
      $recovery.smoke_cidfile -cne $SmokeCidFile -or
      $recovery.smoke_name -cne $SmokeName -or
      @($volumeKeys).Count -ne 3 -or
      @($volumeKeys) -join ',' -cne 'media,models,monitor' -or
      $recovery.volume_names.media -cne $MediaVolume -or
      $recovery.volume_names.models -cne $ModelsVolume -or
      $recovery.volume_names.monitor -cne $MonitorVolume -or
      @($recordedVolumes | Sort-Object -Unique).Count -ne $recordedVolumes.Count -or
      @($recordedVolumes | Where-Object { $_ -cnotin $expectedVolumes }).Count -ne 0) {
    throw 'Release recovery state is not token-derived and exact'
  }
  $allowedPhases = @('intent','engine_bound','process_baseline_bound',
    'wsl_config_created','windows_directories_created','image_pull_intent','image_pulled',
    'volume_created','container_created','container_started','container_stopped',
    'container_removed','volume_removed','image_removed','anonymous_engine_clean',
    'cleanup_container_removed','cleanup_volume_removed','cleanup_image_removed',
    'wsl_paths_removed')
  if ([string]$recovery.phase -cnotin $allowedPhases) {
    throw 'Unknown release recovery phase'
  }
  $recordedSmokeId = if ($null -eq $recovery.smoke_id) {
    $null
  } else { [string]$recovery.smoke_id }
  if ($null -ne $recordedSmokeId -and
      $recordedSmokeId -cnotmatch '^[0-9a-f]{64}$') {
    throw 'Recorded smoke ID is invalid'
  }
  $expectedPublishedRef = "ghcr.io/herbertmt978/subgen-english-plex@$ExpectedIndex"
  if (($null -ne $recovery.image_pull_ref -and
       [string]$recovery.image_pull_ref -cne $expectedPublishedRef) -or
      ($null -ne $recovery.published_ref -and
       [string]$recovery.published_ref -cne $expectedPublishedRef) -or
      ($null -ne $recovery.published_ref -and
       [string]$recovery.image_pull_ref -cne $expectedPublishedRef)) {
    throw 'Recorded image pull/published reference is not exact'
  }
  $imageOwnedPhases = @('image_pull_intent','image_pulled','volume_created',
    'container_created','container_started','container_stopped','container_removed',
    'volume_removed','cleanup_container_removed','cleanup_volume_removed')
  $baseline = @($recovery.wsl_process_baseline)
  if (@($baseline | Where-Object {
        $_ -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$_)
      }).Count -ne 0 -or
      ($null -eq $recovery.anonymous_engine_id -and
       [string]$recovery.phase -cne 'intent') -or
      ($baseline.Count -eq 0 -and
       [string]$recovery.phase -cnotin @('intent','engine_bound'))) {
    throw 'Recorded WSL Engine/process identity is inconsistent with its phase'
  }
  $primaryId = @(& docker.exe info --format '{{.ID}}')
  if ($LASTEXITCODE -ne 0 -or $primaryId.Count -ne 1 -or
      $primaryId[0].Trim() -cne [string]$recovery.primary_engine_id) {
    throw 'Primary Docker Engine changed before recovery'
  }
  & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test -e $AnonymousRunRoot
  $earlyRunExit = $LASTEXITCODE
  if ($earlyRunExit -notin @(0,1)) { throw 'Unable to inspect recovery run root' }
  if ($earlyRunExit -eq 0) {
    $earlyRunFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
      -c '%F,%u,%g,%a' -- $AnonymousRunRoot)
    $earlyRunReal = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/realpath `
      -e -- $AnonymousRunRoot)
    & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test -e $AnonymousConfigRoot
    $earlyConfigExit = $LASTEXITCODE
    if ($earlyConfigExit -notin @(0,1) -or $earlyRunFacts.Count -ne 1 -or
        $earlyRunFacts[0].Trim() -cne 'directory,0,0,700' -or
        $earlyRunReal.Count -ne 1 -or $earlyRunReal[0].Trim() -cne $AnonymousRunRoot) {
      throw 'Recovery POSIX run root is unsafe before Docker use'
    }
    if ($earlyConfigExit -eq 0) {
      $earlyConfigFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
        -c '%F,%u,%g,%a' -- $AnonymousConfigRoot)
      $earlyConfigReal = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/realpath `
        -e -- $AnonymousConfigRoot)
      if ($LASTEXITCODE -ne 0 -or $earlyConfigFacts.Count -ne 1 -or
          $earlyConfigFacts[0].Trim() -cne 'directory,0,0,700' -or
          $earlyConfigReal.Count -ne 1 -or
          $earlyConfigReal[0].Trim() -cne $AnonymousConfigRoot) {
        throw 'Recovery WSL Docker config is unsafe before use'
      }
    }
  }
  $dockerPrefix = @(
    '-d',$AnonymousDistro,'-u','root','--','/usr/bin/env','-i',
    'HOME=/root','PATH=/usr/sbin:/usr/bin:/sbin:/bin',
    "DOCKER_CONFIG=$AnonymousConfigRoot",'/usr/bin/docker','--host',$AnonymousSocket
  )
  $factsRaw = @(& wsl.exe @dockerPrefix info --format '{{json .}}')
  if ($LASTEXITCODE -ne 0 -or $factsRaw.Count -ne 1) {
    throw 'Unable to bind WSL Docker during recovery'
  }
  $facts = $factsRaw[0] | ConvertFrom-Json
  if ([string]::IsNullOrWhiteSpace([string]$facts.ID) -or
      $facts.OSType -cne 'linux' -or
      $facts.ServerVersion -cne $AnonymousServerVersion -or
      $facts.ID -ceq $primaryId[0].Trim() -or
      ($null -ne $recovery.anonymous_engine_id -and
       $facts.ID -cne [string]$recovery.anonymous_engine_id)) {
    throw 'Anonymous Engine identity changed before recovery'
  }
  $socketFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
    -c '%F,%u,%g,%a' -- /var/run/docker.sock)
  $tcpListeners = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/ss -H -ltnp)
  if ($LASTEXITCODE -ne 0 -or $socketFacts.Count -ne 1 -or
      $socketFacts[0].Trim() -cnotmatch '^socket,0,[0-9]+,660$' -or
      @($tcpListeners | Where-Object { $_ -match '(?i)\bdockerd\b' }).Count -ne 0) {
    throw 'Recovery WSL Docker boundary is unsafe'
  }
  $runExistsRaw = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test `
    -e $AnonymousRunRoot)
  $runExistsExit = $LASTEXITCODE
  if ($runExistsExit -notin @(0,1)) { throw 'Unable to inspect recovery run root' }
  $runExists = $runExistsExit -eq 0
  $cidSmokeId = $null
  if ($runExists) {
    $runFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
      -c '%F,%u,%g,%a' -- $AnonymousRunRoot)
    $runReal = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/realpath `
      -e -- $AnonymousRunRoot)
    $runEntries = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/find `
      $AnonymousRunRoot -mindepth 1 -maxdepth 1 -printf '%f\n')
    if ($LASTEXITCODE -ne 0 -or $runFacts.Count -ne 1 -or
        $runFacts[0].Trim() -cne 'directory,0,0,700' -or
        $runReal.Count -ne 1 -or $runReal[0].Trim() -cne $AnonymousRunRoot -or
        @($runEntries | Where-Object { $_.Trim() -cnotin @('docker-config','smoke.cid') }).Count -ne 0) {
      throw 'Recovery POSIX run root is not exact'
    }
    & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test -e $SmokeCidFile
    $cidExistsExit = $LASTEXITCODE
    if ($cidExistsExit -notin @(0,1)) { throw 'Unable to inspect smoke cidfile' }
    if ($cidExistsExit -eq 0) {
      $cidFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
        -c '%F,%u,%g,%a' -- $SmokeCidFile)
      $cidLines = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/cat -- $SmokeCidFile)
      if ($LASTEXITCODE -ne 0 -or $cidFacts.Count -ne 1 -or
          $cidFacts[0].Trim() -cnotmatch '^regular file,0,0,(600|640|644)$' -or
          $cidLines.Count -gt 1) { throw 'Smoke cidfile ownership/content is unsafe' }
      $cidText = ($cidLines -join '').Trim()
      if (-not [string]::IsNullOrWhiteSpace($cidText)) {
        if ($cidText -cnotmatch '^[0-9a-f]{64}$') { throw 'Smoke cidfile ID is invalid' }
        $cidSmokeId = $cidText
      }
    }
  }
  $namedIds = @(& wsl.exe @dockerPrefix ps -aq --no-trunc `
    --filter "name=^/$SmokeName$")
  if ($LASTEXITCODE -ne 0 -or $namedIds.Count -gt 1 -or
      @($namedIds | Where-Object { $_.Trim() -cnotmatch '^[0-9a-f]{64}$' }).Count -ne 0) {
    throw 'Exact smoke-name recovery binding is ambiguous'
  }
  $allContainers = @(& wsl.exe @dockerPrefix ps -aq --no-trunc)
  $containerExit = $LASTEXITCODE
  $allImages = @(& wsl.exe @dockerPrefix image ls -aq --no-trunc)
  $imageExit = $LASTEXITCODE
  $allVolumes = @(& wsl.exe @dockerPrefix volume ls -q)
  $volumeExit = $LASTEXITCODE
  if ($containerExit -ne 0 -or $imageExit -ne 0 -or $volumeExit -ne 0 -or
      @($allContainers | Where-Object { $_.Trim() -cnotmatch '^[0-9a-f]{64}$' }).Count -ne 0 -or
      @($allImages | Where-Object { $_.Trim() -cne $ExpectedConfig }).Count -ne 0 -or
      @($allVolumes | Where-Object { $_.Trim() -cnotin $expectedVolumes }).Count -ne 0 -or
      ($allImages.Count -ne 0 -and
       ([string]$recovery.image_pull_ref -cne $expectedPublishedRef -or
        [string]$recovery.phase -cnotin $imageOwnedPhases))) {
    throw 'Unknown WSL Docker object blocks recovery'
  }
  $historicalIds = @(@($recordedSmokeId,$cidSmokeId) | Where-Object {
    -not [string]::IsNullOrWhiteSpace([string]$_)
  } | Sort-Object -Unique)
  if ($allContainers.Count -eq 0) {
    if ($namedIds.Count -ne 0) {
      throw 'Name inventory disagrees with authoritative empty container set'
    }
    # A recorded/cidfile ID may be stale after successful removal; live absence wins.
    $boundSmokeId = $null
  } else {
    if ($allContainers.Count -ne 1 -or $namedIds.Count -ne 1 -or
        $allContainers[0].Trim() -cne $namedIds[0].Trim() -or
        @($historicalIds | Where-Object {
          $_ -cne $allContainers[0].Trim()
        }).Count -ne 0) {
      throw 'Live/recorded smoke container identities disagree'
    }
    $boundSmokeId = $allContainers[0].Trim()
  }
  if ($null -ne $boundSmokeId) {
    $containerRaw = @(& wsl.exe @dockerPrefix inspect $boundSmokeId `
      --format '{{json .}}')
    if ($LASTEXITCODE -ne 0 -or $containerRaw.Count -ne 1) {
      throw 'Unable to inspect recovery smoke container'
    }
    $container = $containerRaw[0] | ConvertFrom-Json
    if ($container.Id -cne $boundSmokeId -or $container.Name -cne "/$SmokeName" -or
        $container.Image -cne $ExpectedConfig -or
        $container.Config.Labels.'io.github.herbertmt978.subgen.release-smoke' -cne 'true' -or
        $container.Config.Labels.'io.github.herbertmt978.subgen.release-token' -cne $RunToken) {
      throw 'Recovery smoke container ownership mismatch'
    }
  }
  foreach ($Volume in $allVolumes) {
    $volumeLabelsRaw = @(& wsl.exe @dockerPrefix volume inspect $Volume.Trim() `
      --format '{{json .Labels}}')
    if ($LASTEXITCODE -ne 0 -or $volumeLabelsRaw.Count -ne 1 -or
        ($volumeLabelsRaw[0] | ConvertFrom-Json).'io.github.herbertmt978.subgen.release-token' -cne
          $RunToken) { throw 'Recovery volume ownership mismatch' }
  }
  if ($null -ne $boundSmokeId) {
    $running = @(& wsl.exe @dockerPrefix inspect $boundSmokeId `
      --format '{{.State.Running}}')
    if ($LASTEXITCODE -ne 0 -or $running.Count -ne 1) {
      throw 'Unable to read recovery smoke running state'
    }
    if ($running[0].Trim() -ceq 'true') {
      & wsl.exe @dockerPrefix stop --time 30 $boundSmokeId | Out-Null
      if ($LASTEXITCODE -ne 0) { throw 'Recovery smoke stop failed' }
    }
    & wsl.exe @dockerPrefix rm $boundSmokeId | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Recovery smoke removal failed' }
  }
  foreach ($Volume in $allVolumes) {
    & wsl.exe @dockerPrefix volume rm $Volume.Trim() | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Recovery volume removal failed' }
  }
  if ($allImages.Count -ne 0) {
    $imageConfig = @(& wsl.exe @dockerPrefix image inspect $expectedPublishedRef `
      --format '{{.Id}}')
    if ($LASTEXITCODE -ne 0 -or $imageConfig.Count -ne 1 -or
        $imageConfig[0].Trim() -cne $ExpectedConfig) {
      throw 'Recovery image reference/config mismatch'
    }
    & wsl.exe @dockerPrefix image rm $expectedPublishedRef | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Recovery image removal failed' }
  }
  $remaining = @(& wsl.exe @dockerPrefix ps -aq)
  $remainingContainerExit = $LASTEXITCODE
  $remainingImages = @(& wsl.exe @dockerPrefix image ls -aq)
  $remainingImageExit = $LASTEXITCODE
  $remainingVolumes = @(& wsl.exe @dockerPrefix volume ls -q)
  $remainingVolumeExit = $LASTEXITCODE
  if ($remainingContainerExit -ne 0 -or $remainingImageExit -ne 0 -or
      $remainingVolumeExit -ne 0 -or $remaining.Count -ne 0 -or
      $remainingImages.Count -ne 0 -or $remainingVolumes.Count -ne 0) {
    throw 'Recovery did not return the WSL Docker Engine to empty'
  }
  if ($runExists) {
    & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rm -f -- $SmokeCidFile
    if ($LASTEXITCODE -ne 0) { throw 'Recovery cidfile removal failed' }
    $configExistsRaw = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test `
      -e $AnonymousConfigRoot)
    $configExistsExit = $LASTEXITCODE
    if ($configExistsExit -notin @(0,1)) { throw 'Unable to inspect WSL config root' }
    if ($configExistsExit -eq 0) {
      $configFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
        -c '%F,%u,%g,%a' -- $AnonymousConfigRoot)
      $configReal = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/realpath `
        -e -- $AnonymousConfigRoot)
      if ($LASTEXITCODE -ne 0 -or $configFacts.Count -ne 1 -or
          $configFacts[0].Trim() -cne 'directory,0,0,700' -or
          $configReal.Count -ne 1 -or $configReal[0].Trim() -cne $AnonymousConfigRoot) {
        throw 'Recovery anonymous config path is unsafe'
      }
      & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rm -rf -- $AnonymousConfigRoot
      if ($LASTEXITCODE -ne 0) { throw 'Recovery WSL config cleanup failed' }
    }
    & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rmdir -- $AnonymousRunRoot
    if ($LASTEXITCODE -ne 0) { throw 'Recovery WSL run-root cleanup failed' }
  }
  $dockerConfigExists = Assert-RecoveryWindowsDirectory $recovery.docker_config_root `
    $DockerConfigRoot
  $smokeRootExists = Assert-RecoveryWindowsDirectory $recovery.smoke_root $SmokeRoot
  if ($dockerConfigExists) {
    $env:DOCKER_CONFIG = $DockerConfigRoot
    & docker.exe logout ghcr.io 2>$null | Out-Null
    $logoutExit = $LASTEXITCODE
    Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
    if ($logoutExit -ne 0) { throw 'Recovery task-scoped registry logout failed' }
  }
  if ($smokeRootExists) { Remove-Item -LiteralPath $SmokeRoot -Recurse -Force }
  if ($dockerConfigExists) {
    Remove-Item -LiteralPath $DockerConfigRoot -Recurse -Force
  }
  if ((Test-Path -LiteralPath $SmokeRoot) -or
      (Test-Path -LiteralPath $DockerConfigRoot)) {
    throw 'Recovery Windows task directory cleanup failed'
  }
  if (-not [bool]$recovery.anonymous_was_running) {
    if ($baseline.Count -eq 0) {
      throw 'No exact WSL process baseline exists; refusing termination'
    }
    $currentProcesses = @(Get-WslProcessSnapshot)
    if (@(Compare-Object -CaseSensitive $baseline $currentProcesses).Count -ne 0) {
      throw 'Unrelated WSL process change blocks termination'
    }
    $otherRuns = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/find `
      $AnonymousStateParent -mindepth 1 -maxdepth 1 -print -quit)
    if ($LASTEXITCODE -ne 0 -or $otherRuns.Count -ne 0) {
      throw 'Another WSL release run blocks termination'
    }
    $finalProcesses = @(Get-WslProcessSnapshot)
    if (@(Compare-Object -CaseSensitive $baseline $finalProcesses).Count -ne 0) {
      throw 'WSL process set changed immediately before termination'
    }
    & wsl.exe --terminate $AnonymousDistro
    if ($LASTEXITCODE -ne 0) { throw 'Unable to restore stopped WSL state' }
    $runningAfterRaw = @(& wsl.exe --list --running --quiet)
    $runningAfter = @($runningAfterRaw | ForEach-Object {
      ([string]$_).Replace("`0", '').Trim()
    } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($LASTEXITCODE -ne 0 -or $runningAfter -ccontains $AnonymousDistro) {
      throw 'WSL distribution remained running after recovery termination'
    }
  }
  if (Test-Path -LiteralPath $StateTempPath) {
    throw 'Pending replacement remained after recovery'
  }
  Assert-OwnerOnlyAcl -Path $StatePath
  Remove-Item -LiteralPath $StatePath -Force
  if (Test-Path -LiteralPath $StatePath) {
    throw 'Recovery state cleanup failed'
  }
  Assert-OwnerOnlyAcl -Path $LockPath
  Remove-Item -LiteralPath $LockPath -Force
  if (Test-Path -LiteralPath $LockPath) {
    throw 'Recovery lock cleanup failed'
  }
}

if ($Mode -ceq 'recover') {
  Invoke-ReleaseRecovery
  return
}
if ((Test-Path -LiteralPath $LockPath) -or
    (Test-Path -LiteralPath $StatePath) -or
    @(Get-ChildItem -LiteralPath $TaskParent -File | Where-Object {
      $_.Name -cmatch '^subgen-v050-release-state-[0-9a-f]{32}\.pending\.json$'
    }).Count -ne 0) {
  throw 'Stale release state must be safely recovered before a new run'
}
$RunToken = [Guid]::NewGuid().ToString('N')
$StateTempPath = Join-Path $TaskParent `
  "$StateTempPrefix$RunToken.pending.json"
$DockerConfigRoot = Join-Path $TaskParent "subgen-v050-docker-config-$RunToken"
$SmokeRoot = Join-Path $TaskParent "subgen-v050-release-$RunToken"
$AnonymousRunRoot = "$AnonymousStateParent/$RunToken"
$AnonymousConfigRoot = "$AnonymousRunRoot/docker-config"
$SmokeCidFile = "$AnonymousRunRoot/smoke.cid"
$SmokeName = "subgen-v050-release-$RunToken"
$MediaVolume = "subgen-v050-media-$RunToken"
$ModelsVolume = "subgen-v050-models-$RunToken"
$MonitorVolume = "subgen-v050-monitor-$RunToken"
$SmokeId = $null
$LoggedIn = $false
$ImagePullOwnershipDurable = $false
$AnonymousDockerPrefix = @(
  '-d', $AnonymousDistro, '-u', 'root', '--', '/usr/bin/env', '-i'
  'HOME=/root', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin'
  "DOCKER_CONFIG=$AnonymousConfigRoot", '/usr/bin/docker', '--host', $AnonymousSocket
)
function Write-ReleaseState {
  param(
    [Parameter(Mandatory)][System.Collections.IDictionary]$Value,
    [switch]$CreateOnly
  )
  if (Test-Path -LiteralPath $StateTempPath) {
    throw 'Release-state temporary path already exists'
  }
  $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
    ($Value | ConvertTo-Json -Depth 8 -Compress))
  $stream = [IO.FileStream]::new(
    $StateTempPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
    [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
  try {
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
  }
  finally {
    $stream.Dispose()
  }
  Set-OwnerOnlyAcl -Path $StateTempPath
  if ($CreateOnly) {
    if (Test-Path -LiteralPath $StatePath) { throw 'Release state appeared concurrently' }
    [IO.File]::Move($StateTempPath, $StatePath)
  } else {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
      throw 'Release state disappeared during mutation'
    }
    [IO.File]::Move($StateTempPath, $StatePath, $true)
  }
  if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    throw 'Release state was not durably replaced'
  }
  Assert-OwnerOnlyAcl -Path $StatePath
}
function Write-ReleaseLock {
  $lockValue = [ordered]@{
    schema = 'subgen.release-lock/v1'; run_token = $RunToken; process_id = $PID
  }
  $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
    ($lockValue | ConvertTo-Json -Compress))
  $stream = [IO.FileStream]::new(
    $LockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
    [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
  try {
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
  }
  finally { $stream.Dispose() }
  Set-OwnerOnlyAcl -Path $LockPath
}
Write-ReleaseLock
$runningWslRaw = @(& wsl.exe --list --running --quiet)
if ($LASTEXITCODE -ne 0) { throw 'Unable to capture pre-task WSL state' }
$runningWsl = @($runningWslRaw | ForEach-Object {
  ([string]$_).Replace("`0", '').Trim()
} | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$AnonymousWasRunning = $runningWsl -ccontains $AnonymousDistro
$PrimaryEngineId = @(& docker.exe info --format '{{.ID}}')
if ($LASTEXITCODE -ne 0 -or $PrimaryEngineId.Count -ne 1) {
  throw 'Unable to bind primary Docker Engine ID'
}
function Assert-TaskPath([string]$Path, [string]$LeafPrefix) {
  $full = [IO.Path]::GetFullPath($Path)
  $parent = [IO.Path]::GetFullPath($TaskParent).TrimEnd('\') + '\'
  if (-not $full.StartsWith($parent, [StringComparison]::OrdinalIgnoreCase) -or
      -not [IO.Path]::GetFileName($full).StartsWith($LeafPrefix,
        [StringComparison]::Ordinal)) { throw 'Unsafe task path' }
  $full
}
$DockerConfigRoot = Assert-TaskPath $DockerConfigRoot 'subgen-v050-docker-config-'
$SmokeRoot = Assert-TaskPath $SmokeRoot 'subgen-v050-release-'
if ($AnonymousRunRoot -cnotmatch
      '^/root/\.local/state/subgen-v050-release/[0-9a-f]{32}$' -or
    $AnonymousConfigRoot -cne "$AnonymousRunRoot/docker-config") {
  throw 'Unsafe WSL release path'
}
$state = [ordered]@{
  schema = 'subgen.release-state/v2'; run_token = $RunToken; phase = 'intent'
  process_id = $PID; primary_engine_id = $PrimaryEngineId[0].Trim()
  anonymous_transport = 'wsl'; anonymous_distro = $AnonymousDistro
  anonymous_socket = $AnonymousSocket; anonymous_server_version = $AnonymousServerVersion
  anonymous_was_running = $AnonymousWasRunning; anonymous_engine_id = $null
  docker_config_root = $DockerConfigRoot; smoke_root = $SmokeRoot
  anonymous_run_root = $AnonymousRunRoot; anonymous_config_root = $AnonymousConfigRoot
  smoke_cidfile = $SmokeCidFile; smoke_name = $SmokeName
  wsl_process_baseline = $null
  lock_path = $LockPath; state_temp_path = $StateTempPath
  image_pull_ref = $null; published_ref = $null
  smoke_id = $null; created_volumes = @()
  volume_names = [ordered]@{
    media = $MediaVolume; models = $ModelsVolume; monitor = $MonitorVolume
  }
}
Write-ReleaseState -Value $state -CreateOnly

$anonymousFactsRaw = @(& wsl.exe @AnonymousDockerPrefix info `
  --format '{{json .}}')
if ($LASTEXITCODE -ne 0 -or $anonymousFactsRaw.Count -ne 1) {
  throw 'Unable to bind the WSL Docker Engine'
}
$anonymousFacts = $anonymousFactsRaw[0] | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$anonymousFacts.ID) -or
    $anonymousFacts.OSType -cne 'linux' -or
    $anonymousFacts.ServerVersion -cne $AnonymousServerVersion -or
    $anonymousFacts.ID -ceq $PrimaryEngineId[0].Trim()) {
  throw 'WSL Docker identity/version is not the verified distinct boundary'
}
$socketFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
  -c '%F,%u,%g,%a' -- /var/run/docker.sock)
if ($LASTEXITCODE -ne 0 -or $socketFacts.Count -ne 1 -or
    $socketFacts[0].Trim() -cnotmatch '^socket,0,[0-9]+,660$') {
  throw 'WSL Docker endpoint is not the expected root-owned mode-0660 Unix socket'
}
$tcpListeners = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/ss -H -ltnp)
if ($LASTEXITCODE -ne 0 -or @($tcpListeners | Where-Object {
      $_ -match '(?i)\bdockerd\b'
    }).Count -ne 0) { throw 'WSL dockerd has or may have a TCP listener' }
$state.anonymous_engine_id = $anonymousFacts.ID
$state.phase = 'engine_bound'
Write-ReleaseState -Value $state
$state.wsl_process_baseline = @(Get-WslProcessSnapshot)
$state.phase = 'process_baseline_bound'
Write-ReleaseState -Value $state

$anonymousContainers = @(& wsl.exe @AnonymousDockerPrefix ps -aq)
$containersExit = $LASTEXITCODE
$anonymousImages = @(& wsl.exe @AnonymousDockerPrefix image ls -aq)
$imagesExit = $LASTEXITCODE
$anonymousVolumes = @(& wsl.exe @AnonymousDockerPrefix volume ls -q)
$volumesExit = $LASTEXITCODE
if ($containersExit -ne 0 -or $imagesExit -ne 0 -or $volumesExit -ne 0 -or
    $anonymousContainers.Count -ne 0 -or $anonymousImages.Count -ne 0 -or
    $anonymousVolumes.Count -ne 0) { throw 'WSL Docker Engine is not empty' }
& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test '!' -e $AnonymousRunRoot
if ($LASTEXITCODE -ne 0) { throw 'WSL release path already exists' }
& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/install -d -m 0700 `
  -o root -g root -- $AnonymousStateParent
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect WSL release-state parent' }
& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/mkdir -m 0700 -- $AnonymousRunRoot
if ($LASTEXITCODE -ne 0) { throw 'Unable to create WSL release run root' }
& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/mkdir -m 0700 -- $AnonymousConfigRoot
if ($LASTEXITCODE -ne 0) { throw 'Unable to create WSL anonymous Docker config' }
$wslPathFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
  -c '%F,%u,%g,%a' -- $AnonymousRunRoot $AnonymousConfigRoot)
$wslRealPaths = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/realpath `
  -e -- $AnonymousRunRoot $AnonymousConfigRoot)
if ($LASTEXITCODE -ne 0 -or $wslPathFacts.Count -ne 2 -or
    @($wslPathFacts | Where-Object { $_.Trim() -cne 'directory,0,0,700' }).Count -ne 0 -or
    $wslRealPaths.Count -ne 2 -or $wslRealPaths[0].Trim() -cne $AnonymousRunRoot -or
    $wslRealPaths[1].Trim() -cne $AnonymousConfigRoot) {
  throw 'WSL anonymous Docker config confinement failed'
}
$anonymousConfigEntries = @(& wsl.exe -d $AnonymousDistro -u root -- `
  /usr/bin/find $AnonymousConfigRoot -mindepth 1 -maxdepth 1 -print -quit)
if ($LASTEXITCODE -ne 0 -or $anonymousConfigEntries.Count -ne 0) {
  throw 'WSL anonymous Docker config is not empty'
}
$state.phase = 'wsl_config_created'
Write-ReleaseState -Value $state

New-Item -ItemType Directory -Path $DockerConfigRoot,$SmokeRoot | Out-Null
Set-OwnerOnlyAcl -Path $DockerConfigRoot -Directory
Set-OwnerOnlyAcl -Path $SmokeRoot -Directory
$state.phase = 'windows_directories_created'
Write-ReleaseState -Value $state
$env:DOCKER_CONFIG = $DockerConfigRoot
try {
  $IdentityRoot = [string]$env:SUBGEN_TASK11A_IDENTITY_ROOT
  if ([string]::IsNullOrWhiteSpace($IdentityRoot)) {
    throw 'SUBGEN_TASK11A_IDENTITY_ROOT is required'
  }
  $identityPath = Join-Path $IdentityRoot 'image-identity.json'
  $configPath = Join-Path $IdentityRoot 'image-config-final.json'
  $identity = Get-Content -Raw -LiteralPath $identityPath | ConvertFrom-Json
  $imageConfig = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
  $expectedLayers = @($identity.image_identity.layer_diff_ids)
  if ($identity.image_identity.config_digest -cne $ExpectedConfig -or
      ('sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $configPath).Hash.ToLower()) -cne
        $ExpectedConfig -or
      $imageConfig.config.Labels.'org.opencontainers.image.revision' -cne $RuntimeCommit) {
    throw 'Sealed candidate identity mismatch'
  }
  $actualIndex = @(& docker.exe image inspect $Candidate --format '{{.Id}}')
  $actualLayersRaw = @(& docker.exe image inspect $Candidate --format '{{json .RootFS.Layers}}')
  if ($LASTEXITCODE -ne 0 -or $actualIndex.Count -ne 1 -or
      $actualIndex[0].Trim() -cne $ExpectedIndex -or $actualLayersRaw.Count -ne 1) {
    throw 'Candidate changed before publication'
  }
  $actualLayers = @($actualLayersRaw[0] | ConvertFrom-Json)
  if ($actualLayers.Count -ne $expectedLayers.Count) { throw 'Candidate layer count changed' }
  for ($i = 0; $i -lt $expectedLayers.Count; $i++) {
    if ($actualLayers[$i] -cne $expectedLayers[$i]) {
      throw "Candidate ordered layer mismatch at index $i"
    }
  }
  $token = [Console]::In.ReadLine()
  if ([string]::IsNullOrWhiteSpace($token)) { throw 'Missing registry token' }
  $token | & docker.exe login ghcr.io --username Herbertmt978 --password-stdin
  $token = $null
  if ($LASTEXITCODE -ne 0) { throw 'GHCR login failed' }
  $LoggedIn = $true
  if ([string]::IsNullOrWhiteSpace($VersionBeforeDigest)) {
    $versionProbeError = Join-Path $SmokeRoot 'version-probe.err'
    $unexpectedVersion = @(& docker.exe buildx imagetools inspect $VersionRef `
      --format '{{.Manifest.Digest}}' 2>$versionProbeError)
    $versionProbeExit = $LASTEXITCODE
    if ($versionProbeExit -eq 0 -or $unexpectedVersion.Count -ne 0) {
      throw 'GHCR v0.5.0 appeared after the absent-state preflight'
    }
    $versionProbeText = if (Test-Path -LiteralPath $versionProbeError) {
      Get-Content -Raw -LiteralPath $versionProbeError
    } else { '' }
    if ($versionProbeText -notmatch
        '(?i)(manifest unknown|subgen-english-plex:v0\.5\.0:\s+not found)') {
      throw 'GHCR v0.5.0 lookup failed without authoritative absence'
    }
    # The version tag is immutable. The only authorized writer is the tested
    # registry client whose disposable two-writer race proved atomic
    # create-if-absent for this GHCR endpoint and credential path.
    $atomicPublisher = Get-Command Invoke-ProvenAtomicRegistryCreate `
      -CommandType Function -ErrorAction SilentlyContinue
    if ($null -eq $atomicPublisher -or -not $RegistryAtomicCapabilityProved) {
      throw 'No proven GHCR create-if-absent publisher is active; ordinary version push is forbidden'
    }
    try {
      Invoke-ProvenAtomicRegistryCreate -Reference $VersionRef `
        -ExpectedDigest $ExpectedIndex -Candidate $Candidate `
        -CapabilitySeal $RegistryAtomicCapabilitySeal
    }
    finally {
      Assert-HostedRunSetUnchanged 'conditional GHCR v0.5.0 create'
    }
  } elseif ($VersionBeforeDigest -cne $ExpectedIndex) {
    throw 'GHCR version preflight input is not absent-or-exact'
  }
  $versionDigest = @(& docker.exe buildx imagetools inspect $VersionRef --format '{{.Manifest.Digest}}')
  if ($LASTEXITCODE -ne 0 -or $versionDigest.Count -ne 1 -or
      $versionDigest[0].Trim() -cne $ExpectedIndex) { throw 'Version digest mismatch' }
  $PublishedRef = "ghcr.io/herbertmt978/subgen-english-plex@$($versionDigest[0].Trim())"
  $state.image_pull_ref = $PublishedRef
  $state.phase = 'image_pull_intent'
  Write-ReleaseState -Value $state
  $ImagePullOwnershipDurable = $true
  & wsl.exe @AnonymousDockerPrefix pull $PublishedRef
  if ($LASTEXITCODE -ne 0) { throw 'Clean anonymous digest pull failed' }
  $state.published_ref = $PublishedRef
  $state.phase = 'image_pulled'
  Write-ReleaseState -Value $state
  $pulledConfig = @(& wsl.exe @AnonymousDockerPrefix image inspect `
    $PublishedRef --format '{{.Id}}')
  if ($LASTEXITCODE -ne 0 -or $pulledConfig.Count -ne 1 -or
      $pulledConfig[0].Trim() -cne $ExpectedConfig) { throw 'Anonymous pulled config mismatch' }
  $pulledLabelsRaw = @(& wsl.exe @AnonymousDockerPrefix image inspect `
    $PublishedRef --format '{{json .Config.Labels}}')
  if ($LASTEXITCODE -ne 0 -or $pulledLabelsRaw.Count -ne 1) {
    throw 'Pulled label inspect failed'
  }
  $pulledLabels = $pulledLabelsRaw[0] | ConvertFrom-Json
  if ($pulledLabels.'org.opencontainers.image.revision' -cne $RuntimeCommit) {
    throw 'Pulled revision mismatch'
  }
  $pulledLayersRaw = @(& wsl.exe @AnonymousDockerPrefix image inspect `
    $PublishedRef --format '{{json .RootFS.Layers}}')
  if ($LASTEXITCODE -ne 0 -or $pulledLayersRaw.Count -ne 1) {
    throw 'Pulled layer inspect failed'
  }
  $pulledLayers = @($pulledLayersRaw[0] | ConvertFrom-Json)
  if ($pulledLayers.Count -ne $expectedLayers.Count) {
    throw 'Pulled layer count mismatch'
  }
  for ($i = 0; $i -lt $expectedLayers.Count; $i++) {
    if ($pulledLayers[$i] -cne $expectedLayers[$i]) {
      throw "Pulled ordered layer mismatch at index $i"
    }
  }

  foreach ($Volume in @($MediaVolume,$ModelsVolume,$MonitorVolume)) {
    $createdVolume = @(& wsl.exe @AnonymousDockerPrefix volume create `
      --label io.github.herbertmt978.subgen.release-token=$RunToken $Volume)
    if ($LASTEXITCODE -ne 0 -or $createdVolume.Count -ne 1 -or
        $createdVolume[0].Trim() -cne $Volume) { throw 'Anonymous smoke volume creation failed' }
    $volumeLabel = @(& wsl.exe @AnonymousDockerPrefix volume inspect $Volume `
      --format '{{index .Labels "io.github.herbertmt978.subgen.release-token"}}')
    if ($LASTEXITCODE -ne 0 -or $volumeLabel.Count -ne 1 -or
        $volumeLabel[0].Trim() -cne $RunToken) { throw 'Anonymous volume ownership mismatch' }
    $state.created_volumes = @($state.created_volumes) + $Volume
    $state.phase = 'volume_created'
    Write-ReleaseState -Value $state
  }
  $existing = @(& wsl.exe @AnonymousDockerPrefix ps -aq --filter "name=^/$SmokeName$")
  if ($LASTEXITCODE -ne 0 -or $existing.Count -ne 0) { throw 'Smoke name collision' }
  & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test '!' -e $SmokeCidFile
  if ($LASTEXITCODE -ne 0) { throw 'Smoke cidfile path already exists' }
  $createdSmoke = @(& wsl.exe @AnonymousDockerPrefix create `
    --cidfile $SmokeCidFile --name $SmokeName --restart=no `
    --label io.github.herbertmt978.subgen.release-smoke=true `
    --label "io.github.herbertmt978.subgen.release-token=$RunToken" `
    --memory=9g --memory-swap=9g --cpus=4 `
    -e MONITOR=False -e PROCESS_ADDED_MEDIA=False -e SKIP_STARTUP_SCAN=True `
    -e AUTO_DELETE_FAILED_FILES=false -e AUTO_DELETE_INVALID_MEDIA=false `
    -e TRANSCRIBE_FOLDERS=/media -e MODEL_PATH=/subgen/models `
    --mount "type=volume,src=$MediaVolume,dst=/media,readonly" `
    --mount "type=volume,src=$ModelsVolume,dst=/subgen/models" `
    --mount "type=volume,src=$MonitorVolume,dst=/opt/subgen/monitor" $PublishedRef)
  $createExit = $LASTEXITCODE
  $cidFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
    -c '%F,%u,%g,%a' -- $SmokeCidFile)
  $cidLines = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/cat -- $SmokeCidFile)
  if ($LASTEXITCODE -ne 0 -or $createExit -ne 0 -or $createdSmoke.Count -ne 1 -or
      $createdSmoke[0].Trim() -cnotmatch '^[0-9a-f]{64}$' -or
      $cidFacts.Count -ne 1 -or
      $cidFacts[0].Trim() -cnotmatch '^regular file,0,0,(600|640|644)$' -or
      $cidLines.Count -ne 1 -or
      $cidLines[0].Trim() -cne $createdSmoke[0].Trim()) { throw 'Smoke create/cidfile binding failed' }
  $SmokeId = $cidLines[0].Trim()
  $state.smoke_id = $SmokeId
  $state.phase = 'container_created'
  Write-ReleaseState -Value $state
  $createdFactsRaw = @(& wsl.exe @AnonymousDockerPrefix inspect $SmokeId `
    --format '{{json .}}')
  if ($LASTEXITCODE -ne 0 -or $createdFactsRaw.Count -ne 1) {
    throw 'Stopped smoke inspect failed'
  }
  $createdFacts = $createdFactsRaw[0] | ConvertFrom-Json
  if ($createdFacts.State.Status -cne 'created' -or
      $createdFacts.Image -cne $ExpectedConfig -or
      $createdFacts.Config.Labels.'io.github.herbertmt978.subgen.release-smoke' -cne 'true' -or
      $createdFacts.Config.Labels.'io.github.herbertmt978.subgen.release-token' -cne $RunToken) {
    throw 'Stopped smoke boundary mismatch'
  }
  & wsl.exe @AnonymousDockerPrefix start $SmokeId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Smoke start failed' }
  $state.phase = 'container_started'
  Write-ReleaseState -Value $state
  $deadline = [DateTime]::UtcNow.AddSeconds(90)
  do {
    & wsl.exe @AnonymousDockerPrefix exec $SmokeId /usr/bin/python3 -c `
      "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:9000/status',timeout=3); raise SystemExit(0 if r.status == 200 else 1)" `
      2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    if ([DateTime]::UtcNow -ge $deadline) { throw 'In-container HTTP smoke timed out' }
    Start-Sleep -Seconds 2
  } while ($true)
  $restarts = @(& wsl.exe @AnonymousDockerPrefix inspect $SmokeId --format '{{.RestartCount}}')
  if ($LASTEXITCODE -ne 0 -or $restarts.Count -ne 1 -or
      $restarts[0].Trim() -cne '0') { throw 'Smoke restarted' }
  $ownedRaw = @(& wsl.exe @AnonymousDockerPrefix inspect $SmokeId `
    --format '{{json .Config.Labels}}')
  if ($LASTEXITCODE -ne 0 -or $ownedRaw.Count -ne 1) {
    throw 'Smoke ownership inspect failed'
  }
  $ownedLabels = $ownedRaw[0] | ConvertFrom-Json
  $ownedImage = @(& wsl.exe @AnonymousDockerPrefix inspect $SmokeId --format '{{.Image}}')
  if ($LASTEXITCODE -ne 0 -or $ownedImage.Count -ne 1 -or
      $ownedImage[0].Trim() -cne $ExpectedConfig -or
      $ownedLabels.'io.github.herbertmt978.subgen.release-smoke' -cne 'true' -or
      $ownedLabels.'io.github.herbertmt978.subgen.release-token' -cne $RunToken) {
    throw 'Smoke immutable ownership mismatch'
  }
  # Stop and remove this revalidated immutable ID before publishing latest.
  & wsl.exe @AnonymousDockerPrefix stop --time 30 $SmokeId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Smoke stop failed' }
  $state.phase = 'container_stopped'
  Write-ReleaseState -Value $state
  & wsl.exe @AnonymousDockerPrefix rm $SmokeId | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Smoke removal failed' }
  & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rm -- $SmokeCidFile
  if ($LASTEXITCODE -ne 0) { throw 'Smoke cidfile removal failed' }
  $SmokeId = $null
  $state.smoke_id = $null
  $state.phase = 'container_removed'
  Write-ReleaseState -Value $state
  foreach ($Volume in @($MediaVolume,$ModelsVolume,$MonitorVolume)) {
    $volumeLabel = @(& wsl.exe @AnonymousDockerPrefix volume inspect $Volume `
      --format '{{index .Labels "io.github.herbertmt978.subgen.release-token"}}')
    if ($LASTEXITCODE -ne 0 -or $volumeLabel.Count -ne 1 -or
        $volumeLabel[0].Trim() -cne $RunToken) { throw 'Volume cleanup ownership mismatch' }
    & wsl.exe @AnonymousDockerPrefix volume rm $Volume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Anonymous smoke volume removal failed' }
    $state.created_volumes = @($state.created_volumes | Where-Object { $_ -cne $Volume })
    $state.phase = 'volume_removed'
    Write-ReleaseState -Value $state
  }
  if (-not $ImagePullOwnershipDurable -or
      $state.image_pull_ref -cne $PublishedRef -or
      $state.phase -cne 'volume_removed') {
    throw 'Durable image ownership/phase proof is missing before removal'
  }
  & wsl.exe @AnonymousDockerPrefix image rm $PublishedRef | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Anonymous pulled image removal failed' }
  $state.image_pull_ref = $null
  $state.published_ref = $null
  $state.phase = 'image_removed'
  Write-ReleaseState -Value $state
  $ImagePullOwnershipDurable = $false
  $remainingContainers = @(& wsl.exe @AnonymousDockerPrefix ps -aq)
  $remainingContainersExit = $LASTEXITCODE
  $remainingImages = @(& wsl.exe @AnonymousDockerPrefix image ls -aq)
  $remainingImagesExit = $LASTEXITCODE
  $remainingVolumes = @(& wsl.exe @AnonymousDockerPrefix volume ls -q)
  $remainingVolumesExit = $LASTEXITCODE
  if ($remainingContainersExit -ne 0 -or $remainingImagesExit -ne 0 -or
      $remainingVolumesExit -ne 0 -or $remainingContainers.Count -ne 0 -or
      $remainingImages.Count -ne 0 -or $remainingVolumes.Count -ne 0) {
    throw 'Anonymous Docker Engine did not return empty'
  }
  $state.phase = 'anonymous_engine_clean'
  Write-ReleaseState -Value $state
}
finally {
  $token = $null
  $cleanupFailure = $null
  if ($null -ne $SmokeId) {
    # Inspect by full ID and remove only if image plus both private labels match.
    $owned = @(& wsl.exe @AnonymousDockerPrefix inspect $SmokeId `
      --format '{{json .Config.Labels}}' 2>$null)
    if ($LASTEXITCODE -eq 0 -and $owned.Count -eq 1) {
      $ownedLabels = $owned[0] | ConvertFrom-Json
      $cleanupImage = @(& wsl.exe @AnonymousDockerPrefix inspect `
        $SmokeId --format '{{.Image}}' 2>$null)
      if ($LASTEXITCODE -eq 0 -and $cleanupImage.Count -eq 1 -and
          $cleanupImage[0].Trim() -ceq $ExpectedConfig -and
          $ownedLabels.'io.github.herbertmt978.subgen.release-smoke' -ceq 'true' -and
          $ownedLabels.'io.github.herbertmt978.subgen.release-token' -ceq $RunToken) {
        & wsl.exe @AnonymousDockerPrefix stop --time 30 $SmokeId 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
          # Revalidate the same immutable ID before escalation.
          $again = @(& wsl.exe @AnonymousDockerPrefix inspect $SmokeId `
            --format '{{.Image}} {{index .Config.Labels "io.github.herbertmt978.subgen.release-token"}}' 2>$null)
          if ($LASTEXITCODE -eq 0 -and $again.Count -eq 1 -and
              $again[0].Trim() -ceq "$ExpectedConfig $RunToken") {
            & wsl.exe @AnonymousDockerPrefix kill $SmokeId 2>$null | Out-Null
          }
        }
        $running = @(& wsl.exe @AnonymousDockerPrefix inspect `
          $SmokeId --format '{{.State.Running}}' 2>$null)
        if ($LASTEXITCODE -ne 0 -or $running.Count -ne 1 -or
            $running[0].Trim() -cne 'false') {
          $cleanupFailure = 'Smoke cleanup could not prove stopped'
        } else {
          & wsl.exe @AnonymousDockerPrefix rm $SmokeId 2>$null | Out-Null
          if ($LASTEXITCODE -ne 0) {
            $cleanupFailure = 'Smoke removal failed'
          } else {
            $cidFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
              -c '%F,%u,%g,%a' -- $SmokeCidFile 2>$null)
            $cidLines = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/cat `
              -- $SmokeCidFile 2>$null)
            if ($LASTEXITCODE -ne 0 -or $cidFacts.Count -ne 1 -or
                $cidFacts[0].Trim() -cnotmatch '^regular file,0,0,(600|640|644)$' -or
                $cidLines.Count -ne 1 -or $cidLines[0].Trim() -cne $SmokeId) {
              $cleanupFailure = 'Smoke cidfile cleanup identity mismatch'
            } else {
              & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rm -- $SmokeCidFile
              if ($LASTEXITCODE -ne 0) {
                $cleanupFailure = 'Smoke cidfile cleanup failed'
              } else {
                $SmokeId = $null
                $state.smoke_id = $null
                $state.phase = 'cleanup_container_removed'
                Write-ReleaseState -Value $state
              }
            }
          }
        }
      } else {
        $cleanupFailure = 'Smoke cleanup ownership mismatch'
      }
    } else {
      $cleanupFailure = 'Smoke cleanup target could not be inspected'
    }
  }
  foreach ($Volume in @($MediaVolume,$ModelsVolume,$MonitorVolume)) {
    $volumeLabels = @(& wsl.exe @AnonymousDockerPrefix volume inspect $Volume `
      --format '{{json .Labels}}' 2>$null)
    if ($LASTEXITCODE -eq 0 -and $volumeLabels.Count -eq 1) {
      $labels = $volumeLabels[0] | ConvertFrom-Json
      if ($labels.'io.github.herbertmt978.subgen.release-token' -ceq $RunToken) {
        & wsl.exe @AnonymousDockerPrefix volume rm $Volume 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0 -and $null -eq $cleanupFailure) {
          $cleanupFailure = 'Anonymous smoke volume cleanup failed'
        } elseif ($LASTEXITCODE -eq 0) {
          $state.created_volumes = @($state.created_volumes | Where-Object {
            $_ -cne $Volume
          })
          $state.phase = 'cleanup_volume_removed'
          Write-ReleaseState -Value $state
        }
      } elseif ($null -eq $cleanupFailure) {
        $cleanupFailure = 'Anonymous smoke volume ownership mismatch'
      }
    }
  }
  $anonymousImage = @(& wsl.exe @AnonymousDockerPrefix image inspect `
    "ghcr.io/herbertmt978/subgen-english-plex@$ExpectedIndex" `
    --format '{{.Id}}' 2>$null)
  if ($LASTEXITCODE -eq 0 -and $anonymousImage.Count -eq 1) {
    $cleanupOwnedImagePhases = @('image_pull_intent','image_pulled','volume_created',
      'container_created','container_started','container_stopped','container_removed',
      'volume_removed','cleanup_container_removed','cleanup_volume_removed')
    if ($anonymousImage[0].Trim() -ceq $ExpectedConfig -and
        $ImagePullOwnershipDurable -and
        $state.image_pull_ref -ceq
          "ghcr.io/herbertmt978/subgen-english-plex@$ExpectedIndex" -and
        $state.phase -cin $cleanupOwnedImagePhases) {
      & wsl.exe @AnonymousDockerPrefix image rm `
        "ghcr.io/herbertmt978/subgen-english-plex@$ExpectedIndex" 2>$null | Out-Null
      if ($LASTEXITCODE -ne 0 -and $null -eq $cleanupFailure) {
        $cleanupFailure = 'Anonymous image cleanup failed'
      } elseif ($LASTEXITCODE -eq 0) {
        $state.image_pull_ref = $null
        $state.published_ref = $null
        $state.phase = 'cleanup_image_removed'
        Write-ReleaseState -Value $state
        $ImagePullOwnershipDurable = $false
      }
    } elseif ($null -eq $cleanupFailure) {
      $cleanupFailure = 'Anonymous image cleanup identity mismatch'
    }
  }
  $remainingContainers = @(& wsl.exe @AnonymousDockerPrefix ps -aq)
  $remainingContainersExit = $LASTEXITCODE
  $remainingImages = @(& wsl.exe @AnonymousDockerPrefix image ls -aq)
  $remainingImagesExit = $LASTEXITCODE
  $remainingVolumes = @(& wsl.exe @AnonymousDockerPrefix volume ls -q)
  $remainingVolumesExit = $LASTEXITCODE
  if (($remainingContainersExit -ne 0 -or $remainingImagesExit -ne 0 -or
       $remainingVolumesExit -ne 0 -or $remainingContainers.Count -ne 0 -or
       $remainingImages.Count -ne 0 -or $remainingVolumes.Count -ne 0) -and
      $null -eq $cleanupFailure) {
    $cleanupFailure = 'WSL Docker Engine cleanup did not return empty'
  }
  if ($null -eq $cleanupFailure) {
    $wslPathFacts = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/stat `
      -c '%F,%u,%g,%a' -- $AnonymousRunRoot $AnonymousConfigRoot 2>$null)
    $wslRealPaths = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/realpath `
      -e -- $AnonymousRunRoot $AnonymousConfigRoot 2>$null)
    $runEntries = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/find `
      $AnonymousRunRoot -mindepth 1 -maxdepth 1 -printf '%f\n' 2>$null)
    if ($LASTEXITCODE -ne 0 -or $wslPathFacts.Count -ne 2 -or
        @($wslPathFacts | Where-Object { $_.Trim() -cne 'directory,0,0,700' }).Count -ne 0 -or
        $wslRealPaths.Count -ne 2 -or $wslRealPaths[0].Trim() -cne $AnonymousRunRoot -or
        $wslRealPaths[1].Trim() -cne $AnonymousConfigRoot -or
        $runEntries.Count -ne 1 -or $runEntries[0].Trim() -cne 'docker-config') {
      $cleanupFailure = 'WSL task-path cleanup identity mismatch'
    } else {
      & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rm -rf -- $AnonymousConfigRoot
      if ($LASTEXITCODE -eq 0) {
        & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/rmdir -- $AnonymousRunRoot
      }
      & wsl.exe -d $AnonymousDistro -u root -- /usr/bin/test '!' -e $AnonymousRunRoot
      if ($LASTEXITCODE -ne 0) {
        $cleanupFailure = 'WSL task-path cleanup failed'
      } else {
        $state.phase = 'wsl_paths_removed'
        Write-ReleaseState -Value $state
      }
    }
  }
  $logoutConfigOwned = $false
  try {
    $logoutConfigOwned = Assert-RecoveryWindowsDirectory $DockerConfigRoot `
      $DockerConfigRoot
  }
  catch {
    if ($null -eq $cleanupFailure) {
      $cleanupFailure = 'Docker config ACL/ownership changed before logout'
    }
  }
  if ($LoggedIn -and $logoutConfigOwned) {
    $env:DOCKER_CONFIG = $DockerConfigRoot
    & docker.exe logout ghcr.io 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -and $null -eq $cleanupFailure) {
      $cleanupFailure = 'Task-scoped registry logout failed'
    }
  } elseif ($LoggedIn -and $null -eq $cleanupFailure) {
    $cleanupFailure = 'Task-scoped registry config is absent or unsafe'
  }
  try {
    $smokeRootOwned = Assert-RecoveryWindowsDirectory $SmokeRoot $SmokeRoot
    $dockerConfigOwned = Assert-RecoveryWindowsDirectory $DockerConfigRoot `
      $DockerConfigRoot
    if ($smokeRootOwned) {
      Remove-Item -LiteralPath $SmokeRoot -Recurse -Force
    }
    if ($dockerConfigOwned) {
      Remove-Item -LiteralPath $DockerConfigRoot -Recurse -Force
    }
  }
  catch {
    if ($null -eq $cleanupFailure) {
      $cleanupFailure = 'Task directory ACL/ownership cleanup failed'
    }
  }
  Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
  if ((Test-Path -LiteralPath $SmokeRoot) -or
      (Test-Path -LiteralPath $DockerConfigRoot)) {
    $cleanupFailure = 'Task directory cleanup failed'
  }
  if ($null -eq $cleanupFailure -and -not $AnonymousWasRunning) {
    $currentProcesses = @(Get-WslProcessSnapshot)
    $baselineProcesses = @($state.wsl_process_baseline)
    if ($baselineProcesses.Count -eq 0 -or
        @(Compare-Object -CaseSensitive $baselineProcesses $currentProcesses).Count -ne 0) {
      $cleanupFailure = 'Unrelated WSL process change blocks termination'
    } else {
      $otherWslRuns = @(& wsl.exe -d $AnonymousDistro -u root -- /usr/bin/find `
        $AnonymousStateParent -mindepth 1 -maxdepth 1 -print -quit)
      if ($LASTEXITCODE -ne 0 -or $otherWslRuns.Count -ne 0) {
        $cleanupFailure = 'WSL distribution has another task-owned run'
      } else {
        $finalProcesses = @(Get-WslProcessSnapshot)
        if (@(Compare-Object -CaseSensitive $baselineProcesses `
              $finalProcesses).Count -ne 0) {
          $cleanupFailure = 'WSL process set changed immediately before termination'
        } else {
          & wsl.exe --terminate $AnonymousDistro
          if ($LASTEXITCODE -ne 0) {
            $cleanupFailure = 'Unable to restore stopped WSL state'
          } else {
            $runningAfterRaw = @(& wsl.exe --list --running --quiet)
            $runningAfter = @($runningAfterRaw | ForEach-Object {
              ([string]$_).Replace("`0", '').Trim()
            } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            if ($LASTEXITCODE -ne 0 -or $runningAfter -ccontains $AnonymousDistro) {
              $cleanupFailure = 'WSL distribution remained running after restore'
            }
          }
        }
      }
    }
  }
  if ($null -eq $cleanupFailure) {
    if (Test-Path -LiteralPath $StateTempPath) {
      $cleanupFailure = 'Pending release-state replacement remained'
    } else {
      Assert-OwnerOnlyAcl -Path $StatePath
      Remove-Item -LiteralPath $StatePath -Force
    }
    if ($null -eq $cleanupFailure -and
        (Test-Path -LiteralPath $StatePath)) {
      $cleanupFailure = 'Release state cleanup failed'
    }
    if ($null -eq $cleanupFailure) {
      Assert-OwnerOnlyAcl -Path $LockPath
      Remove-Item -LiteralPath $LockPath -Force
      if (Test-Path -LiteralPath $LockPath) {
        $cleanupFailure = 'Release lock cleanup failed'
      }
    }
  }
  if ($null -ne $cleanupFailure) { throw $cleanupFailure }
}
```

Only after the version digest and clean anonymous HTTP checks pass, reuse the
owner-only, exact-blob release body and authoritative REST helper established
before publication. Query again immediately before create. A release that
appears concurrently is resumable only when a fresh HTTP 200 is exact; do not
edit a mismatch. The complete hosted-run set is asserted in `finally`, even if
the create response is lost:

```powershell
$releaseNow = Get-AuthoritativeReleaseState
if ($releaseNow.status -eq 200) {
  Assert-ExactRelease200 $releaseNow
} elseif ($releaseNow.status -eq 404) {
  # This is the immediate pre-create query. Only this exact HTTP 404 authorizes
  # one create attempt; any other failure was already rejected by the helper.
  $createExit = $null
  try {
    & gh release create v0.5.0 --repo Herbertmt978/Subgen-English-Plex `
      --verify-tag --title 'Subgen English Plex v0.5.0' `
      --notes-file $ReleaseBodyPath
    $createExit = $LASTEXITCODE
  }
  finally {
    Assert-HostedRunSetUnchanged 'GitHub release create'
  }
  $releaseAfterCreate = Get-AuthoritativeReleaseState
  Assert-ExactRelease200 $releaseAfterCreate
  if ($createExit -ne 0) {
    # A lost/error response is acceptable only because the authoritative
    # postcondition above proved the exact release.
    'GitHub release create response was nonzero but exact state is proven'
  }
} else {
  throw 'Fresh release state is neither exact HTTP 200 nor authoritative HTTP 404'
}
"release_body_sha256=$ReleaseBodySha256"
```

After the exact release is live, mutate `latest` as the final public write. Use
the same task-scoped credential transport and immutable expected image ID. Pass
the already proved release commit, annotated-tag object, and release-body hash
through `SUBGEN_RELEASE_COMMIT`, `SUBGEN_RELEASE_TAG_OBJECT`, and
`SUBGEN_RELEASE_BODY_SHA256`. Run the exact script below with
`SUBGEN_LATEST_MODE=publish`; after interruption run that same verified script
with `SUBGEN_LATEST_MODE=recover` before any retry.

Both modes hold one exclusive local lock and one fixed repository publication
lock. The repository lock is a unique annotated-tag object installed through
GitHub's atomic create-ref API; every cooperating publisher must acquire it,
and this process verifies that no hosted publication run is active. Publication
durably creates an owner-only v3 intent record before creating a credential
directory, publication lock, helper, or local/remote tag mutation. The record
contains the exact canonical Docker-config path, immutable Git/tag/release-body
identity, repository-lock object, registry-settlement result, and task-owned
push-helper/receipt identity. Publish and recover mechanically recheck those
identities before accepting or mutating registry state.

The helper waits behind a token-derived gate until its exact PID is committed,
writes an owner-only durable receipt, rechecks `latest` immediately before the
push, and records its post-push observation. Recovery refuses an active or
ambiguous helper and requires repeated stable remote observations before it
settles either the recorded prior digest or expected final digest. State
survives until registry state is settled, the helper is quiescent, registry
logout is proven, the exact credential/helper paths are absent, and the remote
publication lock is removed by its exact object ID.

GHCR does not expose a reliable compare-and-swap operation for mutable tags.
The repository lock prevents cooperating release processes from racing, and
the script checks for hosted work plus rechecks immediately before and after
the single mutation, but an uncoordinated external writer can still enter the final
check/push interval. Treat that bounded residual as a release blocker whenever
another publisher may exist; do not claim absolute non-overwrite. A third
digest observed at any check remains unresolved, retains state and lock, and is
never overwritten deliberately. Never delete a registry manifest to simulate
tag rollback. The immutable `v0.5.0` digest remains the deployment identity.

```powershell
$ErrorActionPreference = 'Stop'
$TaskParent = [IO.Path]::GetFullPath('D:\CodexTemp')
$Repository = 'Herbertmt978/Subgen-English-Plex'
$RepositoryRemote = 'origin'
$RepositoryImage = 'ghcr.io/herbertmt978/subgen-english-plex'
$VersionRef = $RepositoryImage + ':v0.5.0'
$LatestRef = $RepositoryImage + ':latest'
$ExpectedConfig = [string]$env:SUBGEN_TASK11A_CONFIG_DIGEST
$ExpectedIndex = [string]$env:SUBGEN_TASK11A_OCI_INDEX
$ReleaseNotesPath = 'docs/RELEASE_NOTES_0.5.0.md'
$RepositoryLockRef = 'refs/tags/subgen-v050-latest-publication-lock'
$Mode = [string]$env:SUBGEN_LATEST_MODE
$ReleaseCommit = [string]$env:SUBGEN_RELEASE_COMMIT
$ReleaseTagObject = [string]$env:SUBGEN_RELEASE_TAG_OBJECT
$ReleaseBodySha256 = [string]$env:SUBGEN_RELEASE_BODY_SHA256
if ($Mode -cnotin @('publish','recover')) {
  throw 'SUBGEN_LATEST_MODE must be exactly publish or recover'
}
if ($ExpectedConfig -ceq 'sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae' -or
    $ExpectedIndex -ceq 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc') {
  throw 'Pre-amendment candidate identity is retired'
}
if ($ReleaseCommit -cnotmatch '^[0-9a-f]{40}$' -or
    $ReleaseTagObject -cnotmatch '^[0-9a-f]{40}$' -or
    $ReleaseBodySha256 -cnotmatch '^[0-9a-f]{64}$') {
  throw 'Immutable release identity inputs are invalid'
}

$LatestStatePath = Join-Path $TaskParent 'subgen-v050-latest-state.json'
$LatestStateTempPath = $LatestStatePath + '.next'
$LatestLockPath = Join-Path $TaskParent 'subgen-v050-latest-state.lock'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$HelperSource = @'
param(
  [Parameter(Mandatory)][string]$RunToken,
  [Parameter(Mandatory)][string]$DockerConfigRoot,
  [Parameter(Mandatory)][string]$BirthPath,
  [Parameter(Mandatory)][string]$GatePath,
  [Parameter(Mandatory)][string]$ReceiptPath,
  [Parameter(Mandatory)][string]$HelperScriptPath,
  [Parameter(Mandatory)][string]$HelperScriptSha256,
  [Parameter(Mandatory)][string]$Repository,
  [Parameter(Mandatory)][string]$RepositoryRemote,
  [Parameter(Mandatory)][string]$RepositoryLockRef,
  [Parameter(Mandatory)][string]$RepositoryLockObject,
  [Parameter(Mandatory)][string]$ReleaseCommit,
  [Parameter(Mandatory)][string]$RepositoryImage,
  [Parameter(Mandatory)][string]$VersionRef,
  [Parameter(Mandatory)][string]$LatestRef,
  [Parameter(Mandatory)][string]$PriorDigest,
  [Parameter(Mandatory)][string]$ExpectedDigest
)
$ErrorActionPreference = 'Stop'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
function Write-ExclusiveJson {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Value)
  $next = $Path + '.next'
  if ((Test-Path -LiteralPath $Path) -or (Test-Path -LiteralPath $next)) {
    throw 'Helper record path is not exclusive'
  }
  $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
    ($Value | ConvertTo-Json -Depth 8 -Compress))
  $stream = [IO.FileStream]::new(
    $next, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
    [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
  try {
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush($true)
  }
  finally {
    $stream.Dispose()
  }
  & icacls.exe $next /inheritance:r /grant:r ($owner + ':F') | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Unable to protect helper record' }
  [IO.File]::Move($next, $Path)
}
function Assert-OwnerOnlyFile {
  param([Parameter(Mandatory)][string]$Path)
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.PSIsContainer -or
      ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      [IO.Path]::GetFullPath($item.FullName) -cne [IO.Path]::GetFullPath($Path)) {
    throw 'Helper input path is unsafe'
  }
  $acl = Get-Acl -LiteralPath $Path
  $foreign = @($acl.Access | Where-Object {
    $_.IdentityReference.Value -cne $owner -or
    $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow
  })
  if ($acl.Owner -cne $owner -or -not $acl.AreAccessRulesProtected -or
      $foreign.Count -ne 0) {
    throw 'Helper input ACL is not owner-only'
  }
}
function Get-RemoteDigest {
  param([Parameter(Mandatory)][string]$Reference)
  $lines = @(& docker.exe --config $DockerConfigRoot buildx imagetools inspect $Reference --format '{{.Manifest.Digest}}' 2>$null)
  if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1) {
    throw 'Registry digest observation failed'
  }
  $digest = $lines[0].Trim()
  if ($digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Registry returned an invalid digest'
  }
  $digest
}
function Get-LockObject {
  $lines = @(& git ls-remote $RepositoryRemote $RepositoryLockRef)
  if ($LASTEXITCODE -ne 0 -or $lines.Count -gt 1) {
    throw 'Repository publication lock observation failed'
  }
  if ($lines.Count -eq 0) { return $null }
  $parts = $lines[0] -split [char]9, 2
  if ($parts.Count -ne 2 -or $parts[1] -cne $RepositoryLockRef -or
      $parts[0] -cnotmatch '^[0-9a-f]{40}$') {
    throw 'Repository publication lock response is invalid'
  }
  $parts[0]
}
$self = Get-Process -Id $PID -ErrorAction Stop
$birth = [ordered]@{
  schema = 'subgen.latest-helper-birth/v1'
  run_token = $RunToken
  helper_pid = $PID
  helper_start_utc_ticks = $self.StartTime.ToUniversalTime().Ticks
  helper_executable_path = [IO.Path]::GetFullPath($self.Path)
  helper_script_path = $HelperScriptPath
  helper_script_sha256 = $HelperScriptSha256
  docker_config_root = $DockerConfigRoot
}
Write-ExclusiveJson -Path $BirthPath -Value $birth
$receipt = [ordered]@{
  schema = 'subgen.latest-helper-result/v1'
  run_token = $RunToken
  helper_pid = $PID
  pre_digest = $null
  post_digest = $null
  push_attempted = $false
  push_exit_code = $null
  result = 'helper_error'
  finished_utc = $null
}
try {
  $deadline = [DateTime]::UtcNow.AddMinutes(5)
  while (-not (Test-Path -LiteralPath $GatePath -PathType Leaf)) {
    if ([DateTime]::UtcNow -ge $deadline) {
      $receipt.result = 'gate_timeout'
      throw 'The committed helper gate was not opened'
    }
    Start-Sleep -Milliseconds 250
  }
  Assert-OwnerOnlyFile -Path $GatePath
  Assert-OwnerOnlyFile -Path $HelperScriptPath
  $gate = Get-Content -Raw -LiteralPath $GatePath | ConvertFrom-Json
  $gateKeys = @($gate.PSObject.Properties.Name | Sort-Object)
  $expectedGateKeys = @('helper_pid','helper_script_sha256',
    'helper_start_utc_ticks','run_token','schema')
  if (@(Compare-Object -CaseSensitive ($expectedGateKeys | Sort-Object) $gateKeys).Count -ne 0 -or
      $gate.schema -cne 'subgen.latest-helper-gate/v1' -or
      $gate.run_token -cne $RunToken -or
      [int64]$gate.helper_pid -ne $PID -or
      [int64]$gate.helper_start_utc_ticks -ne $birth.helper_start_utc_ticks -or
      $gate.helper_script_sha256 -cne $HelperScriptSha256) {
    $receipt.result = 'gate_identity_mismatch'
    throw 'Helper gate identity mismatch'
  }
  $lockObject = Get-LockObject
  if ($lockObject -cne $RepositoryLockObject) {
    $receipt.result = 'repository_lock_mismatch'
    throw 'Repository publication lock changed before push'
  }
  if ($null -eq (Get-Command Assert-HostedRunSetUnchanged `
        -CommandType Function -ErrorAction SilentlyContinue)) {
    $receipt.result = 'hosted_baseline_contract_missing'
    throw 'Complete persisted hosted-run baseline contract is not active'
  }
  Assert-HostedRunSetUnchanged 'latest helper precondition'
  $versionDigest = Get-RemoteDigest -Reference $VersionRef
  if ($versionDigest -cne $ExpectedDigest) {
    $receipt.result = 'immutable_version_mismatch'
    throw 'Immutable version digest changed before push'
  }
  $receipt.pre_digest = Get-RemoteDigest -Reference $LatestRef
  if ($receipt.pre_digest -ceq $ExpectedDigest) {
    $receipt.result = 'already_final'
  } elseif ($receipt.pre_digest -cne $PriorDigest) {
    $receipt.result = 'foreign_precondition'
  } else {
    $receipt.push_attempted = $true
    & docker.exe --config $DockerConfigRoot buildx imagetools create --tag $LatestRef ($RepositoryImage + '@' + $ExpectedDigest)
    $receipt.push_exit_code = $LASTEXITCODE
    if ($receipt.push_exit_code -eq 0) {
      $receipt.result = 'mutation_returned'
    } else {
      $receipt.result = 'mutation_failed'
    }
    try {
      $receipt.post_digest = Get-RemoteDigest -Reference $LatestRef
    }
    catch {
      $receipt.result = 'post_observation_failed'
    }
  }
}
catch {
  if ($receipt.result -ceq 'helper_error') {
    $receipt.result = 'helper_exception'
  }
}
finally {
  $receipt.finished_utc = [DateTime]::UtcNow.ToString('o')
  Write-ExclusiveJson -Path $ReceiptPath -Value $receipt
}
if ($receipt.result -cin @('already_final','mutation_returned')) { exit 0 }
exit 1
'@

function Get-Sha256Hex {
  param([Parameter(Mandatory)][byte[]]$Bytes)
  $hasher = [Security.Cryptography.SHA256]::Create()
  try {
    ([BitConverter]::ToString($hasher.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
  }
  finally {
    $hasher.Dispose()
  }
}
$HelperSourceBytes = [Text.UTF8Encoding]::new($false).GetBytes($HelperSource)
$HelperScriptSha256 = Get-Sha256Hex -Bytes $HelperSourceBytes

function Assert-OwnerOnlyItem {
  param(
    [Parameter(Mandatory)][string]$Path,
    [switch]$Directory,
    [string]$ExactFullName
  )
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      ($Directory -and -not $item.PSIsContainer) -or
      (-not $Directory -and $item.PSIsContainer) -or
      (-not [string]::IsNullOrEmpty($ExactFullName) -and
       [IO.Path]::GetFullPath($item.FullName) -cne $ExactFullName)) {
    throw ('Unsafe task-owned object: ' + $Path)
  }
  $acl = Get-Acl -LiteralPath $Path
  $foreignRules = @($acl.Access | Where-Object {
    $_.IdentityReference.Value -cne $owner -or
    $_.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow
  })
  $ownerFull = @($acl.Access | Where-Object {
    $_.IdentityReference.Value -ceq $owner -and
    ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
      [Security.AccessControl.FileSystemRights]::FullControl
  })
  if ($acl.Owner -cne $owner -or -not $acl.AreAccessRulesProtected -or
      $foreignRules.Count -ne 0 -or $ownerFull.Count -eq 0) {
    throw ('Task-owned ACL mismatch: ' + $Path)
  }
}
function Write-ExclusiveBytes {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][byte[]]$Bytes)
  $stream = [IO.FileStream]::new(
    $Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
    [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
  try {
    $stream.Write($Bytes, 0, $Bytes.Length)
    $stream.Flush($true)
  }
  finally {
    $stream.Dispose()
  }
  & icacls.exe $Path /inheritance:r /grant:r ($owner + ':F') | Out-Null
  if ($LASTEXITCODE -ne 0) { throw ('Unable to protect task-owned file: ' + $Path) }
  Assert-OwnerOnlyItem -Path $Path -ExactFullName ([IO.Path]::GetFullPath($Path))
}
function Get-CanonicalRunPaths {
  param([Parameter(Mandatory)][string]$RunToken)
  $root = [IO.Path]::GetFullPath(
    (Join-Path $TaskParent ('subgen-v050-latest-config-' + $RunToken)))
  [ordered]@{
    docker_config_root = $root
    helper_script_path = [IO.Path]::GetFullPath((Join-Path $root 'latest-helper.ps1'))
    helper_birth_path = [IO.Path]::GetFullPath((Join-Path $root 'helper-birth.json'))
    helper_gate_path = [IO.Path]::GetFullPath((Join-Path $root 'helper-gate.json'))
    helper_receipt_path = [IO.Path]::GetFullPath((Join-Path $root 'helper-result.json'))
    helper_stdout_path = [IO.Path]::GetFullPath((Join-Path $root 'helper.stdout.log'))
    helper_stderr_path = [IO.Path]::GetFullPath((Join-Path $root 'helper.stderr.log'))
  }
}
function Get-DockerProcessFingerprints {
  @(
    Get-Process -Name docker,docker-buildx -ErrorAction SilentlyContinue | ForEach-Object {
      try {
        [string]$_.ProcessName + ':' + [string]$_.Id + ':' +
          [string]$_.StartTime.ToUniversalTime().Ticks + ':' +
          [IO.Path]::GetFullPath($_.Path)
      }
      catch {
        throw 'A docker.exe process could not be fingerprinted'
      }
    } | Sort-Object
  )
}
function Assert-NoNewDockerProcess {
  param([Parameter(Mandatory)][string[]]$Baseline)
  $current = @(Get-DockerProcessFingerprints)
  $newProcesses = @(Compare-Object -CaseSensitive $Baseline $current |
    Where-Object { $_.SideIndicator -ceq '=>' })
  if ($newProcesses.Count -ne 0) {
    throw 'A post-baseline Docker CLI/buildx process is still active'
  }
}
function Assert-ReleaseIdentity {
  $blobHash = @(& python -I -c 'import hashlib,subprocess,sys; raw=subprocess.check_output(["git","show",sys.argv[1]]); raw.decode("utf-8","strict"); assert not raw.startswith(b"\xef\xbb\xbf"); print(hashlib.sha256(raw).hexdigest())' ($ReleaseCommit + ':' + $ReleaseNotesPath))
  if ($LASTEXITCODE -ne 0 -or $blobHash.Count -ne 1) {
    throw 'Unable to read the immutable release-notes blob'
  }
  if ($blobHash[0].Trim() -cne $ReleaseBodySha256) {
    throw 'Immutable release-body hash changed'
  }
  $tagRefLines = @(& gh api ('/repos/' + $Repository + '/git/ref/tags/v0.5.0'))
  if ($LASTEXITCODE -ne 0 -or $tagRefLines.Count -eq 0) {
    throw 'Unable to inspect the immutable release tag ref'
  }
  $tagRef = ($tagRefLines -join [Environment]::NewLine) | ConvertFrom-Json
  if ($tagRef.object.type -cne 'tag' -or $tagRef.object.sha -cne $ReleaseTagObject) {
    throw 'Immutable release tag object mismatch'
  }
  $tagObjectLines = @(& gh api ('/repos/' + $Repository + '/git/tags/' + $ReleaseTagObject))
  if ($LASTEXITCODE -ne 0 -or $tagObjectLines.Count -eq 0) {
    throw 'Unable to inspect the annotated release tag'
  }
  $tagObject = ($tagObjectLines -join [Environment]::NewLine) | ConvertFrom-Json
  if ($tagObject.tag -cne 'v0.5.0' -or $tagObject.object.type -cne 'commit' -or
      $tagObject.object.sha -cne $ReleaseCommit) {
    throw 'Annotated release tag does not peel to RELEASE_COMMIT'
  }
  foreach ($requiredFunction in @('Get-AuthoritativeReleaseState',
      'Assert-ExactRelease200','Assert-HostedRunSetUnchanged')) {
    if ($null -eq (Get-Command $requiredFunction -CommandType Function `
          -ErrorAction SilentlyContinue)) {
      throw 'The superseding authoritative publication contract is not active'
    }
  }
  $releaseState = Get-AuthoritativeReleaseState
  Assert-ExactRelease200 $releaseState
  Assert-HostedRunSetUnchanged 'release identity assertion'
}
function Get-RepositoryLockObject {
  $lines = @(& git ls-remote $RepositoryRemote $RepositoryLockRef)
  if ($LASTEXITCODE -ne 0 -or $lines.Count -gt 1) {
    throw 'Unable to inspect the repository publication lock'
  }
  if ($lines.Count -eq 0) { return $null }
  $parts = $lines[0] -split [char]9, 2
  if ($parts.Count -ne 2 -or $parts[1] -cne $RepositoryLockRef -or
      $parts[0] -cnotmatch '^[0-9a-f]{40}$') {
    throw 'Repository publication lock ref is malformed'
  }
  $parts[0]
}
function Assert-RepositoryLockTag {
  param([Parameter(Mandatory)][string]$ObjectSha,
        [Parameter(Mandatory)][string]$RunToken,
        [Parameter(Mandatory)][string]$ExpectedMessage)
  $lines = @(& gh api ('/repos/' + $Repository + '/git/tags/' + $ObjectSha))
  if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0) {
    throw 'Unable to inspect the repository lock tag object'
  }
  $tag = ($lines -join [Environment]::NewLine) | ConvertFrom-Json
  if ($tag.tag -cne ('subgen-v050-latest-lock-' + $RunToken) -or
      $tag.message -cne $ExpectedMessage -or
      $tag.object.type -cne 'commit' -or
      $tag.object.sha -cne $ReleaseCommit) {
    throw 'Repository lock tag object identity mismatch'
  }
}

$StateKeys = @(
  'docker_config_root','docker_process_baseline','expected_config',
  'expected_digest','foreign_digest','foreign_digest_seen',
  'foreign_observed_utc','helper_birth_path','helper_executable_path',
  'helper_gate_path','helper_pid','helper_receipt_path',
  'helper_receipt_sha256','helper_script_path','helper_script_sha256',
  'helper_start_utc_ticks','helper_started','helper_stderr_path',
  'helper_stdout_path','logged_in','mutation_intent','phase',
  'primary_engine_id','prior_digest','process_executable_path','process_id',
  'process_start_utc_ticks','registry_settled','release_body_sha256',
  'release_commit','release_tag_object','repository_lock_acquired',
  'repository_lock_object','repository_lock_ref','run_token','schema',
  'sequence','settled_digest'
)
function Assert-StateEnvelope {
  param([Parameter(Mandatory)]$State)
  $actualKeys = @($State.PSObject.Properties.Name | Sort-Object)
  if (@(Compare-Object -CaseSensitive ($StateKeys | Sort-Object) $actualKeys).Count -ne 0 -or
      $State.schema -cne 'subgen.latest-state/v3' -or
      [string]$State.run_token -cnotmatch '^[0-9a-f]{32}$' -or
      [string]$State.prior_digest -cnotmatch '^sha256:[0-9a-f]{64}$' -or
      [string]$State.expected_digest -cne $ExpectedIndex -or
      [string]$State.expected_config -cne $ExpectedConfig -or
      [string]$State.release_commit -cne $ReleaseCommit -or
      [string]$State.release_tag_object -cne $ReleaseTagObject -or
      [string]$State.release_body_sha256 -cne $ReleaseBodySha256 -or
      [string]$State.repository_lock_ref -cne $RepositoryLockRef -or
      [string]$State.helper_script_sha256 -cne $HelperScriptSha256 -or
      ($null -ne $State.repository_lock_object -and
       [string]$State.repository_lock_object -cnotmatch '^[0-9a-f]{40}$') -or
      ($null -ne $State.helper_receipt_sha256 -and
       [string]$State.helper_receipt_sha256 -cnotmatch '^[0-9a-f]{64}$') -or
      $State.logged_in -isnot [bool] -or
      $State.mutation_intent -isnot [bool] -or
      $State.registry_settled -isnot [bool] -or
      $State.foreign_digest_seen -isnot [bool] -or
      $State.repository_lock_acquired -isnot [bool] -or
      $State.helper_started -isnot [bool] -or
      [int64]$State.sequence -lt 1) {
    throw 'Latest v3 state schema/immutable identity mismatch'
  }
  if ($State.registry_settled) {
    if ([string]$State.settled_digest -cne [string]$State.prior_digest -and
        [string]$State.settled_digest -cne $ExpectedIndex) {
      throw 'Settled registry state has an unrecognized digest'
    }
  } elseif ($null -ne $State.settled_digest) {
    throw 'Unsettled registry state cannot record a settled digest'
  }
  if ($State.helper_started) {
    if ([int64]$State.helper_pid -le 0 -or
        [int64]$State.helper_start_utc_ticks -le 0) {
      throw 'Committed helper process identity is incomplete'
    }
  } elseif ([int64]$State.helper_pid -ne 0 -or
            [int64]$State.helper_start_utc_ticks -ne 0) {
    throw 'Uncommitted helper cannot carry a process identity'
  }
  $paths = Get-CanonicalRunPaths -RunToken ([string]$State.run_token)
  foreach ($name in $paths.Keys) {
    $recorded = [string]$State.$name
    if ($recorded -cne [string]$paths[$name] -or
        [IO.Path]::GetFullPath($recorded) -cne [string]$paths[$name]) {
      throw ('Latest v3 state has a non-canonical path: ' + $name)
    }
  }
  if ($State.foreign_digest_seen) {
    if ([string]$State.foreign_digest -cnotmatch '^sha256:[0-9a-f]{64}$' -or
        [string]::IsNullOrWhiteSpace([string]$State.foreign_observed_utc)) {
      throw 'Foreign-digest latch is malformed'
    }
  } elseif ($null -ne $State.foreign_digest -or
            $null -ne $State.foreign_observed_utc) {
    throw 'Foreign-digest latch cannot be cleared after recording a value'
  }
  foreach ($fingerprint in @($State.docker_process_baseline)) {
    if ([string]$fingerprint -cnotmatch '^[^:]+:\d+:\d+:.+$') {
      throw 'Docker process baseline is malformed'
    }
  }
}

if (Test-Path -LiteralPath $LatestLockPath) {
  $lockItem = Get-Item -LiteralPath $LatestLockPath -Force
  if (($lockItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      $lockItem.PSIsContainer) {
    throw 'Unsafe latest-state lock path'
  }
}
$latestLock = [IO.FileStream]::new(
  $LatestLockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite,
  [IO.FileShare]::None, 1, [IO.FileOptions]::WriteThrough)
& icacls.exe $LatestLockPath /inheritance:r /grant:r ($owner + ':F') | Out-Null
if ($LASTEXITCODE -ne 0) {
  $latestLock.Dispose()
  throw 'Unable to protect latest-state lock'
}
Assert-OwnerOnlyItem -Path $LatestLockPath -ExactFullName ([IO.Path]::GetFullPath($LatestLockPath))

$latestState = $null
$RegistrySettled = $false
$OperationFailure = $null
$CleanupFailure = $null
$DockerConfigRoot = $null
$StateCommitted = $false
$RecoverySafeToCleanup = $false
try {
  Assert-ReleaseIdentity
  if ($Mode -ceq 'publish') {
    if ((Test-Path -LiteralPath $LatestStatePath) -or
        (Test-Path -LiteralPath $LatestStateTempPath)) {
      throw 'Identity-bound latest recovery must complete before publication'
    }
    $RunToken = [Guid]::NewGuid().ToString('N')
    $paths = Get-CanonicalRunPaths -RunToken $RunToken
    $PriorLatestDigest = [string]$env:SUBGEN_PRIOR_LATEST_DIGEST
    if ($PriorLatestDigest -cnotmatch '^sha256:[0-9a-f]{64}$' -or
        $PriorLatestDigest -ceq $ExpectedIndex) {
      throw 'A distinct valid prior latest digest is mandatory'
    }
    $primaryEngine = @(& docker.exe info --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0 -or $primaryEngine.Count -ne 1 -or
        [string]::IsNullOrWhiteSpace($primaryEngine[0])) {
      throw 'Unable to bind latest publication to the primary Docker Engine'
    }
    $self = Get-Process -Id $PID -ErrorAction Stop
    $helperExecutable = [IO.Path]::GetFullPath(
      (Get-Command powershell.exe -ErrorAction Stop).Source)
    $latestState = [ordered]@{
      schema = 'subgen.latest-state/v3'
      sequence = 0
      run_token = $RunToken
      process_id = $PID
      process_start_utc_ticks = $self.StartTime.ToUniversalTime().Ticks
      process_executable_path = [IO.Path]::GetFullPath($self.Path)
      primary_engine_id = $primaryEngine[0].Trim()
      prior_digest = $PriorLatestDigest
      expected_digest = $ExpectedIndex
      expected_config = $ExpectedConfig
      release_commit = $ReleaseCommit
      release_tag_object = $ReleaseTagObject
      release_body_sha256 = $ReleaseBodySha256
      repository_lock_ref = $RepositoryLockRef
      repository_lock_object = $null
      repository_lock_acquired = $false
      docker_config_root = $paths.docker_config_root
      docker_process_baseline = @(Get-DockerProcessFingerprints)
      helper_script_path = $paths.helper_script_path
      helper_script_sha256 = $HelperScriptSha256
      helper_birth_path = $paths.helper_birth_path
      helper_gate_path = $paths.helper_gate_path
      helper_receipt_path = $paths.helper_receipt_path
      helper_receipt_sha256 = $null
      helper_stdout_path = $paths.helper_stdout_path
      helper_stderr_path = $paths.helper_stderr_path
      helper_executable_path = $helperExecutable
      helper_pid = 0
      helper_start_utc_ticks = 0
      helper_started = $false
      logged_in = $false
      mutation_intent = $false
      registry_settled = $false
      settled_digest = $null
      foreign_digest_seen = $false
      foreign_digest = $null
      foreign_observed_utc = $null
      phase = 'intent'
    }
  } else {
    if (-not (Test-Path -LiteralPath $LatestStatePath -PathType Leaf) -and
        -not (Test-Path -LiteralPath $LatestStateTempPath -PathType Leaf)) {
      throw 'No committed latest v3 publication state exists to recover'
    }
    $mainState = $null
    $pendingState = $null
    if (Test-Path -LiteralPath $LatestStatePath -PathType Leaf) {
      Assert-OwnerOnlyItem -Path $LatestStatePath -ExactFullName ([IO.Path]::GetFullPath($LatestStatePath))
      $mainState = Get-Content -Raw -LiteralPath $LatestStatePath | ConvertFrom-Json
      Assert-StateEnvelope -State $mainState
    }
    if (Test-Path -LiteralPath $LatestStateTempPath -PathType Leaf) {
      Assert-OwnerOnlyItem -Path $LatestStateTempPath -ExactFullName ([IO.Path]::GetFullPath($LatestStateTempPath))
      $pendingState = Get-Content -Raw -LiteralPath $LatestStateTempPath | ConvertFrom-Json
      Assert-StateEnvelope -State $pendingState
    }
    if ($null -ne $mainState -and $mainState.foreign_digest_seen -and
        $null -ne $pendingState -and -not $pendingState.foreign_digest_seen) {
      throw 'Pending state attempted to clear the foreign-digest latch'
    }
    if ($null -ne $pendingState -and $pendingState.foreign_digest_seen -and
        $null -ne $mainState -and -not $mainState.foreign_digest_seen -and
        [int64]$pendingState.sequence -ne ([int64]$mainState.sequence + 1)) {
      throw 'Foreign-digest latch successor is not monotonic'
    }
    if ($null -eq $mainState) {
      [IO.File]::Move($LatestStateTempPath, $LatestStatePath)
      $latestState = $pendingState
    } elseif ($null -eq $pendingState) {
      $latestState = $mainState
    } else {
      $immutableNames = @(
        'run_token','process_id','process_start_utc_ticks',
        'process_executable_path','primary_engine_id','prior_digest',
        'expected_digest','expected_config','release_commit',
        'release_tag_object','release_body_sha256','repository_lock_ref',
        'docker_config_root','helper_script_path','helper_script_sha256',
        'helper_birth_path','helper_gate_path','helper_receipt_path',
        'helper_stdout_path','helper_stderr_path','helper_executable_path'
      )
      foreach ($name in $immutableNames) {
        if ([string]$mainState.$name -cne [string]$pendingState.$name) {
          throw ('Pending state immutable identity mismatch: ' + $name)
        }
      }
      if ([int64]$pendingState.sequence -eq ([int64]$mainState.sequence + 1)) {
        [IO.File]::Move($LatestStateTempPath, $LatestStatePath, $true)
        $latestState = $pendingState
      } elseif ([int64]$pendingState.sequence -le [int64]$mainState.sequence) {
        Remove-Item -LiteralPath $LatestStateTempPath -Force
        $latestState = $mainState
      } else {
        throw 'Pending state sequence is not a single durable successor'
      }
    }
    Assert-StateEnvelope -State $latestState
    $RunToken = [string]$latestState.run_token
    $PriorLatestDigest = [string]$latestState.prior_digest
    $paths = Get-CanonicalRunPaths -RunToken $RunToken
    $originProcess = Get-Process -Id ([int]$latestState.process_id) -ErrorAction SilentlyContinue
    if ($null -ne $originProcess) {
      try {
        if ($originProcess.StartTime.ToUniversalTime().Ticks -eq
              [int64]$latestState.process_start_utc_ticks -and
            [IO.Path]::GetFullPath($originProcess.Path) -ceq
              [string]$latestState.process_executable_path) {
          throw 'Original latest publisher is still active'
        }
      }
      catch {
        if ($_.Exception.Message -ceq 'Original latest publisher is still active') { throw }
        throw 'Original publisher PID reuse could not be inspected safely'
      }
    }
    $primaryEngine = @(& docker.exe info --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0 -or $primaryEngine.Count -ne 1 -or
        $primaryEngine[0].Trim() -cne [string]$latestState.primary_engine_id) {
      throw 'Latest recovery Docker Engine changed'
    }
  }

  $DockerConfigRoot = [string]$latestState.docker_config_root
  if ($DockerConfigRoot -cne [string]$paths.docker_config_root -or
      [IO.Path]::GetFullPath($DockerConfigRoot) -cne [string]$paths.docker_config_root) {
    throw 'Latest Docker config is not the exact canonical run path'
  }

  function Write-LatestState {
    param([switch]$CreateOnly)
    if (Test-Path -LiteralPath $LatestStateTempPath) {
      throw 'Latest-state replacement path is not exclusive'
    }
    $latestState.sequence = [int64]$latestState.sequence + 1
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
      ($latestState | ConvertTo-Json -Depth 10 -Compress))
    Write-ExclusiveBytes -Path $LatestStateTempPath -Bytes $bytes
    if ($CreateOnly) {
      if (Test-Path -LiteralPath $LatestStatePath) {
        throw 'Latest state appeared concurrently'
      }
      [IO.File]::Move($LatestStateTempPath, $LatestStatePath)
    } else {
      if (-not (Test-Path -LiteralPath $LatestStatePath -PathType Leaf)) {
        throw 'Latest state disappeared during replacement'
      }
      [IO.File]::Move($LatestStateTempPath, $LatestStatePath, $true)
    }
    Assert-OwnerOnlyItem -Path $LatestStatePath -ExactFullName ([IO.Path]::GetFullPath($LatestStatePath))
  }
  function Record-ForeignDigest {
    param([Parameter(Mandatory)][string]$Digest)
    if (-not $latestState.foreign_digest_seen) {
      $latestState.foreign_digest_seen = $true
      $latestState.foreign_digest = $Digest
      $latestState.foreign_observed_utc = [DateTime]::UtcNow.ToString('o')
      $latestState.phase = 'foreign_digest_latched'
      Write-LatestState
    }
    throw 'A third latest digest was observed; state and lock remain unresolved'
  }
  function Get-StableLatestDigest {
    if ($latestState.foreign_digest_seen) {
      throw 'The one-way foreign-digest latch is set'
    }
    $observations = @()
    for ($index = 0; $index -lt 4; $index++) {
      $lines = @(& docker.exe --config $DockerConfigRoot buildx imagetools inspect $LatestRef --format '{{.Manifest.Digest}}' 2>$null)
      if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1) {
        throw 'Latest digest could not be observed repeatedly'
      }
      $digest = $lines[0].Trim()
      if ($digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
        throw 'Latest returned an invalid digest'
      }
      if ($digest -cne $PriorLatestDigest -and $digest -cne $ExpectedIndex) {
        Record-ForeignDigest -Digest $digest
      }
      $observations += $digest
      if ($index -lt 3) { Start-Sleep -Seconds 5 }
    }
    if (@($observations | Select-Object -Unique).Count -ne 1) {
      throw 'Latest did not remain stable across four observations'
    }
    $observations[0]
  }
  function Get-TaskHelperProcesses {
    try {
      @(
        Get-CimInstance Win32_Process | Where-Object {
          $_.CommandLine -and
          $_.CommandLine.IndexOf(
            [string]$latestState.helper_script_path,
            [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
          $_.CommandLine.IndexOf(
            [string]$latestState.run_token,
            [StringComparison]::Ordinal) -ge 0
        }
      )
    }
    catch {
      throw 'Task helper process inspection was ambiguous'
    }
  }
  function Assert-HelperQuiescent {
    if (@(Get-TaskHelperProcesses).Count -ne 0) {
      throw 'The exact latest push helper is still active'
    }
    Assert-NoNewDockerProcess -Baseline @($latestState.docker_process_baseline)
  }
  function Read-HelperRecord {
    param([Parameter(Mandatory)][string]$Path,
          [Parameter(Mandatory)][string[]]$ExpectedKeys)
    Assert-OwnerOnlyItem -Path $Path -ExactFullName ([IO.Path]::GetFullPath($Path))
    $record = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    if (@(Compare-Object -CaseSensitive ($ExpectedKeys | Sort-Object)
          @($record.PSObject.Properties.Name | Sort-Object)).Count -ne 0) {
      throw ('Helper record schema keys mismatch: ' + $Path)
    }
    $record
  }
  function Validate-HelperRecords {
    Assert-HelperQuiescent
    $birthExists = Test-Path -LiteralPath $latestState.helper_birth_path -PathType Leaf
    $gateExists = Test-Path -LiteralPath $latestState.helper_gate_path -PathType Leaf
    $receiptExists = Test-Path -LiteralPath $latestState.helper_receipt_path -PathType Leaf
    if (-not $latestState.helper_started) {
      if ($gateExists) { throw 'An uncommitted helper gate exists' }
      if (-not $birthExists -and -not $receiptExists) { return }
      if (-not $birthExists -or -not $receiptExists) {
        throw 'Uncommitted helper records are incomplete'
      }
    } elseif (-not $birthExists -or -not $gateExists -or -not $receiptExists) {
      throw 'Committed helper records are incomplete after process quiescence'
    }
    $birth = Read-HelperRecord -Path $latestState.helper_birth_path -ExpectedKeys @(
      'docker_config_root','helper_executable_path','helper_pid',
      'helper_script_path','helper_script_sha256',
      'helper_start_utc_ticks','run_token','schema')
    $receipt = Read-HelperRecord -Path $latestState.helper_receipt_path -ExpectedKeys @(
      'finished_utc','helper_pid','post_digest','pre_digest',
      'push_attempted','push_exit_code','result','run_token','schema')
    if ($birth.schema -cne 'subgen.latest-helper-birth/v1' -or
        $birth.run_token -cne $RunToken -or
        $birth.helper_script_path -cne $latestState.helper_script_path -or
        $birth.helper_script_sha256 -cne $HelperScriptSha256 -or
        $birth.docker_config_root -cne $DockerConfigRoot -or
        $receipt.schema -cne 'subgen.latest-helper-result/v1' -or
        $receipt.run_token -cne $RunToken -or
        [int64]$receipt.helper_pid -ne [int64]$birth.helper_pid -or
        $receipt.push_attempted -isnot [bool]) {
      throw 'Helper birth/result identity mismatch'
    }
    if ($latestState.helper_started) {
      if ([int64]$birth.helper_pid -ne [int64]$latestState.helper_pid -or
          [int64]$birth.helper_start_utc_ticks -ne
            [int64]$latestState.helper_start_utc_ticks -or
          $birth.helper_executable_path -cne
            [string]$latestState.helper_executable_path) {
        throw 'Committed helper process fingerprint mismatch'
      }
    } elseif ($receipt.push_attempted -or $receipt.result -cne 'gate_timeout') {
      throw 'An uncommitted helper produced a mutation-capable result'
    }
    foreach ($observed in @($receipt.pre_digest,$receipt.post_digest)) {
      if ($null -ne $observed -and
          [string]$observed -cne $PriorLatestDigest -and
          [string]$observed -cne $ExpectedIndex) {
        Record-ForeignDigest -Digest ([string]$observed)
      }
    }
    $receiptHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $latestState.helper_receipt_path).Hash.ToLowerInvariant()
    if ($null -ne $latestState.helper_receipt_sha256 -and
        [string]$latestState.helper_receipt_sha256 -cne $receiptHash) {
      throw 'Helper receipt changed after it was recorded'
    }
    if ($null -eq $latestState.helper_receipt_sha256) {
      $latestState.helper_receipt_sha256 = $receiptHash
      $latestState.phase = 'helper_receipt_recorded'
      Write-LatestState
    }
  }
  function Get-LockMessage {
    'Subgen v0.5.0 latest publication lock' + [Environment]::NewLine +
      'run_token=' + $RunToken + [Environment]::NewLine +
      'release_commit=' + $ReleaseCommit + [Environment]::NewLine +
      'release_tag_object=' + $ReleaseTagObject + [Environment]::NewLine +
      'release_body_sha256=' + $ReleaseBodySha256 + [Environment]::NewLine +
      'prior_digest=' + $PriorLatestDigest + [Environment]::NewLine +
      'expected_digest=' + $ExpectedIndex
  }
  function Create-RepositoryLock {
    if ($null -ne (Get-RepositoryLockObject)) {
      throw 'Another repository latest-publication lock already exists'
    }
    $lockMessage = Get-LockMessage
    $tagLines = @(& gh api -X POST ('/repos/' + $Repository + '/git/tags') -f ('tag=subgen-v050-latest-lock-' + $RunToken) -f ('message=' + $lockMessage) -f ('object=' + $ReleaseCommit) -f 'type=commit')
    if ($LASTEXITCODE -ne 0 -or $tagLines.Count -eq 0) {
      throw 'Unable to create the repository lock tag object'
    }
    $tagObject = ($tagLines -join [Environment]::NewLine) | ConvertFrom-Json
    if ([string]$tagObject.sha -cnotmatch '^[0-9a-f]{40}$') {
      throw 'Repository lock tag object SHA is invalid'
    }
    $latestState.repository_lock_object = [string]$tagObject.sha
    $latestState.phase = 'lock_object_created'
    Write-LatestState
    Assert-RepositoryLockTag -ObjectSha $latestState.repository_lock_object -RunToken $RunToken -ExpectedMessage $lockMessage
    $refLines = @(& gh api -X POST ('/repos/' + $Repository + '/git/refs') -f ('ref=' + $RepositoryLockRef) -f ('sha=' + $latestState.repository_lock_object) 2>$null)
    $createExit = $LASTEXITCODE
    $observed = Get-RepositoryLockObject
    if ($observed -cne [string]$latestState.repository_lock_object) {
      throw 'Repository lock creation was not exact and atomic'
    }
    if ($createExit -ne 0 -and $refLines.Count -eq 0) {
      $latestState.phase = 'lock_create_response_lost'
    } else {
      $latestState.phase = 'lock_acquired'
    }
    $latestState.repository_lock_acquired = $true
    Write-LatestState
  }
  function Reconcile-RepositoryLock {
    $observed = Get-RepositoryLockObject
    if ($null -eq $latestState.repository_lock_object) {
      if ($null -ne $observed) {
        throw 'An unowned repository publication lock exists'
      }
      return
    }
    Assert-RepositoryLockTag -ObjectSha $latestState.repository_lock_object -RunToken $RunToken -ExpectedMessage (Get-LockMessage)
    if ($observed -ceq [string]$latestState.repository_lock_object) {
      if (-not $latestState.repository_lock_acquired) {
        $latestState.repository_lock_acquired = $true
        $latestState.phase = 'lock_reconciled'
        Write-LatestState
      }
      return
    }
    if ($null -ne $observed) {
      throw 'Repository publication lock points to a foreign object'
    }
    if ($latestState.mutation_intent -and -not $latestState.registry_settled) {
      throw 'Repository publication lock disappeared before registry settlement'
    }
    $latestState.repository_lock_acquired = $false
  }
  function Remove-RepositoryLockExact {
    if (-not $latestState.repository_lock_acquired) { return }
    $current = Get-RepositoryLockObject
    if ($current -cne [string]$latestState.repository_lock_object) {
      throw 'Repository lock changed before exact deletion'
    }
    $latestState.phase = 'lock_delete_intent'
    Write-LatestState
    $lease = $RepositoryLockRef + ':' + [string]$latestState.repository_lock_object
    & git push --porcelain ('--force-with-lease=' + $lease) $RepositoryRemote (':' + $RepositoryLockRef) | Out-Null
    $deleteExit = $LASTEXITCODE
    $after = Get-RepositoryLockObject
    if ($null -ne $after) {
      if ($after -cne [string]$latestState.repository_lock_object) {
        throw 'Repository lock was replaced; refusing to delete the new object'
      }
      throw 'Exact repository lock deletion did not settle'
    }
    if ($deleteExit -ne 0) {
      $latestState.phase = 'lock_delete_response_lost'
    } else {
      $latestState.phase = 'lock_removed'
    }
    $latestState.repository_lock_acquired = $false
    Write-LatestState
  }

  if ($Mode -ceq 'publish') {
    Write-LatestState -CreateOnly
    $StateCommitted = $true
    Create-RepositoryLock
  } else {
    $StateCommitted = $true
    Reconcile-RepositoryLock
    Assert-HelperQuiescent
    $RecoverySafeToCleanup = $true
  }
  if ($latestState.foreign_digest_seen) {
    throw 'The one-way foreign-digest latch is set; publication cannot continue'
  }

  if (-not $latestState.registry_settled) {
    if (Test-Path -LiteralPath $DockerConfigRoot) {
      Assert-OwnerOnlyItem -Path $DockerConfigRoot -Directory -ExactFullName $DockerConfigRoot
    } else {
      New-Item -ItemType Directory -Path $DockerConfigRoot | Out-Null
      & icacls.exe $DockerConfigRoot /inheritance:r /grant:r ($owner + ':(OI)(CI)F') | Out-Null
      if ($LASTEXITCODE -ne 0) { throw 'Unable to protect latest Docker config' }
      Assert-OwnerOnlyItem -Path $DockerConfigRoot -Directory -ExactFullName $DockerConfigRoot
      $latestState.phase = 'config_created'
      Write-LatestState
    }
    $token = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'Missing registry token' }
    $token | & docker.exe --config $DockerConfigRoot login ghcr.io --username Herbertmt978 --password-stdin
    $token = $null
    if ($LASTEXITCODE -ne 0) { throw 'GHCR login failed' }
    $latestState.logged_in = $true
    $latestState.phase = 'logged_in'
    Write-LatestState
    $versionDigest = @(& docker.exe --config $DockerConfigRoot buildx imagetools inspect $VersionRef --format '{{.Manifest.Digest}}')
    if ($LASTEXITCODE -ne 0 -or $versionDigest.Count -ne 1 -or
        $versionDigest[0].Trim() -cne $ExpectedIndex) {
      throw 'Immutable v0.5.0 digest changed before latest handling'
    }

    if ($Mode -ceq 'recover') {
      Validate-HelperRecords
      $settled = Get-StableLatestDigest
      $latestState.registry_settled = $true
      $latestState.settled_digest = $settled
      $latestState.phase = if ($settled -ceq $ExpectedIndex) {
        'recovered_final'
      } else {
        'recovered_prior'
      }
      $RegistrySettled = $true
      Write-LatestState
    } else {
      $before = Get-StableLatestDigest
      if ($before -ceq $ExpectedIndex) {
        $latestState.registry_settled = $true
        $latestState.settled_digest = $ExpectedIndex
        $latestState.phase = 'final_verified'
        $RegistrySettled = $true
        Write-LatestState
      } else {
        Write-ExclusiveBytes -Path $latestState.helper_script_path -Bytes $HelperSourceBytes
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $latestState.helper_script_path).Hash.ToLowerInvariant() -cne
            $HelperScriptSha256) {
          throw 'Helper script bytes changed'
        }
        $latestState.mutation_intent = $true
        $latestState.phase = 'helper_launch_intent'
        Write-LatestState
        $arguments = @(
          '-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
          '-File',$latestState.helper_script_path,
          '-RunToken',$RunToken,
          '-DockerConfigRoot',$DockerConfigRoot,
          '-BirthPath',$latestState.helper_birth_path,
          '-GatePath',$latestState.helper_gate_path,
          '-ReceiptPath',$latestState.helper_receipt_path,
          '-HelperScriptPath',$latestState.helper_script_path,
          '-HelperScriptSha256',$HelperScriptSha256,
          '-Repository',$Repository,
          '-RepositoryRemote',$RepositoryRemote,
          '-RepositoryLockRef',$RepositoryLockRef,
          '-RepositoryLockObject',$latestState.repository_lock_object,
          '-ReleaseCommit',$ReleaseCommit,
          '-RepositoryImage',$RepositoryImage,
          '-VersionRef',$VersionRef,
          '-LatestRef',$LatestRef,
          '-PriorDigest',$PriorLatestDigest,
          '-ExpectedDigest',$ExpectedIndex
        )
        $helper = Start-Process -FilePath $latestState.helper_executable_path -ArgumentList $arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $latestState.helper_stdout_path -RedirectStandardError $latestState.helper_stderr_path
        $birthDeadline = [DateTime]::UtcNow.AddSeconds(30)
        while (-not (Test-Path -LiteralPath $latestState.helper_birth_path -PathType Leaf)) {
          if ([DateTime]::UtcNow -ge $birthDeadline) {
            throw 'Latest helper did not publish its birth record'
          }
          Start-Sleep -Milliseconds 250
        }
        $birth = Read-HelperRecord -Path $latestState.helper_birth_path -ExpectedKeys @(
          'docker_config_root','helper_executable_path','helper_pid',
          'helper_script_path','helper_script_sha256',
          'helper_start_utc_ticks','run_token','schema')
        if ($birth.schema -cne 'subgen.latest-helper-birth/v1' -or
            $birth.run_token -cne $RunToken -or
            [int64]$birth.helper_pid -ne $helper.Id -or
            $birth.helper_script_path -cne $latestState.helper_script_path -or
            $birth.helper_script_sha256 -cne $HelperScriptSha256 -or
            $birth.docker_config_root -cne $DockerConfigRoot -or
            $birth.helper_executable_path -cne $latestState.helper_executable_path) {
          throw 'Latest helper birth identity mismatch'
        }
        if ($helper.StartTime.ToUniversalTime().Ticks -ne
              [int64]$birth.helper_start_utc_ticks -or
            [IO.Path]::GetFullPath($helper.Path) -cne
              [string]$birth.helper_executable_path) {
          throw 'Latest helper PID/start/executable fingerprint mismatch'
        }
        $helperProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $helper.Id)
        if ($null -eq $helperProcess -or -not $helperProcess.CommandLine -or
            $helperProcess.CommandLine.IndexOf(
              $latestState.helper_script_path,
              [StringComparison]::OrdinalIgnoreCase) -lt 0 -or
            $helperProcess.CommandLine.IndexOf(
              $RunToken,
              [StringComparison]::Ordinal) -lt 0) {
          throw 'Latest helper command-line ownership is ambiguous'
        }
        $latestState.helper_pid = [int64]$birth.helper_pid
        $latestState.helper_start_utc_ticks = [int64]$birth.helper_start_utc_ticks
        $latestState.helper_started = $true
        $latestState.phase = 'helper_identity_committed'
        Write-LatestState
        Assert-ReleaseIdentity
        if ((Get-RepositoryLockObject) -cne
            [string]$latestState.repository_lock_object) {
          throw 'Repository lock changed before helper gate'
        }
        $gate = [ordered]@{
          schema = 'subgen.latest-helper-gate/v1'
          run_token = $RunToken
          helper_pid = $latestState.helper_pid
          helper_start_utc_ticks = $latestState.helper_start_utc_ticks
          helper_script_sha256 = $HelperScriptSha256
        }
        $gateBytes = [Text.UTF8Encoding]::new($false).GetBytes(
          ($gate | ConvertTo-Json -Compress))
        Write-ExclusiveBytes -Path $latestState.helper_gate_path -Bytes $gateBytes
        $latestState.phase = 'helper_gate_opened'
        Write-LatestState
        $helper.WaitForExit()
        Validate-HelperRecords
        $settled = Get-StableLatestDigest
        $latestState.registry_settled = $true
        $latestState.settled_digest = $settled
        $latestState.phase = if ($settled -ceq $ExpectedIndex) {
          'final_verified'
        } else {
          'prior_verified_no_mutation'
        }
        $RegistrySettled = $true
        Write-LatestState
        if ($settled -cne $ExpectedIndex) {
          throw 'Latest mutation did not publish the expected digest'
        }
      }
    }
  } else {
    $RegistrySettled = $true
    if (Test-Path -LiteralPath $DockerConfigRoot -PathType Container) {
      Validate-HelperRecords
    } elseif ($latestState.helper_started -and
              [string]::IsNullOrWhiteSpace(
                [string]$latestState.helper_receipt_sha256)) {
      throw 'Settled cleanup state lacks the committed helper receipt identity'
    }
  }
}
catch {
  $OperationFailure = $_
  if ($StateCommitted -and $null -ne $latestState -and
      -not $latestState.mutation_intent -and
      -not $latestState.foreign_digest_seen -and
      ($Mode -ceq 'publish' -or $RecoverySafeToCleanup)) {
    $latestState.registry_settled = $true
    $latestState.phase = 'no_registry_mutation'
    $RegistrySettled = $true
    try { Write-LatestState } catch { $CleanupFailure = $_ }
  }
}
finally {
  $token = $null
  if ($StateCommitted -and $null -ne $latestState) {
    $quiescent = $false
    try {
      Assert-HelperQuiescent
      $quiescent = $true
    }
    catch {
      if ($null -eq $CleanupFailure) { $CleanupFailure = $_ }
    }
    if ($quiescent -and (Test-Path -LiteralPath $DockerConfigRoot -PathType Container)) {
      try {
        Assert-OwnerOnlyItem -Path $DockerConfigRoot -Directory -ExactFullName $DockerConfigRoot
        & docker.exe --config $DockerConfigRoot logout ghcr.io 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Latest task-scoped registry logout failed' }
        $latestState.logged_in = $false
        $latestState.phase = 'logout_proven'
        Write-LatestState
        if ($RegistrySettled -and -not $latestState.foreign_digest_seen) {
          Remove-Item -LiteralPath $DockerConfigRoot -Recurse -Force
          if (Test-Path -LiteralPath $DockerConfigRoot) {
            throw 'Latest task-scoped Docker config cleanup failed'
          }
          $latestState.phase = 'config_removed'
          Write-LatestState
        }
      }
      catch {
        if ($null -eq $CleanupFailure) { $CleanupFailure = $_ }
      }
    }
    if ($RegistrySettled -and -not $latestState.foreign_digest_seen -and
        $quiescent -and $null -eq $CleanupFailure) {
      try {
        Remove-RepositoryLockExact
        $latestState.phase = 'cleanup_proven'
        Write-LatestState
        Remove-Item -LiteralPath $LatestStatePath -Force
        if (Test-Path -LiteralPath $LatestStatePath) {
          throw 'Latest state cleanup failed'
        }
      }
      catch {
        if ($null -eq $CleanupFailure) { $CleanupFailure = $_ }
      }
    }
  }
  if ($null -ne $latestLock) { $latestLock.Dispose() }
  if (-not (Test-Path -LiteralPath $LatestStatePath)) {
    Remove-Item -LiteralPath $LatestLockPath -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $LatestLockPath) {
      if ($null -eq $CleanupFailure) {
        $CleanupFailure = [Exception]::new('Latest-state lock cleanup failed')
      }
    }
  }
}
if ($null -ne $CleanupFailure) { throw $CleanupFailure }
if ($null -ne $OperationFailure) { throw $OperationFailure }
```

After `latest` is exact, perform the simulator lifecycle closeout from the
controller. First rerun the active-user, pytest/Python/build/buildx, Docker
container, other task-marker, and power-ownership checks. Recover any fixed
release/latest state record through the identity-bound cleanup above; require
no task-scoped container, volume, Docker credential directory, or state file to
remain. Recheck the `Ubuntu-24.04` rootful Engine ID, local Unix-socket-only
boundary, and empty container/image/volume sets. Require the token-derived WSL
run/config paths absent. If the v3 state says the distribution was stopped
before this task, require cleanup to have terminated it and prove it absent from
`wsl.exe --list --running --quiet`; if it was already running, leave it running.
Any unrelated WSL process, Docker object, or task marker blocks termination and
simulator shutdown without authorizing broader cleanup. Require the transfer container to be absent or exactly
`subgen-v050-transfer-4418b3c9`; when present, bind and remove it by its full
revalidated ID. Likewise require the scoped Windows Firewall rule to be absent
or exactly `Codex Subgen v0.5 transfer 4418b3c9`; when present, remove that exact
rule and prove it absent. Never delete a container or rule by a prefix match.
Remove the task-created local GHCR version/latest/prior-digest
references only after their remote digests are proven, without pruning shared
layers or unrelated build cache. Check the exact Task 10 roots
`D:\CodexTemp\subgen-v050-task10-0e77a573`,
`D:\CodexTemp\subgen-v050-task10-b40db7c`, and
`D:\CodexTemp\subgen-v050-task10-4418b3c9` against their ownership markers and
recorded absolute paths, preserve any final evidence already committed, then
remove those task roots and prove the three markers, transfer archive, temporary
model archive, synthetic media, release/latest configs, and state files absent.
Do not use a glob, unresolved variable, or cross-shell deletion. Preserve the pre-task power
state: if the Task 10 ownership record says this task woke the simulator and
the final activity check is empty, request a graceful Windows shutdown without
force and verify from the controller that SSH and the host go offline. If the
task did not wake it, or any other user/test/marker is active, leave it running
and record why. A reboot, `finally` bypass, or stale credential directory is a
cleanup gate, not authority to delete by prefix or shut down around other work.

**Expected:** tag/main at the governance-only release commit, candidate/image
provenance at the immutable runtime commit, no non-governance delta between
them, no hosted runs, identical version and latest GHCR digest, successful
pull/HTTP smoke, and live release.

### Task 13: Promote the verified digest and observe Frigate

**Systems:** preserved Frigate-hosted Subgen v0.3.0 rollback set, the exact
Task-11B/Task-12 v0.5.0 digest, systemd/Docker/NVIDIA runtime, and read-only
Frigate health. No real media may be deliberately damaged or deleted.

**Steps:** require the published digest to equal the Frigate-gated candidate.
Without changing Frigate or Ollama, promote that digest with the 10 GiB
hard/no-swap limit, existing low CPU priority/pids/OOM-score adjustment,
preserved generation registry, and classified-failure monitor. The OOM-score
adjustment is separate from `OomKillDisable`: canonicalize only a present null or
false `OomKillDisable` to false, and reject a missing key, true, or any other
type/value. Start with both deletion switches false and repair report/timer
inactive. Use the explicit positive audit-derived priority reserve; `auto`
blocks deployment. On Frigate Docker 29, require the pulled image `.Id` and
stopped container `.Image` to equal the post-amendment published OCI index
bound by the Task 11B seal. Independently require the exact post-amendment inner
config digest
and ordered diff IDs from the owner-only identity artifact. Pull and address the
image only by its published index digest.

Every v0.5 create uses exactly `--log-driver=json-file --log-opt mode=blocking`,
with `SAFE_LOG_CONFIG={"Type":"json-file","Config":{"mode":"blocking"}}` and
no `max-size`, `max-file`, or other logging option. Materialize every bind with
explicit `-v "${source}:${destination}:rw"` or
`-v "${source}:${destination}:ro"`; inspect exact `rw`/`ro` mode, matching `RW`,
and `rprivate` propagation. Request the GPU with exactly `--gpus
driver=nvidia,count=all` and inspect one device request with driver `nvidia`,
count `-1`, null device IDs, capabilities `[["gpu"]]`, and empty options. The
production and destructive-smoke runtimes use their exact audited `bridge`
network attachment. The Task 11B `--network none` then detach-`none` placeholder
sequence is profiler-only and must not be copied into production.

Use create-stopped transactions throughout. Preserve the full stopped v0.3
container ID and its original restart policy. Before renaming it, run
`docker update --restart=no` against that full ID and inspect the same ID to
require stopped state plus restart policy `no`; only then rename it to its
recorded rollback name and free `subgen`. Never remove it. A Docker daemon or
host restart while v0.5 is being tested must therefore leave the parked v0.3
container stopped rather than starting two Subgen runtimes. For the destructive smoke, generate
one conclusively dual-invalid file and one valid silent control under a new
owner-only disposable root. Create the smoke container stopped with only that
root, separate model/state volumes, restart `no`, both private ownership labels,
failed-file deletion false, invalid-media deletion true, the exact
`PRIORITY_PRESSURE_FILE`, and the read-only parent priority-signal mount. Inspect the full
ID before start: exact OCI index, separately verified inner config and ordered
diff IDs, command, environment, 10 GiB hard/no-swap boundary, labels, mounts,
logging, GPU request, OOM policy, and network. No production-media source may
appear. Start only that ID, require marker-before-delete for the invalid fixture,
require the silent control to remain, then stop/remove only the reverified ID
and remove the disposable root. Any disagreement or cleanup uncertainty blocks
deployment.

Next render the exact production Compose/environment, initially with both
deletion switches false. Create `subgen` stopped, bind its full ID, and inspect
the exact OCI index, inner config digest, ordered diff IDs, command, effective
environment, cgroup/CPU/pids/OOM policy, read-only identity/catalog mounts,
read-only priority parent mount, production media mounts, state/model mounts,
logging, GPU request, network, restart policy, and stopped state against the
owner-only deployment boundary. Require `subgen-priority-monitor.service`
active, its private expectation-policy hash/version/config/camera fingerprints
equal the Task 11B seal, and the required signal file owner/mode/boot identity
valid before either deletion-off or deletion-on start.
Only then start that ID. After the deletion-off startup
scan, HTTP/model/envelope/long-transcription/idle-unload and Frigate health gates
pass, stop that same ID and prove it stopped. Render the sole intended policy
change (`AUTO_DELETE_INVALID_MEDIA=true`), recreate stopped by digest, repeat
the complete inspection against a second boundary, then start the new full ID.
The pre-create image inspection and post-create container inspection are both
mandatory; a mutable tag or direct `compose up` is prohibited.

Target effective settings:

```dotenv
MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json
MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=5
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
# GPU_MEMORY_RESERVE_GIB is the positive value recorded by the released audit;
# `auto` is prohibited on this host.
PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json
SKIP_STARTUP_SCAN=False
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_INVALID_MEDIA=true
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

Require HTTP 200; the gated envelope/model decision; fresh system/total/free/
reserved/allocatable telemetry; scan progress; monitor heartbeat; repair timer
inactive; priority producer health and exact private-policy identity; priority
status `configured=true` with latest and latched-transition observation digests
and state never `unavailable`; causal controller phase/reason/model-residency,
`model_load_generation`, and `model_unload_generation`
generations; one successful long GPU transcription; idle-resident unload; marker
skips for retained failures only when the compatibility proof supports them;
10 GiB effective cap; no OOM/restart/host/PSI/GPU regression; and every
audit-recorded Frigate FPS/detector/embedding/health threshold. Use passive
production observation only; do not inject synthetic GPU pressure.
An asserted but valid signal keeps Subgen unloaded and pauses the rollout
observation until three distinct clear source generations; it never permits a
load. A missing, stale, invalid, regressed, or unavailable signal aborts both
the deletion-off and deletion-on transaction before media processing.

Keep an outer host supervisor bound to the current production full ID until the
observation seal is durable. On any failure it first writes and fsyncs the
rollback intent with both deletion booleans false and repair `report`, then
stops that exact v0.5 ID and proves it stopped. If stop identity or stopped
state cannot be proved, do not touch or start v0.3. Once stopped, checksum-copy
the v0.5 generation/state tree to a distinct owner-only audit root that will
never be mounted by v0.3, remove only the reverified v0.5 container, and verify
no Subgen runtime remains. Verify the preserved v0.3 container full ID, config
digest, ordered diff IDs, rollback-name label/record, config/cache/state
checksums, stopped state, and parked restart policy. Materialize deletion-off
and report-only behavior through the captured external monitor/repair
configuration before renaming that exact ID back to `subgen`; a stopped
container's command or environment is immutable and must never be described as
edited in place. If the safe rollback requires a container command/environment
change, create a new rollback container stopped from the captured immutable
v0.3 image/config plus only the deletion-off overrides, bind and inspect its new
full ID, and leave the original parked container untouched. Immediately before
starting the selected verified rollback ID, restore its recorded restart policy
and re-inspect both the policy and safe external deletion/report settings.
Start only that verified v0.3 ID, prove it is the sole Subgen runtime, HTTP 200,
no restart/OOM increase, and deletion-off/report-only effective state, then
restore captured legacy unit states only where those same safe settings remain
provable. Retain the separated v0.5 audit copy. Neither rollout nor rollback may
start, stop, or reconfigure Frigate or Ollama.

Recheck the Plex VM independently: no Subgen container/process/port, monitor
inactive/disabled, and Plex HTTP 200. Never recreate the retired Plex instance.

After the Frigate observation and Plex retirement proofs, but outside
`RELEASE_COMMIT`, resolve `%CODEX_HOME%\home-architecture-source.json` and edit
the canonical Markdown it names in the `Personal_Codex_Setup` repository. If
the pointer is absent, malformed, or names a missing file, locate the
`Herbertmt978/Personal_Codex_Setup` clone, require exactly one candidate whose
Git remote and canonical
`docs/home-architecture/HOME-ARCHITECTURE.md` agree, read its `AGENTS.md`, use
that canonical file, and record the pointer problem as setup drift. Pointer
drift alone must not prevent the required map update; only no verified clone or
an ambiguous set of clones blocks.
Record Frigate as the canonical Subgen host, v0.5.0 and the published digest,
the 10 GiB hard/no-swap limit, the explicit shared-RTX-3090 reserve/model
decision, the preserved v0.3 rollback, and the retired Plex instance. Append a
v0.3.0 -> v0.5.0 entry to component version history and update the map version
and change log without credentials, private tokens, media names, MAC addresses,
or serial numbers. Run that repository's required checks plus `git diff
--check`, review and commit the map separately, and record its commit in Task 13
evidence. A missing canonical map after the fallback search, an ambiguous clone,
or a failed map check blocks issue close; it does not authorize adding the
cross-repository file to the v0.5.0 tag.

Finally resolve the exact issue URL/number recorded by Task 1, re-read its
title/body/state, and require the expected acceptance issue still open. Add one
privacy-safe closing comment containing the release URL, immutable GHCR digest,
Frigate observation result, Plex retirement result, and architecture-map commit;
then close that exact number and re-read it to require `CLOSED`. Never select an
issue by recency, author, or title alone.

**Expected:** healthy immutable deployment and intact tested v0.3.0 operational
rollback. Public rollback guidance remains v0.4.1 with deletion off. The
canonical architecture map is current in its own repository and the exact
traceability issue is closed only after release, Frigate observation, Plex
retirement, rollback, and map evidence are complete.

## Plan Pressure Test

- One owner exists for each new policy and destructive decision.
- Frigate/Ollama compute priority has a host-owned coarse signal; Subgen's
  existing controller remains the sole admission/yield owner and global GPU
  utilization remains observation-only.
- No legacy or recovery route bypasses invalid-media classification.
- Tasks define exact files, contracts, commands, expected evidence, and scoped
  commit boundaries.
- Verification covers unit, integration, package, Linux/cgroup, real inference,
  release, and production without real-media destructive tests.
- Result: `proceed after Task 11A refreeze`; live Frigate deployment remains
  separately gated by Task 11B's representative reserve, priority-signal, and
  shared-health evidence.

## Execution Readiness View

- Intent Lock: bounded transcription, highest-safe fixed model, cooperative
  memory and host-priority yield, first-failure marker, invalid-media-only
  optional deletion, v0.5.0.
- Scope Fence: no Sonarr/Radarr API, no Ollama coordinator, no arbitrary model
  downgrade, no marker schema v2, no public mutation API.
- Compatibility Lock: routes/outputs/queue/webhook/markers preserved; legacy
  destructive inputs accepted but safely narrowed.
- Owner Lock: resource policy; priority-signal parser; Frigate priority
  producer; segmentation; model runtime; media; failure monitor; existing
  marker and unlink owners.
- Review Gates: focused tests and scoped diff per task; full local; full idle
  simulator; isolated pre-publication Frigate candidate; no-runner proof;
  digest/release proof; passive Frigate production observation; and Plex
  retirement verification.
- Rewind Rules: owner/deletion drift returns to design; focused failure returns
  to owning task; any priority contract/runtime/gate change invalidates the
  candidate, envelope, sampler, and lifecycle chain; simulator disproval
  updates evidence-bound policy; rollout failure invokes deletion-off rollback.
- Evidence: commands/results, issue/release URLs, commits/tag/digest, no hosted
  runs, Plex retirement, and Frigate HTTP/model/chunk/host/GPU/OOM/restart/
  scan/monitor/health/rollback state.

## Risks

- stable-ts is archived and callback/result construction behavior may differ in
  the packaged version; simulator gates callback propagation and real result
  rendering before release.
- Callback yielding cannot stop allocations before first progress; conservative
  baseline chunks and preflight pressure remain mandatory.
- Host priority pressure also cannot preempt an already-running CUDA kernel;
  Task 11B must prove bounded callback reaction and higher-priority recovery.
  A dead/stale producer intentionally pauses Subgen indefinitely.
- All-permit release can deadlock if lock order drifts; focused race tests and
  one documented order gate publication.
- Independent chunk timestamps can disagree at boundaries; midpoint ownership,
  core-seam clipping, and strict monotonic rejection are tested against
  synthetic long media. Identical seam text is deliberately retained when its
  semantic ownership cannot be proven, favouring transcript completeness over
  unsafe automatic deduplication.
- Host/container `st_dev` may differ across a bind mount; simulator must prove
  the structured identity comparison before deletion is enabled.
- Generic tiers are fallback hypotheses. Only a matching immutable exact-runtime
  envelope may promote above them, and failed evidence may demote them before
  release.
- The shared 3090 showed about 11 GiB free with `qwen3:8b` pinned and about
  17.4 GiB after it was unloaded while the Ollama API remained idle. Both are
  transient observations, never tier authority or a substitute for the
  explicit priority reserve.
- GPU telemetry loss or a Frigate/Ollama workload increase can pause Subgen
  indefinitely; that is safer than evicting higher-priority workloads.
- Missing simulator/GHCR credentials or another active workload is a blocker,
  not authority to expose secrets, compete, or use hosted runners.
- Frigate rollback to v0.3.0 restores broad legacy semantics unless both
  deletion switches and repair deletion are disabled first; public rollback to
  v0.4.1 has the same deletion-off requirement. These paths are not interchangeable.

## Retirement

- Retire whole-track selected-stream extraction only from segmented jobs; keep
  the compatible short path.
- Retire monitor generic/crash deletion and repair crash deletion in v0.5.0.
- Keep legacy input names with warnings through the documented window.
- Keep marker schema v1, descriptor-relative unlink, `.subgen_skip`, and prior
  release artifacts.
- Keep the Frigate v0.3.0 config/model-cache/image-digest backup and the retired
  Plex deployment files/state until production observation completes.
- Retire all pre-priority candidate, ModelEnvelope, sampler, lifecycle, and gate
  artifacts from authority while preserving them as diagnostic history.

## Execution Route

- Decision: `subagent-driven`.
- Evidence: resource/segmentation, failure policy, and release/deployment are
  bounded specialist slices that benefit from fresh implementation and
  two-stage specification/quality review while the root preserves integration.
- Fallback: root executes a slice inline if simulator/external state makes it
  serial or a subagent slot is unavailable.
- User confirmation required: `no` for continued implementation; the core scope
  and Frigate target were explicitly authorized. The Frigate/GPU document
  amendment remains under review until its evidence gates pass.
