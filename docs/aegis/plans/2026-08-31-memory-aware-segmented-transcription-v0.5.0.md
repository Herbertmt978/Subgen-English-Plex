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
model/chunk policy, admission math, pressure sampling, hysteresis, and adaptive
retry state. `subgen_core/model_envelope_catalog.py` owns catalog and runtime
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
  `tests/test_segmentation.py`, and `tests/test_media_validation.py`.
- Facade, monitor, repair, model runtime, transcription, and media edits are
  restricted to owner logic or wiring. No opportunistic refactor is allowed.
- Result: `at-risk but governed`.

## Plan-Time Complexity Check

- Resource policy and segmentation are cohesive new boundaries, not helpers
  split for line-count aesthetics.
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
- `subgen_core/model_envelope_catalog.py`
- `profile_model_envelopes.py`
- `subgen_core/segmentation.py`
- `tests/test_resource_management.py`
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
- Shift structured segments/words to source time and assign by midpoint; final
  core owns the exact end. Failed/yielded chunks append nothing.
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

**Files:** create `subgen_core/resource_management.py` and
`tests/test_resource_management.py`.

**Steps:** implement injected readers/time/sleep; capacity/model/chunk/reserve
functions; `MemoryPressureYield`; allocation recognition; `PressureController`;
and `AdaptiveChunkState`. Consume only validated immutable catalog entries.
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
python -m pytest -q tests/test_resource_management.py
git diff --check
```

**Expected:** focused tests pass. Commit only these two files as
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
configuration digest plus ordered rootfs layer diff IDs. A tag, local image ID
label, or registry manifest digest alone is insufficient. Save the image only
after the identity record exists and transfer the record, archive, and checksum
together.

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
python -m pytest -q
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
docker build --pull --label org.opencontainers.image.version=0.5.0 --label org.opencontainers.image.revision="$RELEASE_COMMIT" -t subgen-english-plex:v0.5.0-candidate .
identity_root="$(mktemp -d /tmp/subgen-v050-identity.XXXXXX)"
identity_json="$identity_root/image-identity.json"
image_archive="$identity_root/subgen-v050-candidate.tar"
docker image inspect --format '{"schema":"subgen.model-envelope.identity/v1","image_identity":{"config_digest":"{{.Id}}","layer_diff_ids":{{json .RootFS.Layers}}}}' \
  subgen-english-plex:v0.5.0-candidate >"$identity_json"
python - "$identity_json" <<'PY'
import json, re, sys
value = json.load(open(sys.argv[1], encoding="ascii"))
assert set(value) == {"schema", "image_identity"}
assert value["schema"] == "subgen.model-envelope.identity/v1"
identity = value["image_identity"]
assert set(identity) == {"config_digest", "layer_diff_ids"}
assert re.fullmatch(r"sha256:[0-9a-f]{64}", identity["config_digest"])
assert identity["layer_diff_ids"]
assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in identity["layer_diff_ids"])
PY
chmod 0600 "$identity_json"
docker save --output "$image_archive" subgen-english-plex:v0.5.0-candidate
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

**Steps:** after the blocker is resolved and ownership is released, capture the
live baseline and preserve the complete v0.3.0 Compose/config, model cache,
generation registry, application state, OCI config digest/ordered diff IDs, and
the enabled/active states of `subgen-monitor.service`, `subgen-repair.timer`, and
`subgen-repair.service`. Stop/disable the legacy monitor and repair timer/service
before stopping the old Subgen container, and verify all three units inactive
and the monitor/timer disabled. Restore captured unit states only during a
deletion-off v0.3.0 rollback, never for the v0.5 candidate.

```bash
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
  test "$(docker image inspect --format '{{.Id}}' subgen-english-plex:v0.5.0-candidate)" = "$expected_config"
  test "$(docker image inspect --format '{{json .RootFS.Layers}}' subgen-english-plex:v0.5.0-candidate)" = "$expected_layers"
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

