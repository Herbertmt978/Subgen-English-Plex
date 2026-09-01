# Memory-Aware Segmented Transcription and Safe Media Failure Handling

Status: `approved with Frigate/GPU amendment 2026-08-31`
Date: `2026-08-31`

## Intent

Subgen must process long local media without allowing input duration or
competition from other services to grow its memory use until the container or
host crashes. It must also distinguish an unreadable media import from a valid
file that merely cannot be transcribed, so an operator can opt into deleting
only conclusively invalid media and let Sonarr/Radarr replace that generation.

The public runtime therefore:

- selects the highest-quality multilingual Whisper model that fits a matching
  measured runtime envelope, or a conservative fallback tier when no matching
  envelope exists, when the operator has not selected a model;
- segments long media into bounded, sequential chunks;
- observes container and host pressure while a job is running;
- cooperatively abandons an uncommitted chunk, unloads the model, waits, and
  retries the same source interval with a smaller chunk when other workloads
  need memory;
- grows back toward the original chunk size after sustained recovery without
  changing model quality during the file;
- marks the exact failed generation on its first qualifying failure so the
  next scan skips it; and
- permits deletion only for a separately classified `invalid_media` failure,
  with deletion disabled by default.

Success evidence:

- finite cgroup limits and matching runtime envelopes select the documented
  deterministic model and initial chunk policy;
- explicit operator model choices always win and receive a capacity warning
  rather than being silently changed;
- 4 GiB and 6 GiB profiles complete long synthetic media without an OOM event
  or container restart using the automatically selected model;
- only one bounded audio chunk is resident for inference at a time;
- induced external memory pressure causes a cooperative yield, model release,
  smaller retry, and later chunk-size recovery without a media marker,
  subtitle fragment, or container restart;
- every decoder observation has one midpoint owner and its timestamps stay
  inside that owner's core; ambiguous matching seam text is retained rather
  than risking omission of legitimate repeated speech;
- the final SRT/LRC has monotonic source-timeline timestamps and normal
  numbering, and it appears only after every chunk succeeds;
- a Python inference error, resource-exhaustion failure, or native crash is
  attributed to and marker-skips the original generation but cannot select it
  for deletion;
- a valid video with no audio is retained and skipped;
- only a disposable sample that both independent validators conclusively
  reject as media can exercise the opt-in deletion path;
- a replacement generation at the same path has a new fingerprint and is
  processed normally;
- short files, upload APIs, naming, translation behavior, queue concurrency,
  and completion webhooks remain compatible; and
- all verification and release work runs locally or on the dedicated
  simulator, never on GitHub-hosted runners.

The implementation stop condition is a locally and simulator-verified `v0.5.0`
release candidate, an isolated post-audit Frigate candidate gate, a controlled
Frigate-host rollout, and verified Plex-host retirement. Public release
publication follows the candidate gate; production promotion remains a separate
explicit release action.

## Approved Product Behaviour

### Public defaults

```dotenv
MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json
MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
GPU_MEMORY_RESERVE_GIB=auto
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_MIN_FAILURES=1
```

Automatic model choice, segmentation, pressure yielding, and first-failure
generation marking are on by default. Deletion remains opt-in.

`SEGMENTATION_ENABLED=False` is the explicit compatibility opt-out for local
file segmentation. It does not disable model selection, media validation,
markers, or the deletion safety boundary. Pressure preflight, unload, and wait
remain available, but a yielded whole-file inference retries whole-file and
cannot reduce its source duration. The runtime logs that limitation.

`SEGMENTATION_CHUNK_MINUTES` accepts `auto` or an integer from `5` through
`60`. `MEMORY_PRESSURE_RESERVE_GIB` and `GPU_MEMORY_RESERVE_GIB` accept `auto`
or a positive numeric GiB value. An invalid value fails startup with a clear
configuration error instead of silently selecting unsafe behavior.

The overlap is an internal five-second guard on each available side. It is not
a first-release setting because changing it affects merge correctness rather
than ordinary resource tuning.

### Highest-safe-quality automatic model

`WHISPER_MODEL=auto` selects once per process before the first model load and
enumerates `large-v3`, `medium`, `small`, `base`, then `tiny`. It selects only
multilingual model names because this distribution translates non-English
audio to English. It does not select `.en` models or `turbo`; the latter is not
the accuracy-first translation model.

The primary decision input is an evidence-gated immutable `ModelEnvelope`. An
envelope is keyed to the packaged OCI config digest plus ordered layer diff
IDs; stable-ts, faster-whisper, and
CTranslate2 versions; model identifier and revision; compute type; CUDA device
class and driver/runtime evidence; decoding and translation options; inference
concurrency; and chunk policy. It records repeated peak cgroup/host memory and
device VRAM for cold load, first inference, long translation, unload/reload,
and fragmentation recovery, together with the tested reserves and safety
margin. One successful run or an upstream generic figure cannot create an
envelope.

#### External ModelEnvelope catalog and bootstrap

The catalog and runtime image identity are external owner-only artifacts, never
baked into or written by the ordinary runtime image. Their host paths are
`/var/lib/subgen/model-envelopes/v1/catalog.json` and
`/var/lib/subgen/model-envelopes/v1/image-identity.json` in a mode-0700
directory with mode-0600 regular files owned by the deployment owner. Symlinks
and group/other permission bits are rejected. Compose mounts both files
read-only at `/opt/subgen/model-envelopes/catalog.json` and
`/opt/subgen/model-envelopes/image-identity.json`; the exact runtime
configuration keys are:

```dotenv
MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json
MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json
```

The identity artifact uses canonical schema
`subgen.model-envelope.identity/v1` and has exactly this shape:

```json
{
  "schema": "subgen.model-envelope.identity/v1",
  "image_identity": {
    "config_digest": "sha256:<64 lowercase hex>",
    "layer_diff_ids": ["sha256:<64 lowercase hex>"]
  }
}
```

It rejects missing or unknown fields, duplicate keys, non-ASCII values,
non-canonical digests, and an empty or reordered layer list. A host-side
`docker image inspect` comparison must prove that the config digest and ordered
rootfs diff IDs still equal this artifact immediately before every isolated
profiler container start and every automatic runtime container start. The
artifact is an input only: neither runtime nor profiler may infer its own
identity from a tag or rewrite it.

The canonical JSON schema identifier is
`subgen.model-envelope.catalog/v1`. The v1 shape is:

```json
{
  "schema": "subgen.model-envelope.catalog/v1",
  "catalog_version": 1,
  "entries": [
    {
      "image_identity": {
        "config_digest": "sha256:<64 lowercase hex>",
        "layer_diff_ids": ["sha256:<64 lowercase hex>"]
      },
      "runtime": {
        "stable_ts_version": "<exact>",
        "faster_whisper_version": "<exact>",
        "ctranslate2_version": "<exact>",
        "cuda_runtime_version": "<exact>",
        "driver_version": "<exact>",
        "device_name": "<exact>",
        "compute_capability": "<exact>",
        "total_vram_bytes": 1
      },
      "policy": {
        "model": "large-v3",
        "model_revision": "<immutable revision>",
        "compute_type": "<exact>",
        "task": "translate",
        "inference_concurrency": 1,
        "chunk_minutes": 20,
        "decoder_options_sha256": "sha256:<64 lowercase hex>"
      },
      "measurements": {
        "runs": 3,
        "host_preload_used_bytes": 1,
        "host_peak_used_bytes": 1,
        "cgroup_preload_used_bytes": 1,
        "cgroup_peak_used_bytes": 1,
        "device_preload_used_bytes": 1,
        "device_peak_used_bytes": 1,
        "host_incremental_peak_bytes": 1,
        "cgroup_incremental_peak_bytes": 1,
        "device_incremental_peak_bytes": 1,
        "host_margin_bytes": 1,
        "device_margin_bytes": 1
      }
    }
  ],
  "integrity": {
    "algorithm": "sha256",
    "canonical_payload_sha256": "sha256:<64 lowercase hex>"
  }
}
```

