# Memory-Aware Segmented Transcription v0.5.0 Implementation Plan

Status: `approved for execution 2026-08-31`
Date: `2026-08-31`

## Goal

Release Subgen English for Plex `v0.5.0` with bounded sequential audio
segmentation, deterministic highest-safe automatic Whisper model selection,
cooperative memory-pressure yielding, and conservative media validation.
Public installs mark and skip the exact failed generation after the first
qualifying terminal failure but never delete by default. The Plex deployment
deletes on the first failure only when FFprobe and isolated PyAV independently
classify the same unchanged generation as invalid media; all silent,
indeterminate, inference, resource, and native-crash failures are retained.

## Architecture

`subgen_core/resource_management.py` becomes the pure owner of capacity,
model/chunk policy, pressure sampling, hysteresis, and adaptive retry state.
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
- Docker/Compose cgroup v2 on the workstation, simulator PC, and Plex VM
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
  FFprobe and PyAV cannot parse; package v0.5.0; deploy Plex with first-failure
  invalid-media deletion; perform all tests locally or on the idle simulator
  and never on GitHub-hosted runners

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

## TDD Route

- Mode: `off`
- Decision: `skipped`
- Strict authority: `not applicable`
- Test posture: `post-change regression`
- Reason: no explicit strict-TDD authority exists; each cohesive implementation
  slice receives focused regression tests before its commit.
- Verification: focused local tests per slice, complete local suite, complete
  idle-simulator Linux suite/build/inference/pressure/safety smokes, then
  immutable Plex rollout observation.

## Verification

Fresh evidence must cover model/chunk boundaries; cgroup/host/PSI pressure;
yield, unload, same-core retry, recovery, and cancellation; structured overlap
merge and atomic output; the dual-validator truth table and generation race;
marker-before-delete; every retained failure class; repair migration; package
parity; HTTP health; 4, 6, and 9 GiB real inference; no OOM/restart; release
identity/digest; and Plex startup-scan progress. Publication cannot begin until
all local and simulator checks pass.

## Aegis Visibility

Planning is required because this change introduces two canonical modules, a
cross-process typed failure boundary, adaptive state, destructive-policy
migration, package defaults, a public release, and a production rollout.

## Plan Basis

The current runtime loads one model and transcribes an entire selected audio
stream, sometimes materializing that stream in memory. Progress callbacks do
not sample pressure. PyAV admission reduces failures to a boolean. Generic
worker/file events cause the monitor to mark and optionally delete, while the
repair utility can independently delete crash candidates. Existing bounded
FFmpeg segment extraction, model locks, inference semaphore, exact marker
identity, and descriptor-relative secure deletion are reusable foundations.

## BaselineUsageDraft

- Required: approved v0.5 design, initial baseline, governance, contribution
  rules, current tests, live 9 GiB Plex constraints.
- Delivered: all required references and the user-approved safety boundary.
- Missing: none that blocks planning.
- Decision: `continue`.

## Requirement Ready Check

- Goal: long files complete within bounded memory and yield to other services.
- Public scenario: automatic highest-safe model, segmentation, first-failure
  marker/skip, deletion off.
- Plex scenario: 9 GiB cap, automatic `medium`/20-minute profile, startup scan,
  first-failure deletion only for unchanged dual-invalid media.
- Release scenario: locally/simulator-verified v0.5.0 tag, release, GHCR image,
  immutable deployment, rollback to v0.4.1 with all deletion disabled first.
- Open blocker questions: none.
- Decision: `ready`.

## Ripple Signal Triage

The model decision flows through configuration, status, loading, logging,
Compose, docs, and tests. Chunking flows through media metadata, queue payloads,
transcription, callbacks, model release, output, and webhook/task completion.
Failure classification flows through admission, event schema, monitor state,
marker mapping, delete recovery, repair migration, examples, systemd text, and
release notes. Packaging/version/publication/Plex deployment are therefore all
in scope; Plex/Sonarr/Radarr APIs and Ollama orchestration are not.

## Change Necessity

- Input duration currently permits whole-stream memory growth; a higher cap
  alone cannot provide a bounded public solution.