```bash
python /subgen/profile_model_envelopes.py \
  --catalog-input "$MODEL_ENVELOPE_CATALOG" \
  --catalog-output /profile-output/catalog.json \
  --identity "$MODEL_ENVELOPE_IDENTITY" \
  --model large-v3 --runs 3 --media /profile-input/long-translation.wav
```

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

**Steps:** record the privacy-safe candidate evidence, accept ADR 0002 only
after every gate passes, rerun the structural checker and `git diff --check`,
and commit the completed evidence as `Record v0.5.0 verification evidence`.

**Expected:** the exact publishable digest and all release gates are complete.

### Task 12: Publish v0.5.0 without hosted runners

**Systems:** GitHub repository/release and GHCR.

**Steps:** prove workflows remain manual-only and snapshot run history. Fetch;
require zero remote-side divergence; fast-forward verified history to `main`;
prove remote identity and no new run; create annotated tag. On the idle
simulator, tag and securely push the already verified image as `v0.5.0` and
`latest` using a private task-scoped Docker configuration with guaranteed
cleanup; require the same manifest digest and pull-smoke. Create release last.
The local candidate and remotely pulled image must equal Task 11B's OCI config
digest and ordered layer diff IDs; publication blocks on any mismatch. Registry
manifest equality between version/latest is an additional tag check, not the
candidate identity test.

```powershell
rg -n "workflow_dispatch|pull_request|push:|release:" .github/workflows
gh run list --repo Herbertmt978/Subgen-English-Plex --limit 20 --json databaseId,headSha,event,status,conclusion,workflowName,createdAt
git fetch origin main --tags
git rev-list --left-right --count origin/main...HEAD
git push origin HEAD:main
git fetch origin main
git rev-parse HEAD
git rev-parse origin/main
gh run list --repo Herbertmt978/Subgen-English-Plex --commit (git rev-parse HEAD) --json databaseId,event,status,conclusion,workflowName
git tag -a v0.5.0 origin/main -m "Subgen English Plex 0.5.0"
git push origin v0.5.0
```

On the simulator, recheck that it is idle and that the candidate image label
matches the release commit. Use the already authorized `gh` credential through
stdin into a mode-700 task-scoped `DOCKER_CONFIG`; never print it, put it in an
argument, persist it in the normal user Docker configuration, or retain it
after the command. A guaranteed trap logs out and removes the private config,
smoke container, and verified temporary root on success, error, or interruption.
If the credential lacks GHCR package-write authority, stop at that boundary.