Angle-bracket strings above describe validation syntax; emitted catalogs contain
only concrete ASCII values. All byte fields are positive base-10 integers and
JSON booleans are not accepted as integers. Canonical payload generation uses
Python stdlib `json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False).encode("utf-8")` over the object containing
only `schema`, `catalog_version`, and `entries`. SHA-256 covers those exact
bytes. Parsers use an `object_pairs_hook` to reject duplicate keys before normal
decoding and also reject NaN/infinity, non-ASCII strings, unknown or missing
fields, non-canonical digest syntax, non-positive byte/margin/run values, fewer
than three runs, duplicate matching entries, unordered or empty layer lists,
and any integrity mismatch. This narrow stdlib contract is the only canonical
form; no external canonical-JSON library is required.
`catalog_version` increases for each owner-approved replacement; schema changes
require a new schema/path version.

Image identity is the OCI image configuration digest plus the ordered rootfs
layer diff IDs. Tags, repository names, local image names, and a registry
manifest digest alone are not identity. Every runtime and policy field matches
exactly; there are no wildcards, compatible ranges, or nearest matches. The
catalog may contain version/digest strings and aggregate numeric measurements
only. It must not contain credentials, environment values, hostnames, device
UUIDs, user/media names, private paths, or raw diagnostics.

The loader validates both artifacts' regular-file/owner-only modes, both
schemas, catalog canonical integrity, the identity artifact against the
catalog entry, and strict current runtime/policy matching before using any
entry. A missing, unreadable, malformed, non-owner-only, integrity-invalid, or
non-matching artifact logs one bounded reason. Public automatic installs use
the conservative generic fallback. Canonical shared CUDA enters the no-safe-
model `recovering` state and admits no automatic model until a valid identity,
valid exact catalog entry, and fresh capacity are available. Explicit operator
models retain their authority; the isolated profiler exception below is still
guarded by generic admission and requires a valid identity artifact.

Bootstrap does not rebuild the image. Before transfer, the operator captures
the already-built candidate's configuration digest and ordered diff IDs in the
schema-v1 identity artifact. Immediately before each profiler start, the host
repeats `docker image inspect`, verifies both identity components, and mounts
the identity plus the current canonical catalog read-only. The profiler writes
only a distinct staged catalog on an owner-only output mount; the host validates
and atomically installs that file before the next start. An isolated profiler
may start explicit `large-v3` only if the resource-policy owner's conservative
generic incremental-peak-plus-margin host/cgroup/device admission passes. It
runs at least three cold-start/load/first-inference/long-translation/unload
cycles, records pre-load and peak bytes, derives the incremental peaks, and
writes the staged catalog with canonical integrity. If `large-v3` fails
admission or allocation safely, the profiler records no entry for it, fully
unloads, restarts the same image, and may profile `medium`, `small`, `base`,
then `tiny` in separate explicit processes. It never duplicates admission math
or downgrades a live file.

Immediately before the exact image is restarted with `WHISPER_MODEL=auto`, the
host repeats the same inspect comparison. The catalog and identity artifact are
then mounted read-only. Automatic selection must reproduce the highest
qualified entry, including `large-v3` when identity/catalog/runtime/policy match
and fresh effective host/device bytes cover its incremental peaks plus explicit
margins. This profile/write/restart sequence keys evidence to the candidate
without changing its layers or configuration digest.

On Frigate only, explicit envelope measurement runs in an isolated profiling
cgroup with a 12 GiB hard memory limit and 12 GiB memory-plus-swap limit, so no
extra swap is available. This cap does not relax any fresh host/cgroup/GPU
admission inequality, priority reserve, legacy-unit isolation, or immediate
Frigate abort threshold, and it is never a production limit or model
authorization. After a staged envelope is written, the profiler container is
destroyed and its model/cache release is verified. The exact image then starts
with `auto` under the final 10 GiB hard/no-swap cap and repeats every fresh
identity, runtime, host, cgroup, GPU, margin, and reserve check. If `large-v3`'s
measured incremental peaks plus margins do not fit that 10 GiB boundary, the
12 GiB profiling result cannot qualify it; `medium` and lower candidates are
profiled as needed and auto selects the highest entry that does fit.

For a matching envelope, a candidate qualifies only when its recorded
host/cgroup incremental peak plus its explicit host margin fits fresh effective
host/cgroup admission bytes and, on CUDA, its recorded device incremental peak
plus its explicit device margin fits fresh VRAM after subtracting the separate
GPU priority reserve. A matching envelope may authorize a model
above the generic fallback tier; therefore `medium` is a fallback outcome, not
a permanent ceiling on an evidence-verified faster-whisper deployment. If no
candidate has a matching envelope, the following conservative tables provide
the fallback ceiling.

| Effective memory capacity | CPU/fallback ceiling |
| --- | --- |
| below 2 GiB | `tiny`, with a constrained-capacity warning |
| 2 GiB to below 4 GiB | `base` |
| 4 GiB to below 8 GiB | `small` |
| 8 GiB to below 16 GiB | `medium` |
| 16 GiB or more | `large-v3` |
| unavailable or unbounded with no physical fallback | `small`, with a warning |

For CUDA fallback, Subgen derives both the system-memory ceiling above and an
allocatable-VRAM ceiling, then selects the lower-quality result:

| Allocatable VRAM after reserve | CUDA fallback ceiling |
| --- | --- |
| below 2 GiB | `tiny` |
| 2 GiB to below 3 GiB | `base` |
| 3 GiB to below 7 GiB | `small` |
| 7 GiB to below 12 GiB | `medium` |
| 12 GiB or more | `large-v3` |
| free or total VRAM unavailable | no promotion beyond `small`, with a warning |

Fallback ceilings never imply a zero-cost model. When no matching envelope is
available, admission uses these conservative nonzero incremental load budgets:

| Model | Host/cgroup load budget | Device load budget |
| --- | ---: | ---: |
| `tiny` | 0.75 GiB | 1 GiB |
| `base` | 1 GiB | 2 GiB |
| `small` | 2 GiB | 3 GiB |
| `medium` | 5 GiB | 7 GiB |
| `large-v3` | 9 GiB | 12 GiB |

Fallback admission adds a 512 MiB host margin and a 1 GiB device margin. Exact
envelopes supply their own strictly positive measured margins. These margins
are model/runtime uncertainty margins; they are separate from host/cgroup
reserves and the shared-GPU priority reserve.

The host values are conservative incremental faster-whisper load budgets, not
total process memory. They preserve the acceptance boundaries after measured
baseline use and cgroup floor: a 4 GiB cgroup with 1 GiB current use and a
512 MiB floor has 2.5 GiB available, exactly fitting `small`'s 2 GiB increment
plus 512 MiB margin; a 9 GiB cgroup with 2 GiB current use and a 0.9 GiB floor
has 6.1 GiB available, fitting `medium`'s 5 GiB increment plus 512 MiB margin.
Fresh host `MemAvailable` after its reserve must independently meet the same
required bytes. If measured baseline or either fresh term is worse, the next
candidate is tried; if `tiny` cannot fit its nonzero increment plus margin,
Subgen waits in `recovering(reason=no_safe_model)`.

The default GPU reserve is the greater of 1 GiB and 10% of total VRAM. An
explicit positive `GPU_MEMORY_RESERVE_GIB` may raise this reserve but cannot
lower the mandatory automatic floor. General-purpose automatic selection uses
the minimum free-memory value from three fresh exact-device samples five
seconds apart, then rechecks the same device immediately inside the model-load
admission boundary. It never sums devices or uses a different CUDA index/UUID.
Ambiguous, malformed, timed-out, stale, or missing telemetry cannot promote the
fallback beyond `small`; the canonical shared-CUDA deployment fails closed as
defined below.