- Container priority cannot dynamically reclaim already allocated model/audio
  memory; a cooperative callback/release/retry path is required.
- A path or generic parser error cannot distinguish corrupt media from a
  transient validator or resource failure; independent typed validation is
  required before deletion.
- Minimum code boundary: two focused core modules, narrow owner integration,
  typed event/state migration, config/package/docs/version/release surfaces.
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
  candidate can reach unlink; if found, return to the approved design.
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
- `subgen_core/segmentation.py`
- `tests/test_resource_management.py`
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
- `.env.example`, `monitor.env.example`, all three Compose files,
  `systemd/subgen-repair.service`
- `README.md`, `docs/CONFIGURATION.md`, `docs/INSTALL.md`,
  `docs/MIGRATION.md`, `SECURITY.md`, `CONTRIBUTING.md`
- `CHANGELOG.md`, `VERSION`, `docs/aegis/INDEX.md`, baseline governance evidence

## Contracts to Implement

### Resource policy

- Immutable `CapacityProfile`, `ModelDecision`, and `PressureSample` values.
- Pure discovery supports finite cgroup v2/v1 limits and physical fallback.
- CPU RAM ceilings: `<2` tiny, `2–<4` base, `4–<8` small, `8–<16` medium,
  `>=16` large-v3; unknown selects small.
- GPU uses the lower RAM/VRAM ceiling: `<2` tiny, `2–<3` base, `3–<7`
  small, `7–<12` medium, `>=12` large-v3; unknown selects small.
- Initial chunks: `<4` GiB 5m, `4–<8` 10m, `8–<16` 20m, `>=16` 30m,
  unknown 10m; explicit values are 5–60 minutes.
- Automatic reserve is `min(max(1 GiB, 15% host), 25% capacity)` and finite
  cgroups retain `max(512 MiB, 10% limit)` headroom.
- Samples are throttled to five seconds; pressure requires two sustained
  samples, critical headroom/oom yields immediately, recovery requires three
  healthy samples. Waits are cancellation-aware 5–60 second backoff.
- Yield does not count as a media failure. Two healthy minimum-chunk allocation
  failures become `resource_exhaustion`; model-load failures are profile-level
  and never mark/delete a file.
- The model runtime has one single-flight release state. New admissions wait
  while state is `yielding` or `recovering`; concurrent release requests join
  the active release rather than draining separate permit subsets.

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

### Task 2: Implement pure resource and adaptive policy

**Files:** create `subgen_core/resource_management.py` and
`tests/test_resource_management.py`.