```bash
set -euo pipefail
test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
test "$(docker image inspect subgen-english-plex:v0.5.0-candidate --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$RELEASE_COMMIT"
expected_config="$(python -c "import json; print(json.load(open('image-identity.json'))['image_identity']['config_digest'])")"
expected_layers="$(python -c "import json; print(json.dumps(json.load(open('image-identity.json'))['image_identity']['layer_diff_ids'], separators=(',', ':')))")"
test "$(docker image inspect --format '{{.Id}}' subgen-english-plex:v0.5.0-candidate)" = "$expected_config"
test "$(docker image inspect --format '{{json .RootFS.Layers}}' subgen-english-plex:v0.5.0-candidate)" = "$expected_layers"
gh auth status
smoke_name=subgen-v050-release-smoke
smoke_root=""
docker_config_root="$(mktemp -d /tmp/subgen-v050-docker-config.XXXXXX)"
chmod 700 "$docker_config_root"
export DOCKER_CONFIG="$docker_config_root"
cleanup_release_smoke() (
  set +e
  docker rm -f "$smoke_name" >/dev/null 2>&1 || true
  docker logout ghcr.io >/dev/null 2>&1 || true
  if [ -n "$smoke_root" ]; then
    case "$smoke_root" in
      /tmp/subgen-v050-release.*) rm -rf -- "$smoke_root" ;;
      *) echo "Refusing unexpected smoke cleanup path" >&2 ;;
    esac
  fi
  case "$docker_config_root" in
    /tmp/subgen-v050-docker-config.*) rm -rf -- "$docker_config_root" ;;
    *) echo "Refusing unexpected Docker config cleanup path" >&2 ;;
  esac
)
trap cleanup_release_smoke EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
set +x
gh auth token | docker login ghcr.io --username Herbertmt978 --password-stdin
docker tag subgen-english-plex:v0.5.0-candidate ghcr.io/herbertmt978/subgen-english-plex:v0.5.0
docker tag subgen-english-plex:v0.5.0-candidate ghcr.io/herbertmt978/subgen-english-plex:latest
docker push ghcr.io/herbertmt978/subgen-english-plex:v0.5.0
docker push ghcr.io/herbertmt978/subgen-english-plex:latest
version_digest="$(docker buildx imagetools inspect ghcr.io/herbertmt978/subgen-english-plex:v0.5.0 --format '{{.Manifest.Digest}}')"
latest_digest="$(docker buildx imagetools inspect ghcr.io/herbertmt978/subgen-english-plex:latest --format '{{.Manifest.Digest}}')"
test -n "$version_digest"
test "$version_digest" = "$latest_digest"
published_ref="ghcr.io/herbertmt978/subgen-english-plex@$version_digest"
docker pull "$published_ref"
test "$(docker image inspect --format '{{.Id}}' "$published_ref")" = "$expected_config"
test "$(docker image inspect --format '{{json .RootFS.Layers}}' "$published_ref")" = "$expected_layers"

smoke_root="$(mktemp -d /tmp/subgen-v050-release.XXXXXX)"
mkdir -p "$smoke_root/media" "$smoke_root/models" "$smoke_root/monitor"
docker rm -f "$smoke_name" >/dev/null 2>&1 || true
docker run -d --name "$smoke_name" --memory=9g --memory-swap=9g --cpus=4 \
  -p 127.0.0.1:19000:9000 \
  -e MONITOR=False -e PROCESS_ADDED_MEDIA=False -e SKIP_STARTUP_SCAN=True \
  -e TRANSCRIBE_FOLDERS=/media -e MODEL_PATH=/subgen/models \
  -v "$smoke_root/media:/media:ro" \
  -v "$smoke_root/models:/subgen/models" \
  -v "$smoke_root/monitor:/opt/subgen/monitor" \
  ghcr.io/herbertmt978/subgen-english-plex:v0.5.0
curl --fail --silent --show-error --retry 30 --retry-delay 2 --retry-connrefused \
  http://127.0.0.1:19000/status
test "$(docker inspect --format '{{.RestartCount}}' "$smoke_name")" = "0"
printf '%s\n' "$version_digest"
```

Only after those digest and HTTP checks pass, create the public release and
prove neither tag nor release caused a hosted run:

```powershell
gh release create v0.5.0 --repo Herbertmt978/Subgen-English-Plex --verify-tag --title "Subgen English Plex v0.5.0" --notes-file docs/RELEASE_NOTES_0.5.0.md
gh run list --repo Herbertmt978/Subgen-English-Plex --commit (git rev-parse HEAD) --json databaseId,event,status,conclusion,workflowName
```

**Expected:** tag/main/candidate identity, no hosted runs, identical version and
latest GHCR digest, successful pull/HTTP smoke, and live release.

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
artifact with host-side `docker image inspect` as the immediately preceding
command before the container start, mount identity and catalog read-only, and
require their strict runtime match. Run the isolated invalid-media delete
smoke, then enable canonical invalid deletion.

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
production observation only; do not inject synthetic GPU pressure. If rollout
fails, set both deletion booleans false and repair report before restoring the
preserved v0.3.0 config, model cache, and image digest. Retain v0.5 state as
audit evidence and do not start, stop, or reconfigure Frigate or Ollama.

Recheck the Plex VM independently: no Subgen container/process/port, monitor
inactive/disabled, and Plex HTTP 200. Never recreate the retired Plex instance.

**Expected:** healthy immutable deployment and intact tested v0.3.0 operational
rollback. Public rollback guidance remains v0.4.1 with deletion off. Close the
public issue only after release, Frigate observation, and Plex retirement
evidence are complete.

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
- Independent chunk timestamps can disagree at boundaries; midpoint ownership
  plus monotonic rejection is tested against synthetic long media.
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