These fallback tables are conservative hypotheses based on upstream
approximate requirements. The official [OpenAI Whisper model table](https://github.com/openai/whisper/blob/main/README.md)
and [faster-whisper benchmarks](https://github.com/SYSTRAN/faster-whisper/blob/master/README.md)
show that backend and compute type materially change memory use, but neither
proves this packaged runtime. Exact repeated envelope evidence may safely
promote or demote a candidate before release; it must never be inferred from a
transient free-memory snapshot.

Any non-empty, non-`auto` `WHISPER_MODEL` remains authoritative. Subgen logs
the explicit selection and warns when it is above the automatic ceiling, but
does not override it. The selected automatic or explicit model is fixed for
the process and for every retry of a file. Live pressure changes chunk size,
not transcription quality.

Model-load or model-cache failures that occur before file inference are runtime
configuration failures. They do not create a media marker and can never delete
a media file.

### Stable capacity and initial chunk tier

In Docker, capacity discovery prefers a finite cgroup v2 `memory.max`, then a
finite cgroup v1 limit. A native deployment falls back to physical memory. An
unavailable or unbounded capacity with no physical fallback uses the
conservative ten-minute tier and logs the reason once.

| Effective memory capacity | Initial core chunk duration |
| --- | ---: |
| below 4 GiB | 5 minutes |
| 4 GiB to below 8 GiB | 10 minutes |
| 8 GiB to below 16 GiB | 20 minutes |
| 16 GiB or more | 30 minutes |
| unavailable or unbounded with no physical fallback | 10 minutes |

The initial tier is deliberately deterministic. Compared with the earlier
capacity-only draft, the 8–16 GiB tier is held at 20 minutes because it also
runs the `medium` model. The automatic maximum is 30 minutes; an informed
operator can explicitly select up to 60 minutes.

At job admission, the current pressure state may reduce the working chunk
below this baseline. A local file no longer than the current working chunk may
use the existing whole-file path. If that path cooperatively yields or reports
a recognized memory-exhaustion exception, its retry enters the segmented path
at a smaller chunk size.

Segmentation bounds duration-driven allocations. It cannot make a model whose
base weights and runtime exceed the selected capacity fit.

### Cooperative last-priority memory behaviour

`MEMORY_PRESSURE_YIELD=True` makes Subgen a cooperative last-priority workload.
It does not attempt to resize CTranslate2 model weights in place and does not
change Docker's hard limit. It controls when inference starts, how much source
audio a call sees, and whether the loaded model remains resident while the host
is under pressure.

On Linux, a pressure sample includes:

- cgroup `memory.current` and finite `memory.max`;
- cgroup `memory.pressure` when available;
- host `MemAvailable` and `MemTotal` from `/proc/meminfo`; and
- host `/proc/pressure/memory` when available; and
- for CUDA, total and free GPU memory from a bounded NVIDIA runtime query.

Missing optional PSI files degrade to the available headroom signals. Native
non-Linux deployments use their platform physical-total and available-memory
adapter and log that PSI is unavailable.

The automatic host reserve is:

```text
min(max(1 GiB, 15% of host physical memory), 25% of effective capacity)
```

When effective capacity is unknown, the 25% cap is omitted. When physical
memory is also unknown, the host reserve is 1 GiB.

The cgroup headroom floor is the greater of 512 MiB and 10% of the finite
cgroup limit. An explicit `MEMORY_PRESSURE_RESERVE_GIB` replaces only the host
reserve; it cannot disable the cgroup floor.

The resident GPU headroom floor is the greater of
`GPU_MEMORY_RESERVE_GIB`, 1 GiB, and 10% of total VRAM. Public deployments may
use the automatic floor. A canonical shared-CUDA deployment must instead set an
explicit positive reserve derived after audit from the maximum verified
incremental demand of higher-priority workloads plus a reaction margin; it
cannot start with `auto` or an absent audit result.

For an exact envelope:

```text
host_incremental_peak_bytes = max_i(max(0, host_peak_i - host_preload_i))
cgroup_incremental_peak_bytes = max_i(max(0, cgroup_peak_i - cgroup_preload_i))
device_incremental_peak_bytes = max_i(max(0, device_peak_i - device_preload_i))
host_load_bytes = max(host_incremental_peak_bytes,
                      cgroup_incremental_peak_bytes)
device_load_bytes = device_incremental_peak_bytes
required_host_bytes = host_load_bytes + host_margin_bytes
required_device_bytes = device_load_bytes + device_margin_bytes
```

Each incremental value is the maximum paired per-run delta; subtracting a
separately aggregated preload maximum from a separately aggregated peak maximum
is invalid. For fallback, the table's host/cgroup value populates both the host
and cgroup incremental fields, after which `max(...)` selects it once rather
than summing it. The device column supplies `device_load_bytes`, then the
fallback margins are added. Immediately before every automatic load or reload,
Subgen reads fresh host, cgroup, and exact-GPU headroom and computes:

```text
host_admission_bytes = max(0, MemAvailable - host_reserve)
cgroup_admission_bytes = max(0, cgroup_limit - cgroup_current - cgroup_floor)
effective_host_admission_bytes = min(host_admission_bytes,
                                     cgroup_admission_bytes)
device_admission_bytes = max(0, free_vram - gpu_priority_reserve)
```

An unbounded cgroup omits only the cgroup term; an unavailable required term is
not healthy. A candidate qualifies only when
`effective_host_admission_bytes >= host_load_bytes + host_margin_bytes` and,
for CUDA,
`device_admission_bytes >= device_load_bytes + device_margin_bytes`, where the
load terms are incremental peaks. The separate priority reserve has already
been subtracted exactly once in `device_admission_bytes`. The checks occur
inside the model-load gate after the stabilization window. Candidates are tried
only by enumeration before loading; if even `tiny` lacks its nonzero
incremental load plus explicit margin, state becomes
`recovering(reason=no_safe_model)` and waits without attempting a load,
consuming a failure, or marking media. A later retry keeps the already selected
model for an admitted file; it never downgrades mid-file.

For canonical shared CUDA, a missing sample never counts as healthy and a
sample older than two five-second intervals is stale. One missing/stale sample
closes new CUDA admission. Two consecutive missing/stale samples cause the
resident model to unload at the next safe boundary, and recovery requires three
fresh admission-qualified samples. This fail-closed rule does not convert a
resource wait into a file failure.

The controller samples at most once every five seconds and has three states:

1. `normal` — use the current working chunk.
2. `yielding` — do not admit new inference; unwind an in-progress uncommitted
   chunk when the callback can do so safely.
3. `recovering` — keep the model unloaded until three consecutive healthy
   samples have been observed; CUDA recovery samples must also satisfy the
   selected model's load/reload admission floor.

Pressure is sustained when two consecutive samples show any of:

- host available memory below the reserve;
- cgroup headroom below its floor;
- CUDA free VRAM below the GPU headroom floor;
- PSI `full avg10` of at least 1%; or
- PSI `some avg10` of at least 10%.

Cgroup or GPU headroom below half its floor, or a new cgroup OOM event, is
critical and does not wait for the second sample. Thresholds are internal
constants in `v0.5.0` so public compatibility is not created before simulator
evidence exists.

The stable-ts progress callback is composed with the existing progress display.
When pressure becomes sustained, it raises a private
`MemoryPressureYield` control exception. The segmented coordinator catches
only that exception, discards the incomplete chunk, releases its audio/result
objects, exits the inference gate, unloads the model through the canonical
model runtime, clears allocator/accelerator caches, and waits.

The model runtime also owns a five-second resident-idle observer whenever a
CUDA model is loaded and no inference callback is active. It applies the same
exact-device pressure and telemetry-loss rules, closes admission, and unloads
the cached model without waiting for another media job. This prevents idle
Subgen weights from blocking a higher-priority Frigate or Ollama allocation.

This is not an exception raised from a native callback: the packaged
faster-whisper adapter invokes `progress_callback` from its Python generator
loop. A packaging guard and real inference smoke must prove that the installed
version still propagates the private exception and releases the inference gate;
failure of that guard blocks the release and reopens the dependency approach.

After recovery, the same source core is retried from its beginning with half
the previous working duration, clamped to a five-minute minimum. No partial
words are appended before a chunk succeeds. After three consecutive successful
chunks with healthy samples, the duration doubles toward, but never above, the
initial baseline. Thus pressure can lower Subgen's demand and later restore
throughput without changing the selected model.

Pressure waiting is cancellation-aware and uses bounded exponential polling
backoff from five to sixty seconds. Sustained pressure can pause a task
indefinitely, but it cannot spin or consume a retry attempt. Only an actual
allocation attempt counts toward the two minimum-size failures below.

A recognized Python `MemoryError`, CUDA OOM, or CTranslate2 allocation failure
uses the same release-and-shrink path. If the five-minute minimum fails twice
without an external-pressure recovery transition, it surfaces as
`resource_exhaustion`, is marker-eligible, and is never deletion-eligible.

A model-load allocation failure first follows the same pressure release/wait
path but has no file identity. Insufficient, missing, or stale admission
telemetry never consumes a load-failure attempt. Two allocation failures after
fresh samples satisfied the complete selected-model admission floor declare the
matching envelope or fallback profile unhealthy and require operator attention;
they never mark or delete queued media and do not silently downgrade an
explicit or automatically selected model.

A native crash cannot be caught by Python. The existing active-task event lets
the monitor attribute `SIGSEGV` to the source generation after restart; it is
marked and retained.

The callback cannot run before a backend reaches its first progress update.
Conservative initial chunks, the cgroup hard limit, and the existing positive
OOM preference remain emergency protection for that window. The design reduces
crashes but cannot promise graceful yielding after an instantaneous host OOM.

### Media classification and selective deletion

Deletion decisions do not consume generic transcription errors. Before model
load, local media receives two independent, bounded validations:

1. an FFprobe subprocess requests format and stream JSON; and
2. an isolated PyAV probe opens the container, inspects its streams, and when
   audio exists attempts at most the first frame from one reported audio
   stream.

Both validators have timeouts. A timeout, permission error, disappearing path,
network/storage I/O error, validator crash, or ambiguous result is
`probe_indeterminate` and is retained. It is not evidence of corrupt media.

The classifier has these outcomes:

| Outcome | Evidence and action | Marker | Deletion eligible |
| --- | --- | ---: | ---: |
| `valid_audio` | At least one validator establishes a usable audio stream; continue processing. | No | No |
| `no_audio` | At least one validator recognizes a valid container and the available evidence shows no usable audio; skip and retain. | No | No |
| `probe_indeterminate` | Neither validator establishes audio or a valid no-audio container, and dual invalidity is not proven; mark, report, and retain without inference. | Yes | No |
| `invalid_media` | Both validators independently and conclusively reject the input as a parseable media container. | Yes, before delete | Yes, opt-in only |
| `inference_error` | Valid/indeterminate media reaches inference but fails. | Yes | No |
| `resource_exhaustion` | Minimum bounded retry still cannot allocate. | Yes | No |
| `sigsegv` | Native process crash attributed to active source generation. | Yes | No |
| `resource_pressure_yield` | Cooperative control transition; retry later. | No | No |

A validator reports one of `audio_present`, `no_audio`,
`invalid_format`, or `indeterminate`. Aggregation is ordered: two
`invalid_format` results are required for `invalid_media`; otherwise any
`audio_present` result permits processing; otherwise a recognized container
with no reported audio becomes `no_audio`; every remaining combination is
`probe_indeterminate`. A terminal indeterminate result is marked and retained
so a startup scan does not repeatedly attempt the same generation.

A valid silent video is therefore `no_audio` even though it has no audio track.
It is retained and cheaply skipped. Failure to determine duration alone is not
`invalid_media` when either validator recognizes the container.

`invalid_media` requires two conclusive parser-invalid results. A file whose
header parses but whose later payload is damaged is conservatively marked and
retained in this release. Full-file validation is intentionally not performed
because it would duplicate a complete decode and create the same resource
pressure this feature is meant to avoid.

The Subgen structured event carries an explicit `failure_class`. The monitor
may select deletion only from that field. Generic `worker_error`,
`file_error`, log text, `SIGSEGV`, OOM evidence, or a filename can never be
promoted to `invalid_media`.

The shared marker registry remains schema-compatible: `invalid_media`,
`inference_error`, and `resource_exhaustion` persist as the existing
`processing_error` marker kind, while native crashes use `sigsegv`. The richer
failure class belongs to structured events and monitor state, not the
cross-version skip contract.

The monitor always fingerprints and writes the marker before attempting a
delete. Its existing exact-path, exact-generation, containment, descriptor,
quarantine-intent, and recovery checks remain mandatory. If deletion is
disabled or any safety check blocks it, the marker remains and the next scan
skips that exact generation. A Sonarr/Radarr replacement has a different
fingerprint and is processed normally.

## Scope and Compatibility Boundary

In scope:

- local file jobs created by startup scan, Watchdog, Plex/Jellyfin hooks, and
  the existing file queue;
- `transcribe` and `translate` tasks;
- single- and multiple-audio-track media;
- SRT output and the existing LRC path for audio files;
- CPU and GPU Compose profiles;
- automatic model choice when the operator leaves `WHISPER_MODEL` blank or
  selects `auto`;
- pressure-aware pause, release, retry, shrink, and recovery;
- first-failure marker attribution for the original source file;
- invalid-media-only deletion as an optional monitor policy;
- a new minor release and controlled Frigate-host deployment using its RTX
  3090 while Plex remains the media application/source.

Compatibility boundaries:

- `/asr`, OpenAI-compatible upload endpoints, response shapes, and uploaded
  byte-buffer processing are unchanged in this release;
- subtitle naming and language-selection rules are unchanged;
- `CONCURRENT_TRANSCRIPTIONS=1` remains the packaged default and every chunk
  uses the existing shared model-inference gate;
- short jobs retain the current whole-file inference path unless pressure
  forces a segmented retry while segmentation is enabled;
- completion webhook and Plex/Jellyfin refresh happen once, after the final
  output succeeds;
- explicit model names remain authoritative;
- matching failure markers are not cleared on upgrade, so a previously marked
  generation remains skipped until replacement or deliberate operator removal;
- marker JSON schema and fingerprint identity remain backward compatible.

## Approaches Considered

### 1. Application-level bounded chunks plus cooperative yielding — selected

Use existing FFmpeg selected-track extraction for bounded in-memory WAV chunks,
invoke stable-ts/faster-whisper sequentially, merge structured timestamps, and
use the supported progress callback to abort only an uncommitted chunk when
pressure persists.

Advantages:

- controls full-input decode and feature-extraction growth;
- can actually release audio, result, model, and allocator memory;
- preserves the highest-safe-quality selected model across retries;
- requires no maintained dependency fork;
- reuses track selection, the inference semaphore, output formatting, and
  exact-generation markers;
- permits deterministic unit tests and observable pressure state.

Trade-offs:

- each boundary loses some cross-chunk language context;
- FFmpeg processes a small overlap twice;
- the first backend allocation can occur before a progress callback;
- structured ownership and adaptive retries need careful tests.

### 2. OS priority and a fixed memory ceiling only — rejected

CPU nice level, `oom_score_adj`, and a Docker limit protect other services but
cannot make an active inference allocation shrink. Under pressure they choose
which process loses rather than letting Subgen release and resume. They remain
last-resort host protection, not the memory-management algorithm.

### 3. One fixed chunk duration for every host — rejected

A universal five- or ten-minute duration is simpler, but unnecessarily slows
larger systems, does not choose model quality from capacity, and cannot yield
memory already held by a model when Plex or another service becomes active.
Manual duration remains an override, not the public default.

### 4. Fork stable-ts/faster-whisper for native streaming — deferred

A dependency fork could yield results deeper in the backend, but creates an
upstream maintenance boundary and does not remove Subgen-specific output,
marker, queue, classification, and track-selection coordination. Reconsider
only if bounded extraction cannot meet measured quality or memory acceptance.

## Architecture and Ownership

### Canonical owners

- New `subgen_core/resource_management.py` owns capacity discovery, automatic
  model selection from already-validated envelopes/fallbacks, admission math,
  memory/PSI sampling, the pressure state machine, retry-duration decisions,
  and the private control exception.
- New `subgen_core/model_envelope_catalog.py` owns catalog and identity
  schema-v1 validation, mode checks, stdlib-canonical catalog serialization,
  SHA-256 integrity, strict identity/runtime/policy matching, and atomic
  owner-only artifact writing. The ordinary runtime uses only its
  read/validate/match API.
- New owner-operated `profile_model_envelopes.py` owns isolated repeated
  measurements, consumes admission decisions from
  `subgen_core/resource_management.py`, and invokes the catalog writer. It
  contains no duplicate admission math and never runs in the scanner, queue
  worker, or ordinary container entry point.
- New `subgen_core/segmentation.py` owns duration-based chunk planning,
  bounded selected-stream extraction orchestration, timestamp ownership, and
  structured result assembly.
- `subgen_core/media.py` remains the media/track owner and gains the bounded
  dual-validator classifier. It does not delete.
- `subgen_core/transcription.py` remains the owner of the complete local-file
  job, output naming/writing, completion webhook, and task result. It delegates
  resource policy, classification, and segmented inference.
- `subgen_core/model_runtime.py` remains the sole model-loading, unloading,
  cache-release, and shared inference-gate owner. It gains a pressure-release
  operation with explicit lock ordering.
- `monitor_subgen_failures.py` remains the only automatic deletion decision
  owner. It validates structured failure classes, persists failure evidence,
  writes the marker, and then delegates exact-generation deletion to the
  existing operations-safety boundary.
- `repair_subgen_failures.py` remains an evidence/reporting tool in `v0.5.0`.
  Its legacy `delete` input is accepted only to emit a migration warning and
  retain candidates; it cannot delete crash candidates or resume legacy
  delete intents that lack a conclusive `invalid_media` classification.
- `subgen_failure_markers.py` remains the generation-skip authority and does
  not gain a parallel failure taxonomy.
- `subgen_override.py` remains configuration/wiring and compatibility facade
  only; it must not gain resource, segmentation, classifier, or deletion
  algorithms.

The new core modules may not import `subgen_override` or `subgen`. Pure policy,
catalog, and merge helpers accept explicit values. Runtime-dependent operations
receive the existing runtime facade through narrow calls. The profiler imports
core owners but no core owner imports the profiler.

### Admission and transcription flow

1. The scanner checks the generation marker before media probing as it does
   today.
2. The media owner runs bounded FFprobe and isolated PyAV validation and emits
   a typed result.
3. `no_audio` returns a normal skip. Conclusive `invalid_media` emits one typed
   terminal event without loading Whisper. `probe_indeterminate` emits one
   retained processing failure without inference. Only `valid_audio`
   continues. Wrapper layers preserve the original class and must not emit a
   second generic terminal event for the same attempt.
4. The queue and optional language-detection task select the exact audio stream
   as they do today.
5. Resource policy resolves an explicit model or the automatic highest-safe
   model and logs the capacity source and decision once.
6. Duration probing reuses validated FFprobe data where possible. Failure to
   determine duration is a retained processing error, not invalid media.
7. Capacity selects the initial core duration and a preflight pressure sample
   selects the current working duration.
8. If duration is no greater than the current working duration, the existing
   whole-file inference path may run. Otherwise the segmented coordinator
   starts at source time zero.
9. Each iteration chooses its core duration from current controller state, so
   later chunks can shrink or grow. The core is half-open and expands five
   seconds on each available side for inference context.
10. FFmpeg extracts only that interval from the selected stream as mono 16 kHz
    PCM WAV bytes. The whole-track multi-audio extraction must not run first on
    the segmented branch.
11. The chunk passes through `transcribe_with_model` with the same language,
    task, regroup, keyword arguments, and inference gate as the original job.
    Its callback combines ordinary progress with rate-limited pressure checks.
12. Only a successful chunk is offset to source time, trimmed to its ownership
    interval, normalized, and appended. The coordinator then advances the
    monotonic source cursor.
13. Audio and untrimmed results are released before the next extraction. A
    yield does not advance the cursor and retries the same source core.
14. After every chunk succeeds, the aggregate becomes one stable-ts
    `WhisperResult`, attribution is appended once, and normal output side
    effects run once.

### Pressure lock and retry contract

The implementation must document and test one lock order. A pressure release
occurs only after the inference callback unwinds and the shared inference
semaphore is no longer executing backend code. The model runtime then acquires
the inference gate before the model-load lock, unloads the current model,
releases CUDA/allocator caches, and returns with both locks released. Model
reload uses the existing load owner before the retry re-enters inference.

No pressure handler may delete a file, write a marker, mutate the aggregate
transcript, clear a completed subtitle, or bypass task cancellation. A queued
task can wait indefinitely under real host pressure while emitting bounded
heartbeat logs; operator shutdown remains responsive.

## Boundary Ownership and Merge Contract

Chunk overlap provides inference context. Each decoder observation has one
output owner, but independently decoded matching text can be semantically
ambiguous.

- Each core is half-open `[core_start, core_end)`, except the final core also
  owns content ending exactly at media duration.
- Every word is assigned by timestamp midpoint to exactly one core.
- A segment with words is rebuilt from only the words owned by that core;
  empty segment fragments are discarded.
- A wordless segment is assigned by segment midpoint using the same rule.
- Each retained word or wordless segment is clipped to its owning core before
  merge, so independent overlap decodes can meet at a seam but cannot cross it.
- Matching seam text is not automatically removed. Two independent decodes
  cannot prove whether it is one jittered observation or legitimate repeated
  speech, so the safer completeness policy retains both observations.
- Chunk timestamps are shifted by extraction start before ownership, so all
  output uses the original source timeline.
- Rebuilt segments are monotonically ordered and receive fresh sequential IDs.
- Aggregate language is the first non-empty chunk language; every chunk uses
  the already selected/forced language and same task.
- Adaptive chunk size changes affect future core boundaries only. A failed or
  yielded core contributes nothing and is replanned from its original start.

The implementation merges structured segments and words, not rendered SRT
strings. It creates no per-chunk subtitle files. Core clipping guarantees
non-overlapping timestamp intervals at seams; stable-ts owns final numbering,
word highlighting, and formatting once at the end.

## Output Atomicity, Markers, and Failure Routing

The segmented path renders SRT/LRC only after every chunk succeeds. It writes a
private temporary file beside the intended output, flushes and syncs it, then
atomically replaces the destination. Failure removes the temporary file and
leaves no partial final subtitle. The short legacy path is not broadened into
an unrelated output refactor.

All lifecycle events retain the original media path and exact task identity.
Add bounded operator events for:

- capacity source, chosen model, baseline chunk, and manual overrides;
- media classification and validator agreement without raw parser dumps;
- chunk start/finish with source range and working duration;
- pressure transition, release, wait heartbeat, retry shrink, and recovery;
- merge completion and final output;
- duration, extraction, inference, merge, and output failures.

Logs must not contain subtitle text, credentials, validator binary payloads, or
unnecessary host-only media paths. Progress identifies the original filename,
chunk index, and overall source-time progress.

Failure routing is:

- pressure yield: retry only; no failure count or marker;
- recognized allocation failure: bounded shrink/retry, then
  `resource_exhaustion` marker if the minimum fails twice;
- ordinary Python extraction/inference/merge/output error: first-failure
  `processing_error` marker and retain;
- native crash: first-failure `sigsegv` marker after restart and retain;
- conclusive `invalid_media`: first-failure `processing_error` marker, then
  optional invalid-media deletion;
- `no_audio`: normal skip and retain.

If an error happens after a replacement has changed the path generation, the
existing fingerprint check rejects stale marker and delete actions.

## Configuration, Compatibility, and Packaging

The new settings and changed defaults are parsed once by the facade and
documented in:

- `.env.example`;
- all three Compose profiles;
- `README.md` hardware, failure, and migration guidance;
- `docs/CONFIGURATION.md`;
- release notes and changelog.

Configuration semantics:

| Setting | `v0.5.0` default | Contract |
| --- | --- | --- |
| `WHISPER_MODEL` | `auto` | Explicit model wins; blank is treated as `auto`. |
| `SEGMENTATION_ENABLED` | `True` | Local file segmentation/adaptive retry. |
| `SEGMENTATION_CHUNK_MINUTES` | `auto` | Initial capacity tier or explicit 5–60 minutes. |
| `MEMORY_PRESSURE_YIELD` | `True` | Cooperative pressure pause/retry. |
| `MEMORY_PRESSURE_RESERVE_GIB` | `auto` | Host reserve; cgroup floor remains mandatory. |
| `GPU_MEMORY_RESERVE_GIB` | `auto` | Public fallback; canonical shared CUDA requires the positive audit-derived priority reserve. |
| `MODEL_ENVELOPE_CATALOG` | `/opt/subgen/model-envelopes/catalog.json` | Read-only external catalog; missing/invalid uses public fallback and fails closed on canonical shared CUDA. |
| `MODEL_ENVELOPE_IDENTITY` | `/opt/subgen/model-envelopes/image-identity.json` | Read-only schema-v1 runtime OCI identity; it must match the catalog and current runtime/policy. |
| `AUTO_MARK_MIN_FAILURES` | `1` | First qualifying terminal failure marks generation. |
| `AUTO_DELETE_INVALID_MEDIA` | `false` | Opt into deletion of conclusive invalid media only. |
| `AUTO_DELETE_MIN_FAILURES` | `1` | First conclusive invalid-media failure when enabled. |
| `AUTO_DELETE_FAILED_FILES` | `false` | Deprecated compatibility alias for invalid-media-only deletion. |

`AUTO_DELETE_INVALID_MEDIA` is the only new deletion authority. The legacy
`AUTO_DELETE_FAILED_FILES=true` setting is deliberately narrowed in `v0.5.0`:
it enables invalid-media-only deletion and logs one migration warning; it no
longer deletes generic processing errors or `SIGSEGV`. Either boolean may
enable invalid-media deletion during migration, so operators must set both
false to disable it. The legacy alias remains through `0.5.x` and is removed no
earlier than `1.0.0`.

The legacy repair action is narrowed at the same boundary. A configured
`SUBGEN_REPAIR_ACTION=delete` is treated as report-only with a migration
warning in `v0.5.0`; old crash-candidate or untyped pending delete intents are
preserved as policy-blocked evidence and never resumed. Invalid-media deletion
is initiated only by the monitor from a fresh, typed dual-validator event.

This safety change is called out prominently in release notes. It preserves the
old variable as an accepted input but does not preserve destructive behavior
that conflicts with the confirmed failure-class boundary.

Packaged images include both new canonical modules through the existing
`subgen_core` copy and unconditionally copy the owner-operated profiler to
`/subgen/profile_model_envelopes.py`. Packaging tests assert that exact path and
compile it in the built image. Module-boundary tests require source Compose to
mount the complete package and prevent algorithms from drifting into the
facade.

The packaged Compose memory default remains 10 GiB. Explicit hardware examples
may pin a model. The main default profile uses `auto`, documents generic tables
as fallbacks, and records which matching `ModelEnvelope` authorized any
selection above them.

## Verification and Release Acceptance

### Focused automated coverage

Create focused files instead of enlarging existing 800-line reliability and
boundary suites:

- `tests/test_resource_management.py`;
- `tests/test_model_envelope_catalog.py`;
- `tests/test_model_envelope_profiler.py`;
- `tests/test_segmentation.py`;
- `tests/test_media_validation.py`;
- narrow additions to monitor, marker, packaging, and module-boundary tests.

Coverage must prove:

1. cgroup v2/v1 finite limits, unbounded values, physical fallback, and unknown
   fallback;
2. catalog v1 stdlib-canonical writing/loading and identity v1 loading;
   owner-only/read-only policy; duplicate-key/NaN/unknown-field rejection;
   SHA-256 integrity; exact OCI identity-to-catalog plus current runtime/policy
   matching; missing/mismatch public fallback; and canonical shared-CUDA
   fail-closed behavior;
3. every initial chunk boundary and explicit configuration validation;
4. every generic CPU/allocatable-VRAM fallback boundary and nonzero per-model
   incremental load budget including `tiny`; paired-run incremental-peak
   derivation; exact-envelope and fallback margins; host, cgroup, and device
   incremental-peak-plus-margin formulas; exact 4 GiB/1 GiB-current `small` and
   9 GiB/2 GiB-current `medium` feasibility; deterministic `large-v3`-down
   enumeration; no-safe-model recovery; and immediate fresh load/reload checks;
5. the profiler consumes the catalog/identity and resource-policy owners,
   performs no independent admission arithmetic, profiles `large-v3` downward
   in clean processes, writes only staged catalogs, and never rebuilds;
6. host/cgroup/PSI/GPU pressure sampling rate limit, sustained/critical entry,
   recovery hysteresis, stale/missing telemetry fail-closed behavior,
   resident-idle unload, wait heartbeat, and cancellation;
7. callback control-exception propagation, gate release, model/cache release,
   same-core retry, halve-to-minimum, and grow-to-baseline;
8. resource yield never appends results, writes output, increments failure
   counts, marks, or deletes;
9. recognized OOM retry and terminal minimum-failure marker/retain behavior;
10. short, exact-boundary, adaptive multi-chunk, final-partial, and overlap
   plans;
11. word and wordless midpoint ownership, timestamp offset, core-seam clipping,
    monotonic ordering, sequential IDs, and preservation of semantically
    ambiguous matching seam text;
12. selected-track FFmpeg mapping and no whole-track extraction on segmented
    jobs;
13. sequential model calls with at most one chunk resident;
14. preserved transcribe/translate arguments and model semaphore use;
15. one atomic final SRT/LRC, webhook, task result, and metadata refresh;
16. no final/temp subtitle after extraction, inference, merge, write, or yield
    failure;
17. FFprobe/PyAV classifier truth table, including valid audio, valid silent
    video, dual conclusive invalid, disagreement, timeout, permission, path
    replacement, and transient I/O;
18. only explicit `invalid_media` can select either deletion boolean;
19. marker is durable before delete, blocked delete remains skipped, and a
    replacement fingerprint processes;
20. generic worker/file errors, OOM, and SIGSEGV cannot be misclassified as
    invalid media;
21. legacy `AUTO_DELETE_FAILED_FILES=true` is narrowed to invalid-media-only
    deletion with a warning, while both false disables deletion;
22. unchanged marker JSON schema, short-file behavior, upload APIs, and
    source/packaged configuration parity;
23. profiler bootstrap consumes the canonical identity/catalog and resource
    admission owners without duplicate math, records paired incremental peaks,
    writes a staged external catalog without rebuilding, can retry lower
    candidates in clean processes, and proves that the Frigate-only 12 GiB
    profiling cap cannot authorize a model unless a fresh exact-image auto
    restart also satisfies its envelope under the final 10 GiB hard/no-swap
    cap;
24. candidate OCI config digest and ordered diff IDs survive save/load and
    remote pull; and
25. legacy monitor, repair timer, and repair service state capture,
    stop/disable verification, candidate isolation, and deletion-off v0.3.0
    restoration policy.

### Repository checks

Run locally with GitHub plugin autoload disabled where applicable:

```bash
python -m pytest -q
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
```

No push occurs while a required local check is failing or unrun.

### Simulator evidence

Before use, confirm the dedicated simulator has no active user, task marker,
test, Docker build, or container workload. Wake it only if needed and shut it
down afterward only if this task woke it and a final activity check is clear.

On the simulator:

- build the packaged CPU image locally;
- boot source and packaged profiles with real library scanning and external
  notifications disabled and require HTTP 200 from `/status`;
- generate disposable media outside the repository;
- run long-file smokes at 4 GiB and 6 GiB and verify the automatic model;
- run a 9 GiB/`medium` CPU smoke for the public capacity tier;
- when NVIDIA is available, prove total/free/reserve detection, automatic
  allocatable-VRAM model selection, sustained/critical VRAM yield, and recovery;
- measure cgroup `memory.peak`, `memory.events`, restart count, output
  timestamps, boundary ownership, and absence of chunk artifacts;
- introduce bounded external pressure with a separate capped disposable
  workload, prove Subgen yields/unloads/shrinks without restart, remove that
  pressure, and prove recovery/growth and final completion;
- verify a valid silent synthetic video is retained;
- verify dual-parser invalid, single-parser success, timeout, permission, and
  indeterminate samples;
- enable `AUTO_DELETE_INVALID_MEDIA=true` only against a disposable test media
  root and prove marker-before-delete plus replacement-generation processing;
- keep names, audio, transcripts, and diagnostics private and off GitHub.

Pressure tests start below expected thresholds and increase within explicit
cgroup caps. They must not compete with another simulator task.

No GitHub-hosted runner, GitHub Actions test job, or speculative push is part
of the test loop.

### Release and Frigate rollout

This is a public default-on capability and failure-policy migration, so it is
released as `0.5.0` rather than a patch. Release work includes `VERSION`,
changelog, release notes, Compose defaults, locally built image, immutable
digest evidence, and manual publication after the verified commit is on
`main`.

The Plex-hosted Subgen container is retired before release. Its monitor is
disabled, its container is removed, and its Compose files, model cache,
generation markers, state, and prior image remain as recovery evidence. Plex
itself and library media remain untouched.

Frigate target configuration:

```dotenv
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
# GPU_MEMORY_RESERVE_GIB is the positive value recorded by the released audit;
# `auto` is prohibited for this canonical shared-CUDA deployment.
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_INVALID_MEDIA=true
AUTO_DELETE_MIN_FAILURES=1
```

The Frigate VM has a 24 GiB RAM maximum with a 20 GiB balloon floor. The current
post-boot read-only snapshot showed about 7.5 GiB `MemAvailable`, no Ollama model
loaded, and about 18.1 GiB free on its 24 GiB RTX 3090. Earlier readings of about
11 GiB free with `qwen3:8b` pinned and about 17.4 GiB after it was unloaded are
historical context only. None of these momentary readings is a tier guarantee.

The passive audit did not observe or bound incremental Frigate/Ollama demand,
so it cannot supply the explicit priority reserve required above. Deployment is
therefore blocked. A future gate may set the envelope's
`device_margin_bytes = 2,048 MiB` as a reaction margin in addition to the
measured `device_incremental_peak_bytes`; thus
`required_device_bytes = device_incremental_peak_bytes + 2,048 MiB`. The
separate higher-priority reserve is then subtracted once from live free VRAM to
produce `device_admission_bytes`; it is not part of `device_margin_bytes` and
is not added again to the required bytes. The future evidence must either bound
incremental higher-priority demand or set a conservative explicit reserve and
demonstrate it under at least 15 minutes of representative camera, detector,
embedding, and Ollama traffic. Frigate and Ollama remain higher-priority
workloads; v0.5.0 does not stop, reconfigure, or coordinate either service.

Frigate rollout must:

1. remain blocked until future evidence supplies a positive priority reserve
   and the representative-traffic gate above can run;
2. capture the exact enabled/active states of the legacy Subgen monitor, repair
   timer, and repair service; back up the v0.3.0 Compose/config, state, model
   cache, OCI identity, generation registry, and units; then stop, disable, and
   verify inactive the legacy monitor, repair timer/service, and old Subgen
   container without touching Frigate or Ollama;
3. before public release, run explicit envelope bootstrap with the exact
   candidate, separate model cache/state/media/output roots, and a profiling-
   only 12 GiB hard/no-swap cgroup; retain existing low CPU priority and keep
   startup scan, monitor, notifications, and both deletion switches disabled.
   This cap is never used for the automatic or production runtime;
4. install the owner-only identity artifact beside the catalog, verify its
   config digest and ordered diff IDs with host-side `docker image inspect`
   immediately before each profiler start, and mount the identity and current
   catalog read-only; profile explicit `large-v3` only after the resource-policy
   owner's incremental-peak-plus-margin admission, write a staged catalog
   without rebuilding, then validate and atomically install it; profile lower
   candidates in clean processes only if larger ones fail safely. Throughout
   profiling, retain the same fresh host/cgroup/GPU admission, future explicit
   priority reserve, legacy-unit isolation, and immediate abort thresholds;
5. after writing an envelope, destroy the profiler and verify model/cache
   release; repeat the host-side inspect comparison immediately before the
   exact image starts with `auto` under the final 10 GiB hard/no-swap cap and
   both artifacts mounted read-only. Require three fresh samples plus immediate
   host, cgroup, and exact-device checks, the future explicit priority reserve,
   a strictly matching identity/catalog/runtime/policy entry, and measured
   incremental-peak-plus-margin admission under that 10 GiB limit. The 12 GiB
   profiling cap supplies evidence only; if `large-v3` does not fit, profile
   `medium` and lower as needed and select the highest qualified entry, or enter
   `no_safe_model` recovery if none fit;
6. verify cold load, first inference, one long disposable translation,
   unload/reload, idle-resident unloading, `/status`, cgroup peaks/events,
   catalog integrity/strict identity/runtime/policy matching, identity
   continuity, and at least 15
   minutes of representative traffic. Abort immediately on any NVIDIA Xid,
   cgroup/CUDA OOM, or container restart increase; abort when camera process FPS
   stays below 90% of configured FPS for more than 30 seconds, skipped FPS
   exceeds 0.5, a detector stalls/errors, or an embedding error appears. Do not
   add synthetic GPU pressure on this production camera host;
7. prove in isolated state whether v0.3.0 reads a disposable v0.5 schema-v1
   marker. If it does not, retain the registry as evidence but do not claim
   rollback skip compatibility;
8. publish only the exact OCI identity that passed this gate, verify the same
   config digest and ordered diff IDs after remote pull, then promote it with
   the classified-failure monitor while deletion stays off and the repair timer
   and repair service remain inactive;
9. exercise deletion only with a disposable invalid sample under an isolated
   mapped test directory, confirm valid-silent and induced inference failures
   remain, and only then enable canonical invalid-media deletion; and
10. retain the complete v0.3.0 rollback set until long production transcription,
   passive GPU/host observation, scan progress, and subtitle timestamps pass.

Public rollback guidance restores v0.4.1 with all deletion paths off.
Frigate operational rollback instead restores the preserved v0.3.0
config/cache/OCI identity with both deletion switches false and repair in report
mode, then restores the captured legacy monitor/repair timer/service states only
as required by that v0.3.0 rollback. It never recreates Plex-hosted Subgen or
changes Frigate/Ollama. Existing markers remain evidence, and they are claimed
as active rollback skips only if the isolated v0.3.0 compatibility check passed.

## Non-goals

- Streaming or segmenting uploaded `/asr` and OpenAI-compatible API bodies.
- Parallel chunk inference.
- Switching to a lower-quality model in the middle of a file.
- Treating transient host pressure as a media failure.
- Catching a native `SIGSEGV` inside the same Python process.
- Deleting a file because transcription, translation, extraction, duration,
  memory, model loading, or native inference failed.
- Deleting valid silent/no-audio media.
- Full-file validation or deliberate deletion of real Plex media in tests.
- Automatically clearing failure markers or retrying previously marked media
  without a new fingerprint.
- Changing queue concurrency, subtitle naming, language policy, Sonarr/Radarr
  behavior, Frigate camera/embedding settings, or Ollama lifecycle.
- Maintaining a stable-ts/faster-whisper fork unless measured evidence
  invalidates bounded extraction/callback control.

## Design and Governance Checks

### TaskIntentDraft

- Outcome: public, default-on highest-safe-quality model selection, bounded
  segmented transcription, cooperative last-priority memory yielding, and
  selective invalid-media deletion.
- Goal: finish long jobs on constrained/shared hosts while never interpreting
  a resource or inference failure as permission to delete media.
- Success evidence: deterministic unit/integration coverage, constrained real
  inference and pressure smokes, dual-validator safety tests, full local
  checks, image boot, isolated Frigate candidate evidence, Frigate production
  observation, and continued Plex-host retirement.
- Stop condition: verified `v0.5.0` release candidate, passed pre-publication
  Frigate candidate gate, controlled rollout, and no hosted-runner use.
- Principal risks: boundary quality, callback timing, lock ordering, pressure
  flapping, false model-fit claims, destructive misclassification, selected
  track drift, partial output, and rollback compatibility.

### BaselineReadSetHint

- `README.md` and `docs/CONFIGURATION.md` for hardware/default/failure policy;
- `CONTRIBUTING.md` for owners, local checks, and simulator policy;
- `docs/aegis/baseline/2026-08-30-initial-baseline.md` for runtime and marker
  boundaries;
- the generation-bound marker design and ADR 0001 for fingerprint semantics;
- `subgen_core/transcription.py`, `model_runtime.py`, `media.py`, scanner and
  queue paths, facade configuration, monitor, operations safety, Compose
  profiles, and nearby tests;
- retired Plex deployment evidence, the Frigate host/GPU sharing snapshots,
  the audit-owned priority envelope, and installed stable-ts callback behavior;
- upstream Whisper and faster-whisper model/runtime guidance.

### BaselineUsageDraft

- Required baseline refs: repository authority, canonical owners, marker
  design, and exact-generation deletion safety.
- Delivered context refs: prior Plex 8/9 GiB pressure; Frigate's 24 GiB VM RAM
  maximum, 20 GiB balloon floor, and about 7.5 GiB post-boot `MemAvailable`;
  no loaded Ollama model and about 18.1 GiB free RTX 3090 VRAM. Historical
  `qwen3:8b` readings are context only. Passive evidence did not bound
  incremental priority demand. The first-failure skip and dual-validator
  deletion boundaries remain unchanged.
- Acknowledged before plan refs: local/simulator-only testing, highest safe
  quality when automatic, and immutable release packaging.
- Cited in design refs: existing inference gate, model cleanup, selected-track
  extraction, structured events, marker-before-delete, and public defaults.
- Missing refs: matching immutable `ModelEnvelope` evidence and a future
  representative-traffic proof that bounds higher-priority incremental demand
  or demonstrates a conservative explicit reserve. Deployment remains blocked;
  upstream figures and passive snapshots do not substitute for those gates.
- Decision: `continue` with fallback-only generic tiers, exact-runtime envelope
  gates, safety-first classification, and evidence-bound release claims.

### Requirement Ready Check

- Requirement source refs: user-approved segmentation, automatic highest-safe
  model, dynamic yielding/recovery, first-failure skip, and deletion only when
  both validators conclusively reject media.
- Goals and scope refs: Intent, Approved Product Behaviour, and Scope.
- User/scenario refs: 4/6 GiB public users and the shared-GPU Frigate
  deployment with the Plex-hosted instance retired.
- Acceptance refs: Verification and Release Acceptance.
- Open blocker questions: none; the user approved implementation and later
  amended the deployment target to Frigate's shared RTX 3090.
- Decision: `ready` for amended implementation planning.

### ImpactStatementDraft

- Affected layers: configuration, media probing, local-file transcription,
  model lifecycle, progress callback, chunk/result assembly, structured
  failure events, monitor policy, Compose, docs, tests, release, Plex
  retirement, and Frigate deployment.
- Canonical invariants: one queue, one inference gate, one selected stream, one
  final output, original-path identity, marker before delete, exact-generation
  destructive checks, and deletion off by default.
- Compatibility: explicit models, short files, upload APIs, marker JSON, output
  naming, and replacement fingerprints retain existing contracts.
- Non-goals: dependency fork, parallelism, mid-file model downgrade, blanket
  deletion, real-media destructive tests, and infrastructure coordination.

### Existence Check

Proposed new surfaces:

- `subgen_core/resource_management.py`;
- `subgen_core/segmentation.py`;
- focused resource, segmentation, and media-validation tests.

Existing reuse:

- transcription remains job/output coordinator;
- model runtime remains load/unload/gate authority;
- media remains probe/track authority;
- monitor and operations safety remain deletion authorities.

Why add modules:

- `transcription.py` is already substantial and should not combine pressure
  sampling/state, model policy, chunk ownership, output, and webhooks;
- resource policy is used by both startup model selection and live inference,
  while segmentation has a separate timestamp/planning responsibility;
- both modules have pure independently testable logic and no facade dependency.

Entropy/retirement impact:

- no second queue, output writer, marker registry, or deletion path is added;
- the short whole-file path remains only as a bounded fast path and explicit
  compatibility path;
- the narrowed legacy deletion alias has a documented `1.0.0` retirement floor.

Decision: `add-with-proof` for both cohesive owners.

### Complexity Budget

- Artifact class: source, test, destructive-policy, and decision complexity.
- Pressure: the transcription owner is 671 lines, the facade 1,312 lines, and
  nearby reliability/module-boundary tests exceed the 800-line soft signal.
- Planned distribution: two cohesive new modules, a small transcription
  coordinator branch, bounded media-owner additions, model-runtime primitives,
  monitor policy changes, and focused new test files.
- Budget result: `within-budget` if algorithms stay out of the facade and
  existing oversized tests receive only narrow integration assertions.
- Falsifier: duplicated pressure/model logic or a second deletion path makes
  the design `at-risk` and requires redesign before implementation.

### Architecture Integrity Lens

- Invariant: source duration and competing host workloads cannot make one
  admitted inference input unbounded.
- Safety invariant: only two conclusive parser-invalid results can produce an
  `invalid_media` deletion candidate.
- Canonical contract: resource policy chooses/yields, segmentation plans and
  assembles, media classifies, transcription coordinates, model runtime
  loads/releases, monitor selects, operations safety deletes.
- Higher-level simplification: reuse FFmpeg, stable-ts structured results,
  progress callbacks, inference gate, and marker registry rather than fork a
  backend or create chunk subtitle files.
- Falsifiers: chunk inference still reaches cgroup OOM before callback,
  structured merge loses boundary text, pressure cannot safely release the
  model, or any indeterminate/valid sample reaches delete selection.
- Verdict: aligned, subject to simulator evidence.

### Baseline Role Alignment and ADR Signal

- Product/Requirement Baseline: automatic quality, segmentation, cooperative
  yielding, first-failure skip, and optional invalid-only deletion are approved
  requirements; exact tiers remain evidence-bound.
- Architecture/Runtime Boundary Baseline: canonical queue, inference,
  transcription, output, marker, media, monitor, and deletion owners remain
  intact.
- Result: `aligned`, scope `both`.
- ADR signal: model/chunk tiers, pressure state/retry contract, and classified
  invalid-media deletion are durable architecture/public-contract decisions.
  Implementation closeout should record an ADR and baseline sync only after
  verified code and release behavior exist; this proposed spec does not create
  accepted architecture memory by itself.