**Steps:** implement injected readers/time/sleep; capacity/model/chunk/reserve
functions; `MemoryPressureYield`; allocation recognition; `PressureController`;
and `AdaptiveChunkState`. Cover cgroup v2/v1/unbounded/physical fallback, every
tier boundary, explicit model, CPU/GPU lower ceiling, unknown capacity,
reserve/floor, sample throttling, sustained/critical/recovery hysteresis,
cancellation, shrink/grow, and false-positive OOM text.

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTEST_PLUGINS='requests_mock.contrib._pytest_plugin'
python -m pytest -q tests/test_resource_management.py
git diff --check
```

**Expected:** focused tests pass. Commit only these two files as
`Add memory-aware resource policy`.

### Task 3: Integrate model selection and safe pressure release

**Files:** modify `subgen_core/model_runtime.py`, `subgen_override.py`,
`tests/test_custom_reliability.py`, and `tests/test_module_boundaries.py`.

**Steps:** parse settings once and reject invalid chunk/reserve values at
startup; log/expose the resolved profile; preserve explicit model choices; add
cache cleanup and a single-flight release coordinator that closes inference
admission, owns every permit, and then takes `model_load_lock`; recheck/load the
model after entering the gate; compose pressure callback with progress;
distinguish profile-level model-load failure from file failure. Concurrent
release requests join the active transition; new work waits through `yielding`
and `recovering`.

```powershell
python -m pytest -q tests/test_resource_management.py tests/test_custom_reliability.py -k "model or pressure or release or reload"
python -m pytest -q tests/test_module_boundaries.py
python -m compileall -q subgen_override.py subgen_core/model_runtime.py subgen_core/resource_management.py
git diff --check
```

**Expected:** explicit/auto decisions, invalid-setting startup failures,
all-permit lock order, two simultaneous yields plus one queued worker,
preflight admission blocking, reload race, and callback composition pass.
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

**Files:** examples, all Compose profiles, systemd repair text, packaging and
module tests, public docs, `CHANGELOG.md`, `VERSION`, release notes, ADR, work
record, Aegis index/baseline evidence. `Dockerfile` changes only if packaging
tests disprove its existing whole-package copy.

**Steps:** set public model/segmentation/pressure defaults; set marker/delete
defaults; move packaged image to v0.5.0 while retaining public 10 GiB memory;
document 4/6/9 GiB tiers and conservative deletion; document stable runtime
status version versus project version; record repair/legacy migration and
v0.4.1 rollback sequence; update local/simulator-only policy. Document
`SEGMENTATION_ENABLED=False`, unchanged upload APIs, and startup rejection of
invalid chunk/reserve settings. Create ADR 0002 as proposed here and accept it
only in Task 11 after complete local/simulator evidence.

```powershell
python -m pytest -q tests/test_packaging.py tests/test_module_boundaries.py
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
rg -n "0\.4\.1|v0\.4\.1|WHISPER_MODEL|AUTO_DELETE_FAILED_FILES|AUTO_DELETE_INVALID_MEDIA|SUBGEN_REPAIR_ACTION" README.md docs .env.example monitor.env.example docker-compose*.yml systemd tests VERSION CHANGELOG.md
git diff --check
```

**Expected:** package parity, manual-only workflows, docs/version contracts,
Compose, and compilation pass. Commit as `Prepare Subgen English Plex 0.5.0`.

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

Generate only disposable synthetic media. Run constrained real CPU inference:
4 GiB -> small/10m; 6 GiB -> small/10m; 9 GiB -> medium/20m. Under a separately
capped pressure helper prove yield, unload, same-core smaller retry, recovery,
completion, zero restart/OOM, no partial output, and monotonic overlap merge.
Prove silent retain; dual-invalid classify; disagreement/timeout/permission
retain; isolated marker-before-delete and replacement; cross-bind identity.

```bash
python -m pytest -q
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
docker build --pull --label org.opencontainers.image.version=0.5.0 --label org.opencontainers.image.revision="$RELEASE_COMMIT" -t subgen-english-plex:v0.5.0-candidate .
docker inspect --format '{{.RestartCount}}' subgen
docker exec subgen cat /sys/fs/cgroup/memory.peak
docker exec subgen cat /sys/fs/cgroup/memory.events
docker exec subgen cat /sys/fs/cgroup/memory.pressure
```

**Expected:** all gates pass. If NVIDIA is unavailable, record GPU behavior as
mocked/Compose-only rather than claiming a real GPU smoke. Shut down only if
this task woke the simulator and a final activity check is clear.

### Task 11: Reconcile evidence and finalize governance

**Files:** source/tests only if simulator disproves a contract; otherwise ADR,
baseline/work verification, index, and plan status.

**Steps:** adjust any evidence-bound tier/threshold through the owning task,
rerun focused/full local and simulator checks, then accept ADR 0002 and record
commands/results/digests without private paths, media names, or credentials.
Run the Aegis structural checker and record pre-existing workspace-format drift
separately rather than rewriting unrelated v0.4 history.

```powershell
python C:\Users\Ashby\.codex\aegis\scripts\aegis-workspace.py check --root .
git diff --check
```

**Expected:** implementation evidence is complete; any checker failure is only
the already identified pre-existing governance-format drift. Commit evidence
as `Record v0.5.0 verification evidence`.

### Task 12: Publish v0.5.0 without hosted runners

**Systems:** GitHub repository/release and GHCR.

**Steps:** prove workflows remain manual-only and snapshot run history. Fetch;
require zero remote-side divergence; fast-forward verified history to `main`;
prove remote identity and no new run; create annotated tag. On the idle
simulator, tag and securely push the already verified image as `v0.5.0` and
`latest` using a private task-scoped Docker configuration with guaranteed
cleanup; require the same manifest digest and pull-smoke. Create release last.

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
version_digest="$(docker buildx imagetools inspect ghcr.io/herbertmt978/subgen-english-plex:v0.5.0 --format '{{json .Manifest.Digest}}' | tr -d '"')"
latest_digest="$(docker buildx imagetools inspect ghcr.io/herbertmt978/subgen-english-plex:latest --format '{{json .Manifest.Digest}}' | tr -d '"')"
test -n "$version_digest"
test "$version_digest" = "$latest_digest"
docker pull ghcr.io/herbertmt978/subgen-english-plex:v0.5.0

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

### Task 13: Back up, deploy, and observe Plex

**Systems:** Plex deployment/config/state/systemd/Docker. No real media may be
deliberately damaged or deleted for testing.

**Steps:** inspect live state first. Back up owner-only config, modules, units,
state, and v0.4.1 digest outside media. Stop only the monitor during replacement.
Deploy container/monitor together by immutable v0.5 digest, initially with both
deletion booleans false and repair report/timer inactive. Preserve 9 GiB
hard/no-swap, CPU priority, pids, and OOM adjustment. Run isolated disposable
invalid-media delete smoke, then enable canonical invalid deletion.

Target effective settings:

```dotenv
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
SKIP_STARTUP_SCAN=False
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_INVALID_MEDIA=true
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

