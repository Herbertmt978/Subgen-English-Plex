# Memory-Aware Segmented Transcription v0.5.0 Implementation Plan

Status: `approved for execution with Frigate/GPU amendment 2026-08-31`
Date: `2026-08-31`

## Goal

Release Subgen English for Plex `v0.5.0` with bounded sequential audio
segmentation, deterministic highest-safe automatic Whisper model selection,
cooperative memory-pressure yielding, and conservative media validation.
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
or admission arithmetic. `subgen_core/model_envelope_catalog.py` owns catalog
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
- User approvals through 2026-08-31: implement segmentation even if a fork is
  necessary; choose the highest-quality safe model from RAM; yield memory to
  other workloads; mark/skip at the first failure; delete only media that both
  FFprobe and PyAV cannot parse; package v0.5.0; deploy with first-failure
  invalid-media deletion; perform all tests locally or on the idle simulator
  and never on GitHub-hosted runners; retire the Plex-hosted instance and use
  Frigate's RTX 3090 as the canonical deployment target

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
- A path or generic parser error cannot distinguish corrupt media from a
  transient validator or resource failure; independent typed validation is
  required before deletion.
- Minimum code boundary: three focused core modules plus the isolated profiler,
  narrow owner integration, typed event/state migration, and
  config/package/docs/version/release surfaces.
- Decision: `code-change`.

## Existence Check

- Reuse `extract_audio_segment_to_memory`, the inference gate/model lock,
  `ProgressHandler`, generation marker reader, and exact unlink safety.
- Add `resource_management.py` because no current owner represents cgroup/PSI
  capacity or adaptive pressure state.
- Add `segmentation.py` because window/ownership/merge policy does not belong
  in the already large facade or transcription orchestrator.
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

## Files

### Create

- `subgen_core/resource_management.py`
- `subgen_core/resource_probes.py`
- `subgen_core/model_envelope_catalog.py`
- `profile_model_envelopes.py`
- `subgen_core/segmentation.py`
- `tests/test_resource_management.py`
- `tests/test_resource_probes.py`
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
docker build --pull --label org.opencontainers.image.version=0.5.0 --label org.opencontainers.image.revision="$RUNTIME_COMMIT" -t subgen-english-plex:v0.5.0-candidate-4418b3c9 .
identity_root="$(mktemp -d /tmp/subgen-v050-identity.XXXXXX)"
identity_json="$identity_root/image-identity.json"
image_archive="$identity_root/subgen-v050-candidate.tar"
archive_manifest="$identity_root/docker-save-manifest.json"
image_config="$identity_root/image-config.json"
docker save --output "$image_archive" subgen-english-plex:v0.5.0-candidate-4418b3c9
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
actual_layers="$(docker image inspect --format '{{json .RootFS.Layers}}' subgen-english-plex:v0.5.0-candidate-4418b3c9)"
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
generation registry, application state, OCI config digest/ordered diff IDs, and
the enabled/active states of `subgen-monitor.service`, `subgen-repair.timer`, and
`subgen-repair.service`. Stop/disable the legacy monitor and repair timer/service
before stopping the old Subgen container, and verify all three units inactive
and the monitor/timer disabled. Restore captured unit states only during a
deletion-off v0.3.0 rollback, never for the v0.5 candidate.

Before the first live sample, freeze the reviewed Task 11B protocol,
`gate_health_sampler.py`, and `test_gate_health_sampler.py` in a full-SHA
`SAMPLER_COMMIT`. These two Python files are owner-operated host-side evidence
tooling, not Subgen runtime source, repository product tests, an image build
input, production configuration, or an installed release artifact. The
unchanged `.dockerignore` excludes `docs`, and the unchanged Dockerfile's
explicit `COPY` set contains neither file. Record the sampler commit, Git blob
IDs, and SHA-256 values of the exact bytes transferred to Frigate; never mount
them into the candidate. Any sampler change after sampling starts invalidates
and restarts Task 11B evidence, but it does not change the frozen runtime image.