Require HTTP 200, model medium, 20-minute baseline, scan progress, monitor
heartbeat, repair timer inactive, one successful long transcription, marker
skips for retained failures, 9 GiB effective cap, and no OOM/restart/PSI
regression. If rollout fails, set both deletion booleans false and repair report
before restoring v0.4.1 code/digest; retain v0.5 state as audit evidence.

**Expected:** healthy immutable deployment and intact tested rollback. Close
the public issue only after release and Plex observation evidence are complete.

## Plan Pressure Test

- One owner exists for each new policy and destructive decision.
- No legacy or recovery route bypasses invalid-media classification.
- Tasks define exact files, contracts, commands, expected evidence, and scoped
  commit boundaries.
- Verification covers unit, integration, package, Linux/cgroup, real inference,
  release, and production without real-media destructive tests.
- Result: `proceed`.

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
  simulator; no-runner proof; digest/release proof; Plex observation.
- Rewind Rules: owner/deletion drift returns to design; focused failure returns
  to owning task; simulator disproval updates evidence-bound policy; rollout
  failure invokes deletion-off rollback.
- Evidence: commands/results, issue/release URLs, commits/tag/digest, no hosted
  runs, HTTP/model/chunk/memory/OOM/restart/scan/monitor/rollback state.

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
- Model tiers are hypotheses until constrained inference succeeds; evidence may
  lower, never silently raise, a tier before release.
- Missing simulator/GHCR credentials or another active workload is a blocker,
  not authority to expose secrets, compete, or use hosted runners.
- Rollback to v0.4.1 restores broad legacy semantics unless both deletion
  switches and repair deletion are disabled first; this order is mandatory.

## Retirement

- Retire whole-track selected-stream extraction only from segmented jobs; keep
  the compatible short path.
- Retire monitor generic/crash deletion and repair crash deletion in v0.5.0.
- Keep legacy input names with warnings through the documented window.
- Keep marker schema v1, descriptor-relative unlink, `.subgen_skip`, and prior
  release artifacts.
- Keep v0.4.1 config/image backup until production observation completes.

## Execution Route

- Decision: `subagent-driven`.
- Evidence: resource/segmentation, failure policy, and release/deployment are
  bounded specialist slices that benefit from fresh implementation and
  two-stage specification/quality review while the root preserves integration.
- Fallback: root executes a slice inline if simulator/external state makes it
  serial or a subagent slot is unavailable.
- User confirmation required: `no`; the written design and implementation were
  explicitly approved on 2026-08-31.