Create each disposable candidate in the stopped state with restart policy
`no`, a unique 128-bit-or-stronger gate token, the dedicated Task 11B, role,
runtime-commit, and token labels, the exact memory/no-extra-swap boundary, and
no Docker-socket mount or host network. The sampler must bind the full stopped
container ID, OCI config digest, intended-command SHA-256, labels, local Docker
Engine ID, and host boot ID before it opens evidence or starts the container.
The create-only execution-boundary manifest uses schema 3 and binds the complete
Docker execution boundary, including direct `/usr/bin/python3` execution as
UID/GID `1000:1000`, working directory `/subgen`, a read-only root filesystem,
all Linux capabilities dropped, `no-new-privileges`, the exact bounded `/tmp`
tmpfs, no profiler network, the exact NVIDIA device request, blocking
non-rotating `json-file` logging, every mount source/destination/mode, and the
full command and environment digests. Hash that manifest independently before
asking the sampler to emit its transient-systemd wrapper. The wrapper must
register immutable-ID cleanup in `ExecStopPost` before starting the sampler;
the final caller then executes that already materialized wrapper without
reconstructing its arguments.

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
expected_config="$(python -c "import json; print(json.load(open('image-identity.json'))['image_identity']['config_digest'])")"
expected_layers="$(python -c "import json; print(json.dumps(json.load(open('image-identity.json'))['image_identity']['layer_diff_ids'], separators=(',', ':')))")"
verify_candidate_identity() {
  test "$(docker image inspect --format '{{.Id}}' subgen-english-plex:v0.5.0-candidate-4418b3c9)" = "$expected_config"
  test "$(docker image inspect --format '{{json .RootFS.Layers}}' subgen-english-plex:v0.5.0-candidate-4418b3c9)" = "$expected_layers"
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
`/opt/subgen/model-envelopes/` paths, and mount a distinct owner-only staging
directory writable at `/profile-output`. Invoke the packaged profiler for
explicit `large-v3` only after Task 2B approves the generic
incremental-peak-plus-margin admission under the fresh 12 GiB cgroup state:

The profiler command runs beneath a PID-1 hold protocol. The hold process
captures a bounded stdout artifact and a create-once return-code receipt, then
keeps the container alive for the complete 900-second shared-health observation
so the sampler can supervise Frigate while validating the durable profiler
result. Require `large-v3 --runs 3` to return the profiler's documented safe
capacity code `3` with no catalog promotion. Destroy and verify that container
stopped, then run `medium --runs 30 --after-safe-failure large-v3` in a clean
container and require return code `0`. The sampler validates the exact expected
return code, receipt, bounded stdout, staged catalog presence/integrity, and
model identity; the host must additionally run Task 2A's complete catalog
loader/matcher before installing the successful staged catalog. That external
full validation remains mandatory because sampler-side catalog inspection is
deliberately only a bounded gate check.

On safe admission/allocation failure, destroy that profiler container, verify
model/cache release, and repeat in clean explicit 12 GiB profiling processes
for `medium`, `small`, `base`, then `tiny` only as needed. Do not rebuild. After
each successful run, validate the staged artifact through Task 2A and atomically
install it mode 0600 as the next canonical catalog, then destroy the profiler
and verify model/cache release. Before the automatic container start, run
`verify_candidate_identity` again as the immediately preceding command. Mount
both canonical files read-only, set `MODEL_ENVELOPE_CATALOG` and
`MODEL_ENVELOPE_IDENTITY` to their exact container paths, and restart the exact
image with auto in a new cgroup created with exactly `--memory=10g
--memory-swap=10g`. Require strict identity-to-catalog/current-runtime/policy
matching, three fresh stabilized samples, and immediate host/cgroup/GPU checks
showing that measured incremental peaks plus explicit margins and the separate
reserves fit the fresh 10 GiB boundary. The 12 GiB profiling cap is evidence
only and cannot qualify `large-v3`; if it fails fresh 10 GiB admission, do not
load it, profile `medium` and lower as needed in clean 12 GiB profiler
containers, and repeat the fresh 10 GiB auto start until the highest qualified
entry is selected or no-safe-model recovery is required.

Across every 12 GiB profiler and the final 10 GiB auto run, measure cold load,
first inference, long disposable translation, unload/reload, idle-resident
unloading, cgroup/device peaks, and identity continuity for at least 15 minutes
of representative camera/detector/embedding/Ollama traffic. Abort immediately
on NVIDIA Xid, cgroup/CUDA OOM, or container restart increase; abort when any
camera process FPS remains below 90% of configured FPS for more than 30
seconds, skipped FPS exceeds 0.5, a detector stalls/errors, or an embedding
error appears. Do not inject synthetic GPU pressure. Test v0.3.0 marker
compatibility in isolated state. On any failure, set deletion off and restore
the exact v0.3.0 rollback identity/cache/config and captured unit states.

The sampler begins the 900-second clock only after readiness and a complete
valid baseline, performs complete samples at `t=0,5,...,900`, never catches up
with burst samples, and passes only after the full `t=900` status/telemetry/log
sample and durable seal. Camera low-FPS duration uses `time.monotonic()` and
aborts only when it is strictly greater than 30 seconds; sample counts never
stand in for elapsed time. After automatic-runtime readiness, every sample must
receive a fresh `/status` response proving requested `auto`, selected `medium`,
`exact_match`, envelope provenance, the eight-GiB reserve, and normal/open/no-
recovery state; any blind interval restarts the gate. A profiler has no status
endpoint, so its explicitly labelled role and intended-command checksum are
paired with mandatory external profiler exit/catalog/result validation; the
shared-health sampler alone cannot qualify a model.

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

**Steps:** `RUNTIME_COMMIT` is exactly
`4418b3c97296a04b311d29d9ce52abefef64e108`, the source revision recorded in
the candidate's OCI revision label and bound to its verified configuration
digest and ordered rootfs diff IDs. Never recompute it from a later `HEAD`.
Record the privacy-safe candidate evidence, accept ADR 0002 only after every
gate passes, rerun the structural checker and `git diff --check`, and commit
the completed evidence as `Record v0.5.0 verification evidence`.

Before that commit, add exactly one single-line, canonical JSON record to
`90-evidence.md`, prefixed by `Task-11B-Sampler-Binding: `. The object contains
exactly seven keys: `schema` with value
`subgen.task11b.sampler-binding/v1`, plus `sampler_commit`, `sampler_blob`,
`sampler_sha256`, `test_blob`, `test_sha256`, and `gate_seal_sha256`. Derive the
two Git blob IDs and file
hashes from `SAMPLER_COMMIT`, derive `sampler_sha256` and the seal hash from the
owner-only final 10 GiB automatic-runtime pass seal actually returned by Task
11B, and compare the transferred test hash with `test_sha256` before recording
it. Record the large-v3 safe-failure and medium-profiler pass seals separately
in the privacy-safe Task 11B evidence so the final automatic-runtime seal cannot
be mistaken for the complete profiling chain. All hashes are
lowercase, the Git object IDs are the repository's full object IDs, and the
record contains no host path. This committed record, rather than a caller-set
environment variable, is the release-side source of truth for sampler identity.

`RELEASE_COMMIT` is the resulting lowercase full 40-character commit. Require
`RUNTIME_COMMIT`, `SAMPLER_COMMIT`, and `RELEASE_COMMIT` to be three distinct
commits in strict runtime -> sampler -> release order. Require the sampler and
test blobs at `RELEASE_COMMIT` to equal the recorded blobs at
`SAMPLER_COMMIT`, and require their exact SHA-256 values to equal the committed
binding and the Task 11B seal/transfer evidence. Product/runtime source, existing tests,
release notes, packaging, workflows, Dockerfile, `.dockerignore`, and every
image/runtime input must remain byte-for-byte those at `RUNTIME_COMMIT`. The
sampler and its test are the sole permitted executable post-runtime delta.

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
A	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/gate_health_sampler.py
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/resume-state-hint.json
A	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_gate_health_sampler.py
M	docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/todo-checkpoint-draft.json
```

Keep `99-reflection.md` incomplete and outside the tagged release commit until
post-release closeout. Recheck the unchanged candidate's config digest,
ordered diff IDs, and revision label against Task 11B immediately before any
remote mutation. A sampler change restarts Task 11B. A runtime/build-input
change, image rebuild or relabel, config/diff-ID change, or revision-label
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
not absence. Recheck the idle
simulator's candidate label, config digest, and ordered diff IDs against Task
11B. Treat a prior interrupted publication as resumable only when every existing
local/remote Git ref, GitHub release, and GHCR tag is absent or resolves exactly
to the recorded commit, release body, or digest; any mismatched partial state
blocks without overwriting it. Only
then fast-forward verified history to `main`, create or accept the exact
annotated tag at `RELEASE_COMMIT`, and prove both remote refs. On the idle
simulator, securely push the already verified image as `v0.5.0` and `latest`
using private task-scoped Docker configurations with ordinary and restart-safe
identity-bound cleanup;
require the same manifest digest and a clean anonymous pull-smoke on a distinct
empty local Docker Engine. Capture and locally pull the prior `latest` digest
before changing it, and restore/prove that digest on every mutation failure.
Create or accept the exact release after the version smoke, then mutate `latest`
as the final public write.
The local candidate and remotely pulled image must equal Task 11B's OCI config
digest and ordered layer diff IDs. Its OCI revision label must equal
`RUNTIME_COMMIT`; main and the annotated tag must equal `RELEASE_COMMIT`.
Publication blocks unless the runtime-to-release delta exactly equals Task
11C's status/path manifest.
Registry manifest equality between version/latest is an additional tag check,
not the candidate identity test.

```powershell
$ErrorActionPreference = 'Stop'
$RuntimeCommit = '4418b3c97296a04b311d29d9ce52abefef64e108'
$ExpectedConfig = 'sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae'
$ExpectedIndex = 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc'
$EvidencePath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/90-evidence.md'
$SamplerPath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/gate_health_sampler.py'
$SamplerTestPath = 'docs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_gate_health_sampler.py'
$ReleaseNotesPath = 'docs/RELEASE_NOTES_0.5.0.md'
$GateSealPath = $env:SUBGEN_TASK11B_GATE_SEAL
$releaseLines = @(& git rev-parse --verify 'HEAD^{commit}')
if ($LASTEXITCODE -ne 0 -or $releaseLines.Count -ne 1) {
  throw 'Unable to resolve RELEASE_COMMIT'
}
$ReleaseCommit = $releaseLines[0].Trim()
if ($RuntimeCommit -cnotmatch '^[0-9a-f]{40}$' -or
    $ReleaseCommit -cnotmatch '^[0-9a-f]{40}$') {
  throw 'Runtime and release identities must be lowercase full SHAs'
}
$evidenceLines = @(& git show "$ReleaseCommit`:$EvidencePath")
if ($LASTEXITCODE -ne 0) { throw 'Unable to read committed Task 11B evidence' }
$bindingPrefix = 'Task-11B-Sampler-Binding: '
$bindingLines = @($evidenceLines | Where-Object { $_.StartsWith($bindingPrefix, [StringComparison]::Ordinal) })
if ($bindingLines.Count -ne 1) { throw 'Expected exactly one committed sampler binding' }
$binding = $bindingLines[0].Substring($bindingPrefix.Length) | ConvertFrom-Json
$expectedBindingKeys = @('gate_seal_sha256','sampler_blob','sampler_commit',
  'sampler_sha256','schema','test_blob','test_sha256')
$actualBindingKeys = @($binding.PSObject.Properties.Name | Sort-Object)
if (@(Compare-Object -CaseSensitive ($expectedBindingKeys | Sort-Object) $actualBindingKeys).Count -ne 0 -or
    $binding.schema -cne 'subgen.task11b.sampler-binding/v1') {
  throw 'Sampler binding schema mismatch'
}
$SamplerCommit = [string]$binding.sampler_commit
foreach ($Value in @($binding.sampler_blob,$binding.test_blob)) {
  if ([string]$Value -cnotmatch '^[0-9a-f]{40}$') { throw 'Sampler Git blob identity is invalid' }
}
foreach ($Value in @($binding.sampler_sha256,$binding.test_sha256,$binding.gate_seal_sha256)) {
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

$samplerBlob = @(& git rev-parse --verify "$SamplerCommit`:$SamplerPath")
$testBlob = @(& git rev-parse --verify "$SamplerCommit`:$SamplerTestPath")
$releaseSamplerBlob = @(& git rev-parse --verify "$ReleaseCommit`:$SamplerPath")
$releaseTestBlob = @(& git rev-parse --verify "$ReleaseCommit`:$SamplerTestPath")
if ($LASTEXITCODE -ne 0 -or $samplerBlob.Count -ne 1 -or $testBlob.Count -ne 1 -or
    $releaseSamplerBlob.Count -ne 1 -or $releaseTestBlob.Count -ne 1 -or
    $samplerBlob[0].Trim() -cne $binding.sampler_blob -or
    $testBlob[0].Trim() -cne $binding.test_blob -or
    $releaseSamplerBlob[0].Trim() -cne $binding.sampler_blob -or
    $releaseTestBlob[0].Trim() -cne $binding.test_blob) {
  throw 'Sampler/test Git blob binding mismatch'
}
$blobHashes = @(& python -c 'import hashlib,json,subprocess,sys; print(json.dumps([hashlib.sha256(subprocess.check_output(["git","cat-file","blob",oid])).hexdigest() for oid in sys.argv[1:]],separators=(",",":")))' $binding.sampler_blob $binding.test_blob)
if ($LASTEXITCODE -ne 0 -or $blobHashes.Count -ne 1) { throw 'Unable to hash sampler Git blobs' }
$blobHashValues = @($blobHashes[0] | ConvertFrom-Json)
if ($blobHashValues.Count -ne 2 -or
    $blobHashValues[0] -cne $binding.sampler_sha256 -or
    $blobHashValues[1] -cne $binding.test_sha256) {
  throw 'Sampler/test SHA-256 binding mismatch'
}
if ([string]::IsNullOrWhiteSpace($GateSealPath) -or
    -not (Test-Path -LiteralPath $GateSealPath -PathType Leaf) -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $GateSealPath).Hash.ToLower() -cne
      $binding.gate_seal_sha256) { throw 'Task 11B gate seal binding mismatch' }
$gateSeal = Get-Content -Raw -LiteralPath $GateSealPath | ConvertFrom-Json
if ($gateSeal.schema -ne 1 -or $gateSeal.outcome -cne 'pass' -or
    $gateSeal.sampler_sha256 -cne $binding.sampler_sha256 -or
    $gateSeal.candidate_image_config -cne $ExpectedConfig -or
    $gateSeal.cleanup.verified_stopped -ne $true) {
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
  "A`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/gate_health_sampler.py"
  "M`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/resume-state-hint.json"
  "A`tdocs/aegis/work/2026-08-31-memory-aware-segmented-transcription-v0.5.0/test_gate_health_sampler.py"
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
& gh run list --repo Herbertmt978/Subgen-English-Plex --limit 25 `
  --json databaseId,headSha,event,status,conclusion,workflowName,createdAt
if ($LASTEXITCODE -ne 0) { throw 'Unable to snapshot hosted-run baseline' }
& gh repo view Herbertmt978/Subgen-English-Plex --json nameWithOwner --jq .nameWithOwner
if ($LASTEXITCODE -ne 0) { throw 'Unable to verify GitHub repository access' }
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
$releaseBlobJson = @(& python -c 'import hashlib,json,subprocess,sys; raw=subprocess.check_output(["git","show",sys.argv[1]]); print(json.dumps({"body":raw.decode("utf-8"),"sha256":hashlib.sha256(raw).hexdigest()},separators=(",",":")))' `
  "$ReleaseCommit`:$ReleaseNotesPath")
if ($LASTEXITCODE -ne 0 -or $releaseBlobJson.Count -ne 1) {
  throw 'Unable to derive immutable release-body provenance'
}
$releaseBlob = $releaseBlobJson[0] | ConvertFrom-Json
if ($releaseBlob.sha256 -cnotmatch '^[0-9a-f]{64}$') {
  throw 'Immutable release-body hash is invalid'
}
function Get-CanonicalReleaseBody([string]$Body) {
  ($Body -replace "`r`n", "`n").TrimEnd("`n")
}
$releaseProbe = @(& gh api --include `
  '/repos/Herbertmt978/Subgen-English-Plex/releases/tags/v0.5.0' 2>&1)
$releaseProbeExit = $LASTEXITCODE
$releaseProbeText = ($releaseProbe | ForEach-Object { [string]$_ }) -join "`n"
$releaseStatuses = @([regex]::Matches(
  $releaseProbeText, '(?m)^HTTP/\S+\s+(\d{3})\b') |
  ForEach-Object { [int]$_.Groups[1].Value })
$authoritativeNotFound = (
  $releaseProbeExit -ne 0 -and
  (($releaseStatuses.Count -gt 0 -and $releaseStatuses[-1] -eq 404) -or
   $releaseProbeText -match '(?m)\(HTTP 404\)\s*$')
)
if ($authoritativeNotFound) {
  $ReleaseBeforeState = 'absent'
} elseif ($releaseProbeExit -eq 0 -and $releaseStatuses.Count -gt 0 -and
          $releaseStatuses[-1] -eq 200) {
  $releaseBeforeJson = @(& gh release view v0.5.0 `
    --repo Herbertmt978/Subgen-English-Plex `
    --json tagName,name,isDraft,isPrerelease,url,body)
  if ($LASTEXITCODE -ne 0 -or $releaseBeforeJson.Count -ne 1) {
    throw 'Unable to inspect the existing GitHub release'
  }
  $releaseBefore = $releaseBeforeJson[0] | ConvertFrom-Json
  if ($releaseBefore.tagName -cne 'v0.5.0' -or
      $releaseBefore.name -cne 'Subgen English Plex v0.5.0' -or
      $releaseBefore.isDraft -or $releaseBefore.isPrerelease -or
      (Get-CanonicalReleaseBody ([string]$releaseBefore.body)) -cne
        (Get-CanonicalReleaseBody ([string]$releaseBlob.body))) {
    throw 'Existing GitHub release is not exact-resumable'
  }
  $ReleaseBeforeState = 'exact'
} else {
  throw 'GitHub release state is neither authoritative-absent nor exact'
}
```

On the Windows simulator, first run this identity-only preflight before pushing
main or the tag. It uses the already sealed build-history/config/manifest
artifacts and does not assume a current checkout. Docker Desktop's `.Id` is the
OCI index here; the runtime configuration digest is independently recomputed
from the exact configuration bytes and cross-bound to the platform manifest.

```powershell
$RuntimeCommit = '4418b3c97296a04b311d29d9ce52abefef64e108'
$Candidate = 'subgen-english-plex:v0.5.0-candidate-4418b3c9'
$IdentityRoot = 'D:\CodexTemp\subgen-v050-task10-4418b3c9\identity'
$ExpectedIdentityHash = '5d3a7e7d5839a9496ef05cddcd8b10c8a71e04f8676243c5ed90ae1968fff87c'
$ExpectedConfig = 'sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae'
$ExpectedPlatform = 'sha256:9e557e124ca6994c4aa30af77301a75d31145e53ec17e6b18997969c67308b5b'
$ExpectedIndex = 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc'
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
& git push origin "${ReleaseCommit}:main"
if ($LASTEXITCODE -ne 0) { throw 'Main push failed' }
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
  & git push origin 'refs/tags/v0.5.0:refs/tags/v0.5.0'
  if ($LASTEXITCODE -ne 0) { throw 'Tag push failed' }
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
$Candidate = 'subgen-english-plex:v0.5.0-candidate-4418b3c9'
$VersionRef = 'ghcr.io/herbertmt978/subgen-english-plex:v0.5.0'
$RuntimeCommit = '4418b3c97296a04b311d29d9ce52abefef64e108'
$ExpectedConfig = 'sha256:d87f84add38521a195957a4b6469f2e30a81331680c4383d60ede8b2c2ca68ae'
$ExpectedIndex = 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc'
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
  $IdentityRoot = 'D:\CodexTemp\subgen-v050-task10-4418b3c9\identity'
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
    & docker.exe tag $ExpectedIndex $VersionRef
    if ($LASTEXITCODE -ne 0) { throw 'Immutable version tag failed' }
    & docker.exe push $VersionRef
    if ($LASTEXITCODE -ne 0) { throw 'Version push failed' }
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

Only after the version digest and clean anonymous HTTP checks pass, materialize
the release body from the immutable `RELEASE_COMMIT` blob into a new owner-only
temporary file and record its SHA-256. Never read the working-tree copy for
publication or verification. An existing release is resumable only when its
tag, title, state, and body are already exact; do not edit a mismatched public
release. Create or accept the release, then prove neither the tag nor release
caused a hosted run:

```powershell
$ReleaseNotesPath = 'docs/RELEASE_NOTES_0.5.0.md'
$ReleaseTempParent = [IO.Path]::GetFullPath($env:TEMP)
$ReleaseBodyRoot = Join-Path $ReleaseTempParent `
  ("subgen-v050-release-body-" + [Guid]::NewGuid().ToString('N'))
$ReleaseBodyRoot = [IO.Path]::GetFullPath($ReleaseBodyRoot)
$releaseTempPrefix = $ReleaseTempParent.TrimEnd('\') + '\'
if (-not $ReleaseBodyRoot.StartsWith($releaseTempPrefix,
      [StringComparison]::OrdinalIgnoreCase) -or
    -not [IO.Path]::GetFileName($ReleaseBodyRoot).StartsWith(
      'subgen-v050-release-body-',[StringComparison]::Ordinal)) {
  throw 'Unsafe release-body temporary path'
}
$ReleaseBodyPath = Join-Path $ReleaseBodyRoot 'release-body.md'
New-Item -ItemType Directory -Path $ReleaseBodyRoot | Out-Null
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $ReleaseBodyRoot /inheritance:r /grant:r "${owner}:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Unable to protect release-body directory' }
try {
  $releaseBodyHash = @(& python -c 'import hashlib,os,subprocess,sys; raw=subprocess.check_output(["git","show",sys.argv[1]]); fd=os.open(sys.argv[2],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); stream=os.fdopen(fd,"wb"); stream.write(raw); stream.flush(); os.fsync(stream.fileno()); stream.close(); print(hashlib.sha256(raw).hexdigest())' `
    "$ReleaseCommit`:$ReleaseNotesPath" $ReleaseBodyPath)
  if ($LASTEXITCODE -ne 0 -or $releaseBodyHash.Count -ne 1 -or
      $releaseBodyHash[0].Trim() -cnotmatch '^[0-9a-f]{64}$' -or
      (Get-FileHash -Algorithm SHA256 -LiteralPath $ReleaseBodyPath).Hash.ToLower() -cne
        $releaseBodyHash[0].Trim()) {
    throw 'Unable to materialize immutable release body'
  }
  $release = @(& gh release view v0.5.0 --repo Herbertmt978/Subgen-English-Plex `
    --json tagName,name,isDraft,isPrerelease,url,body 2>$null)
  if ($LASTEXITCODE -ne 0) {
    & gh release create v0.5.0 --repo Herbertmt978/Subgen-English-Plex `
      --verify-tag --title 'Subgen English Plex v0.5.0' `
      --notes-file $ReleaseBodyPath
    if ($LASTEXITCODE -ne 0) { throw 'Release was neither absent nor exact-resumable' }
    $release = @(& gh release view v0.5.0 --repo Herbertmt978/Subgen-English-Plex `
      --json tagName,name,isDraft,isPrerelease,url,body)
  }
  if ($LASTEXITCODE -ne 0 -or $release.Count -ne 1) {
    throw 'Release verification failed'
  }
  $releaseState = $release[0] | ConvertFrom-Json
  $expectedBody = (Get-Content -Raw -LiteralPath $ReleaseBodyPath) -replace "`r`n", "`n"
  $actualBody = ([string]$releaseState.body) -replace "`r`n", "`n"
  if ($releaseState.tagName -cne 'v0.5.0' -or
      $releaseState.name -cne 'Subgen English Plex v0.5.0' -or
      $releaseState.isDraft -or $releaseState.isPrerelease -or
      $actualBody.TrimEnd("`n") -cne $expectedBody.TrimEnd("`n")) {
    throw 'Existing/published release is not byte-equivalent to RELEASE_COMMIT'
  }
  $releaseRuns = @(& gh run list --repo Herbertmt978/Subgen-English-Plex `
    --commit $ReleaseCommit --json databaseId,event,status,conclusion,workflowName)
  if ($LASTEXITCODE -ne 0 -or $releaseRuns.Count -ne 1) {
    throw 'Unable to verify hosted-run absence'
  }
  if (@($releaseRuns[0] | ConvertFrom-Json).Count -ne 0) {
    throw 'A hosted run was created for the release commit'
  }
  "release_body_sha256=$($releaseBodyHash[0].Trim())"
}
finally {
  Remove-Item -LiteralPath $ReleaseBodyRoot -Recurse -Force -ErrorAction SilentlyContinue
  if (Test-Path -LiteralPath $ReleaseBodyRoot) {
    throw 'Release-body temporary cleanup failed'
  }
}
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
each push, but an uncoordinated external writer can still enter the final
check/push interval. Treat that bounded residual as a release blocker whenever
another publisher may exist; do not claim absolute non-overwrite. A third
digest observed at any check remains unresolved, retains state and lock, and is
never overwritten deliberately. Never delete a registry manifest to simulate
tag rollback. The immutable `v0.5.0` digest remains the deployment identity.

```powershell
$ErrorActionPreference = 'Stop'
$TaskParent = [IO.Path]::GetFullPath('D:\CodexTemp')
$VersionRef = 'ghcr.io/herbertmt978/subgen-english-plex:v0.5.0'
$LatestRef = 'ghcr.io/herbertmt978/subgen-english-plex:latest'
$ExpectedIndex = 'sha256:61dc0b148599f7bdbb9f03118544288a327f1eb15155c68ccf6052b0f9d4c7bc'
$Mode = [string]$env:SUBGEN_LATEST_MODE
if ($Mode -cnotin @('publish','recover')) {
  throw 'SUBGEN_LATEST_MODE must be exactly publish or recover'
}
$LatestStatePath = Join-Path $TaskParent 'subgen-v050-latest-state.json'
$LatestStateTempPath = "$LatestStatePath.next"
$LatestLockPath = Join-Path $TaskParent 'subgen-v050-latest-state.lock'
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrefix = $TaskParent.TrimEnd('\') + '\'
function Assert-OwnerOnlyItem {
  param([Parameter(Mandatory)][string]$Path, [switch]$Directory)
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      ($Directory -and -not $item.PSIsContainer) -or
      (-not $Directory -and $item.PSIsContainer)) {
    throw "Unsafe latest-state object: $Path"
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
    throw "Latest-state ownership/ACL mismatch: $Path"
  }
}
if (Test-Path -LiteralPath $LatestLockPath) {
  $lockItem = Get-Item -LiteralPath $LatestLockPath -Force
  if (($lockItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
      $lockItem.PSIsContainer) { throw 'Unsafe latest-state lock path' }
}
$latestLock = [IO.FileStream]::new(
  $LatestLockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite,
  [IO.FileShare]::None, 1, [IO.FileOptions]::WriteThrough)
& icacls.exe $LatestLockPath /inheritance:r /grant:r "${owner}:F" | Out-Null
if ($LASTEXITCODE -ne 0) {
  $latestLock.Dispose()
  throw 'Unable to protect latest-state lock'
}
Assert-OwnerOnlyItem -Path $LatestLockPath
$RunToken = $null
$DockerConfigRoot = $null
$PriorLatestDigest = $null
$latestState = $null
$LoggedIn = $false
$RegistrySettled = $false
$OperationFailure = $null
try {
  if (Test-Path -LiteralPath $LatestStateTempPath) {
    if ($Mode -cne 'recover') {
      throw 'Recovery must remove the interrupted latest-state replacement'
    }
    Assert-OwnerOnlyItem -Path $LatestStateTempPath
    Remove-Item -LiteralPath $LatestStateTempPath -Force
    if (Test-Path -LiteralPath $LatestStateTempPath) {
      throw 'Interrupted latest-state replacement cleanup failed'
    }
  }
  if ($Mode -ceq 'recover') {
    if (-not (Test-Path -LiteralPath $LatestStatePath -PathType Leaf)) {
      throw 'No committed latest publication state exists to recover'
    }
    Assert-OwnerOnlyItem -Path $LatestStatePath
    $latestState = Get-Content -Raw -LiteralPath $LatestStatePath | ConvertFrom-Json
    $expectedStateKeys = @('docker_config_root','expected_digest','logged_in',
      'mutation_intent','phase','primary_engine_id','prior_digest','process_id',
      'run_token','schema')
    $actualStateKeys = @($latestState.PSObject.Properties.Name | Sort-Object)
    if (@(Compare-Object -CaseSensitive ($expectedStateKeys | Sort-Object) `
          $actualStateKeys).Count -ne 0 -or
        $latestState.schema -cne 'subgen.latest-state/v2' -or
        [string]$latestState.run_token -cnotmatch '^[0-9a-f]{32}$' -or
        [string]$latestState.prior_digest -cnotmatch '^sha256:[0-9a-f]{64}$' -or
        [string]$latestState.expected_digest -cne $ExpectedIndex -or
        $latestState.logged_in -isnot [bool] -or
        $latestState.mutation_intent -isnot [bool]) {
      throw 'Latest recovery state schema/identity mismatch'
    }
    $allowedPhases = @('intent','config_created','logged_in','prior_cached',
      'mutation_intent','local_tagged','push_returned','final_verified',
      'no_mutation_failure','rollback_proven','recovered_prior','recovered_final',
      'logout_proven','config_removed','cleanup_proven')
    if ([string]$latestState.phase -cnotin $allowedPhases) {
      throw 'Latest recovery phase is invalid'
    }
    $recordedPid = 0
    if (-not [int]::TryParse([string]$latestState.process_id, [ref]$recordedPid) -or
        $recordedPid -le 0 -or
        $null -ne (Get-Process -Id $recordedPid -ErrorAction SilentlyContinue)) {
      throw 'Latest recovery PID is invalid or still active'
    }
    $RunToken = [string]$latestState.run_token
    $PriorLatestDigest = [string]$latestState.prior_digest
    $DockerConfigRoot = [IO.Path]::GetFullPath(
      [string]$latestState.docker_config_root)
  } else {
    if ((Test-Path -LiteralPath $LatestStatePath) -or
        (Test-Path -LiteralPath $LatestStateTempPath)) {
      throw 'Identity-bound latest recovery must complete before publication'
    }
    $PriorLatestDigest = [string]$env:SUBGEN_PRIOR_LATEST_DIGEST
    if ($PriorLatestDigest -cnotmatch '^sha256:[0-9a-f]{64}$') {
      throw 'A valid prior latest digest is mandatory'
    }
    $RunToken = [Guid]::NewGuid().ToString('N')
    $DockerConfigRoot = [IO.Path]::GetFullPath(
      (Join-Path $TaskParent "subgen-v050-latest-config-$RunToken"))
  }
  $expectedConfigLeaf = "subgen-v050-latest-config-$RunToken"
  if (-not $DockerConfigRoot.StartsWith($taskPrefix,
        [StringComparison]::OrdinalIgnoreCase) -or
      [IO.Path]::GetFileName($DockerConfigRoot) -cne $expectedConfigLeaf) {
    throw 'Unsafe latest Docker config path'
  }
  $PrimaryEngineId = @(& docker.exe info --format '{{.ID}}')
  if ($LASTEXITCODE -ne 0 -or $PrimaryEngineId.Count -ne 1 -or
      [string]::IsNullOrWhiteSpace($PrimaryEngineId[0])) {
    throw 'Unable to bind latest publication to the primary Docker Engine'
  }
  if ($Mode -ceq 'recover') {
    if ([string]$latestState.primary_engine_id -cne $PrimaryEngineId[0].Trim()) {
      throw 'Latest recovery Docker Engine changed'
    }
  } else {
    $latestState = [ordered]@{
      schema = 'subgen.latest-state/v2'; run_token = $RunToken
      process_id = $PID; primary_engine_id = $PrimaryEngineId[0].Trim()
      prior_digest = $PriorLatestDigest; expected_digest = $ExpectedIndex
      docker_config_root = $DockerConfigRoot; phase = 'intent'
      logged_in = $false; mutation_intent = $false
    }
  }
  function Write-LatestState {
    param([switch]$CreateOnly)
    if (Test-Path -LiteralPath $LatestStateTempPath) {
      throw 'Latest-state temporary path is not exclusive'
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(
      ($latestState | ConvertTo-Json -Depth 5 -Compress))
    $stream = [IO.FileStream]::new(
      $LatestStateTempPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write,
      [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough)
    try {
      $stream.Write($bytes, 0, $bytes.Length)
      $stream.Flush($true)
    }
    finally {
      $stream.Dispose()
    }
    & icacls.exe $LatestStateTempPath /inheritance:r /grant:r "${owner}:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to protect latest-state replacement' }
    Assert-OwnerOnlyItem -Path $LatestStateTempPath
    if ($CreateOnly) {
      if (Test-Path -LiteralPath $LatestStatePath) {
        throw 'Latest state appeared concurrently'
      }
      [IO.File]::Move($LatestStateTempPath, $LatestStatePath)
    } else {
      if (-not (Test-Path -LiteralPath $LatestStatePath -PathType Leaf)) {
        throw 'Latest state disappeared during mutation'
      }
      [IO.File]::Move($LatestStateTempPath, $LatestStatePath, $true)
    }
    Assert-OwnerOnlyItem -Path $LatestStatePath
  }
  if ($Mode -ceq 'publish') {
    Write-LatestState -CreateOnly
  }
  if (Test-Path -LiteralPath $DockerConfigRoot) {
    Assert-OwnerOnlyItem -Path $DockerConfigRoot -Directory
  } else {
    New-Item -ItemType Directory -Path $DockerConfigRoot | Out-Null
    & icacls.exe $DockerConfigRoot /inheritance:r `
      /grant:r "${owner}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Unable to protect latest Docker config' }
    Assert-OwnerOnlyItem -Path $DockerConfigRoot -Directory
    $latestState.phase = 'config_created'
    Write-LatestState
  }
  $env:DOCKER_CONFIG = $DockerConfigRoot
  try {
    $token = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($token)) { throw 'Missing registry token' }
    $token | & docker.exe login ghcr.io --username Herbertmt978 --password-stdin
    $token = $null
    if ($LASTEXITCODE -ne 0) { throw 'GHCR login failed' }
    $LoggedIn = $true
    $latestState.logged_in = $true
    $latestState.phase = 'logged_in'
    Write-LatestState
    $versionDigest = @(& docker.exe buildx imagetools inspect $VersionRef `
      --format '{{.Manifest.Digest}}')
    $versionExit = $LASTEXITCODE
    $currentLatest = @(& docker.exe buildx imagetools inspect $LatestRef `
      --format '{{.Manifest.Digest}}')
    $latestExit = $LASTEXITCODE
    if ($versionExit -ne 0 -or $latestExit -ne 0 -or
        $versionDigest.Count -ne 1 -or
        $versionDigest[0].Trim() -cne $ExpectedIndex -or
        $currentLatest.Count -ne 1) {
      throw 'Version/latest state could not be bound before latest handling'
    }
    $currentLatestDigest = $currentLatest[0].Trim()
    if ($currentLatestDigest -cne $ExpectedIndex -and
        $currentLatestDigest -cne $PriorLatestDigest) {
      throw 'Current latest is neither the recorded prior nor exact final digest'
    }
    if ($Mode -ceq 'recover') {
      $latestState.phase = if ($currentLatestDigest -ceq $ExpectedIndex) {
        'recovered_final'
      } else {
        'recovered_prior'
      }
      $RegistrySettled = $true
      Write-LatestState
    } elseif ($currentLatestDigest -ceq $ExpectedIndex) {
      $latestState.phase = 'final_verified'
      $RegistrySettled = $true
      Write-LatestState
    } else {
      $priorRef = "ghcr.io/herbertmt978/subgen-english-plex@$PriorLatestDigest"
      & docker.exe pull $priorRef
      if ($LASTEXITCODE -ne 0) { throw 'Unable to cache prior latest for rollback' }
      $latestState.phase = 'prior_cached'
      Write-LatestState
      $latestState.mutation_intent = $true
      $latestState.phase = 'mutation_intent'
      Write-LatestState
      & docker.exe tag $ExpectedIndex $LatestRef
      if ($LASTEXITCODE -ne 0) { throw 'Immutable latest tag failed' }
      $latestState.phase = 'local_tagged'
      Write-LatestState
      & docker.exe push $LatestRef
      if ($LASTEXITCODE -ne 0) { throw 'Latest push failed' }
      $latestState.phase = 'push_returned'
      Write-LatestState
      $provedLatest = @(& docker.exe buildx imagetools inspect $LatestRef `
        --format '{{.Manifest.Digest}}')
      if ($LASTEXITCODE -ne 0 -or $provedLatest.Count -ne 1 -or
          $provedLatest[0].Trim() -cne $ExpectedIndex) {
        throw 'Latest digest mismatch'
      }
      $latestState.phase = 'final_verified'
      $RegistrySettled = $true
      Write-LatestState
    }
  }
  catch {
    $OperationFailure = $_
    if ($Mode -ceq 'publish' -and $latestState.mutation_intent) {
      $latestAfterFailure = @(& docker.exe buildx imagetools inspect $LatestRef `
        --format '{{.Manifest.Digest}}' 2>$null)
      if ($LASTEXITCODE -ne 0 -or $latestAfterFailure.Count -ne 1) {
        throw 'Latest mutation failed and registry state is unknown; state retained'
      }
      $latestAfterFailureDigest = $latestAfterFailure[0].Trim()
      if ($latestAfterFailureDigest -ceq $ExpectedIndex) {
        & docker.exe tag `
          "ghcr.io/herbertmt978/subgen-english-plex@$PriorLatestDigest" $LatestRef
        if ($LASTEXITCODE -ne 0) {
          throw 'Latest mutation failed and prior digest could not be retagged'
        }
        & docker.exe push $LatestRef | Out-Null
        if ($LASTEXITCODE -ne 0) {
          throw 'Latest mutation failed and prior digest restore push failed'
        }
      } elseif ($latestAfterFailureDigest -cne $PriorLatestDigest) {
        throw 'Latest changed to an unrecognized digest; refusing rollback overwrite'
      }
      $restored = @(& docker.exe buildx imagetools inspect $LatestRef `
        --format '{{.Manifest.Digest}}' 2>$null)
      if ($LASTEXITCODE -ne 0 -or $restored.Count -ne 1 -or
          $restored[0].Trim() -cne $PriorLatestDigest) {
        throw 'Latest mutation failed and prior digest could not be restored'
      }
      $latestState.mutation_intent = $false
      $latestState.phase = 'rollback_proven'
      $RegistrySettled = $true
      Write-LatestState
    } elseif ($Mode -ceq 'publish') {
      $latestState.phase = 'no_mutation_failure'
      $RegistrySettled = $true
      Write-LatestState
    }
  }
  finally {
    $token = $null
    $latestCleanupFailure = $null
    if ($LoggedIn) {
      & docker.exe logout ghcr.io 2>$null | Out-Null
      if ($LASTEXITCODE -ne 0) {
        $latestCleanupFailure = 'Latest task-scoped registry logout failed'
      } else {
        $LoggedIn = $false
        $latestState.logged_in = $false
        $latestState.phase = 'logout_proven'
        Write-LatestState
      }
    }
    if ($null -eq $latestCleanupFailure) {
      Assert-OwnerOnlyItem -Path $DockerConfigRoot -Directory
      Remove-Item -LiteralPath $DockerConfigRoot -Recurse -Force
      if (Test-Path -LiteralPath $DockerConfigRoot) {
        $latestCleanupFailure = 'Latest task-scoped Docker config cleanup failed'
      } else {
        $latestState.phase = 'config_removed'
        Write-LatestState
      }
    }
    Remove-Item Env:DOCKER_CONFIG -ErrorAction SilentlyContinue
    if ($RegistrySettled -and $null -eq $latestCleanupFailure) {
      $latestState.phase = 'cleanup_proven'
      Write-LatestState
      Remove-Item -LiteralPath $LatestStatePath -Force
      if (Test-Path -LiteralPath $LatestStatePath) {
        $latestCleanupFailure = 'Latest state cleanup failed'
      }
    }
    if ($null -ne $latestCleanupFailure) { throw $latestCleanupFailure }
  }
  if ($null -ne $OperationFailure) { throw $OperationFailure }
}
finally {
  if ($null -ne $latestLock) { $latestLock.Dispose() }
  if (-not (Test-Path -LiteralPath $LatestStatePath)) {
    Remove-Item -LiteralPath $LatestLockPath -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $LatestLockPath) {
      throw 'Latest-state lock cleanup failed'
    }
  }
}
```

After `latest` is exact, perform the simulator lifecycle closeout from the
controller. First rerun the active-user, pytest/Python/build/buildx, Docker
container, other task-marker, and power-ownership checks. Recover any fixed
release/latest state record through the identity-bound cleanup above; require
no task-scoped container, volume, Docker credential directory, or state file to
remain. Recheck the `Ubuntu-24.04` rootful Engine ID, local Unix-socket-only
boundary, and empty container/image/volume sets. Require the token-derived WSL
run/config paths absent. If the v2 state says the distribution was stopped
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
hard/no-swap limit, existing low CPU priority/pids/OOM adjustment, preserved
generation registry, and classified-failure monitor. Start with both deletion
switches false and repair report/timer inactive. Use the explicit positive
audit-derived priority reserve; `auto` blocks deployment. Verify the published
image's config digest and ordered diff IDs against the owner-only identity
artifact. Pull and address the image only by its published index digest.

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
failed-file deletion false, and invalid-media deletion true. Inspect the full
ID before start: exact image config, command, environment, 10 GiB hard/no-swap
boundary, labels, and mounts. No production-media source may appear. Start only
that ID, require marker-before-delete for the invalid fixture, require the
silent control to remain, then stop/remove only the reverified ID and remove the
disposable root. Any disagreement or cleanup uncertainty blocks deployment.

Next render the exact production Compose/environment, initially with both
deletion switches false. Create `subgen` stopped, bind its full ID, and inspect
the exact digest/config/diff IDs, command, effective environment, cgroup/CPU/
pids/OOM policy, read-only identity/catalog mounts, production media mounts,
state/model mounts, restart policy, and stopped state against the owner-only
deployment boundary. Only then start that ID. After the deletion-off startup
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
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
# GPU_MEMORY_RESERVE_GIB is the positive value recorded by the released audit;
# `auto` is prohibited on this host.
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
inactive; one successful long GPU transcription; idle-resident unload; marker
skips for retained failures only when the compatibility proof supports them;
10 GiB effective cap; no OOM/restart/host/PSI/GPU regression; and every
audit-recorded Frigate FPS/detector/embedding/health threshold. Use passive
production observation only; do not inject synthetic GPU pressure.

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
- No legacy or recovery route bypasses invalid-media classification.
- Tasks define exact files, contracts, commands, expected evidence, and scoped
  commit boundaries.
- Verification covers unit, integration, package, Linux/cgroup, real inference,
  release, and production without real-media destructive tests.
- Result: `proceed`; live Frigate deployment remains separately gated by Task
  11B's representative priority-reserve evidence.

## Execution Readiness View

- Intent Lock: bounded transcription, highest-safe fixed model, cooperative
  yield, first-failure marker, invalid-media-only optional deletion, v0.5.0.
- Scope Fence: no Sonarr/Radarr API, no Ollama coordinator, no arbitrary model
  downgrade, no marker schema v2, no public mutation API.
- Compatibility Lock: routes/outputs/queue/webhook/markers preserved; legacy
  destructive inputs accepted but safely narrowed.
- Owner Lock: resource module; segmentation module; model runtime; media;
  monitor; existing marker and unlink owners.
- Review Gates: focused tests and scoped diff per task; full local; full idle
  simulator; isolated pre-publication Frigate candidate; no-runner proof;
  digest/release proof; passive Frigate production observation; and Plex
  retirement verification.
- Rewind Rules: owner/deletion drift returns to design; focused failure returns
  to owning task; simulator disproval updates evidence-bound policy; rollout
  failure invokes deletion-off rollback.
- Evidence: commands/results, issue/release URLs, commits/tag/digest, no hosted
  runs, Plex retirement, and Frigate HTTP/model/chunk/host/GPU/OOM/restart/
  scan/monitor/health/rollback state.

## Risks

- stable-ts is archived and callback/result construction behavior may differ in
  the packaged version; simulator gates callback propagation and real result
  rendering before release.
- Callback yielding cannot stop allocations before first progress; conservative
  baseline chunks and preflight pressure remain mandatory.
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
