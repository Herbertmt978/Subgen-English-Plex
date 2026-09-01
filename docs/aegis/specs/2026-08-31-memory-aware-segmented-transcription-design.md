# Memory-Aware Segmented Transcription and Safe Media Failure Handling

Status: `approved with Frigate/GPU priority amendment 2026-09-01`
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
- an owner-only higher-priority signal on shared CUDA closes admission,
  unloads or yields at the safe callback boundary, retries the same source
  interval, and fails closed when the signal is stale or unavailable;
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
PRIORITY_PRESSURE_FILE=
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_MIN_FAILURES=1
```

Automatic model choice, segmentation, memory-pressure yielding, and
first-failure generation marking are on by default. The optional
`PRIORITY_PRESSURE_FILE` integration is disabled when empty. Setting it makes
that signal mandatory and fail closed; there is deliberately no second boolean
that could accidentally turn a configured shared-GPU signal into a fail-open
advisory. A non-empty `PRIORITY_PRESSURE_FILE` is valid only when
`MEMORY_PRESSURE_YIELD=True`; the opposite combination is rejected during
startup before scanner, queue worker, profiler, or model initialization. An
unset priority path remains valid with either memory-yield setting. Deletion
remains opt-in.

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

### Cooperative last-priority resource behaviour

`MEMORY_PRESSURE_YIELD=True` enables Subgen's cooperative last-priority yield
machinery. Memory signals are sufficient for ordinary memory contention;
shared-accelerator compute priority additionally requires the host-owned signal
defined below. The runtime does not attempt to resize CTranslate2 model weights
in place and does not change Docker's hard limit. It controls when inference
starts, how much source audio a call sees, and whether the loaded model remains
resident while the host is under pressure. `MEMORY_PRESSURE_YIELD=False` may
disable that machinery only while `PRIORITY_PRESSURE_FILE` is empty. A
configured priority path therefore always has an active cooperative consumer;
configuration cannot silently accept the signal while ignoring it.

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

Generic host/cgroup/PSI/GPU resource probes refresh at most once every five
seconds. When a priority file is configured, a separate lightweight reader
polls and sequence-validates it at least once per second and wakes the same sole
controller immediately on a new priority observation; the controller composes
that observation with the most recent still-fresh generic resource snapshot.
This is a separate input cadence, not a second policy controller. The controller
has three states:

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

The model runtime also owns a resident-idle observer whenever a CUDA model is
loaded and no inference callback is active. It refreshes generic resource terms
on the five-second cadence and, when configured, polls the priority file at
least once per second between those refreshes. It applies the same exact-device
pressure and telemetry-loss rules, closes admission, and unloads the cached
model without waiting for another media job. This prevents idle Subgen weights
from blocking a higher-priority Frigate or Ollama allocation.

#### Host-owned priority pressure for shared accelerators

Free VRAM does not prove that GPU compute is available. A candidate-absent
Frigate control on the shared RTX 3090 showed sustained camera process/skip
pressure while roughly 17.5 GiB of VRAM remained free. Global GPU utilization
is not a valid control input either, because Subgen's own inference raises that
same counter and would make it self-trigger or oscillate. The higher-priority
workload must therefore own the compute-pressure decision.

When `PRIORITY_PRESSURE_FILE` is a non-empty absolute path, Subgen treats it as
a required owner-only host signal. The public default remains empty. Ashby's
canonical shared-CUDA Frigate deployment must configure it; a fixed schedule or
free-VRAM reserve alone is not sufficient for that host. The parent directory
is mounted read-only into Subgen rather than binding the file itself so atomic
replacement is visible. The producer and consuming runtime use a dedicated
matching UID; the directory is mode 0700 and the file is a regular mode-0600
file. Subgen opens it with `O_NOFOLLOW`, accepts at most 4 KiB, and rejects a
symlink, non-regular file, unsafe mode/owner, duplicate/extra/missing key,
invalid type, oversized input, wrong host boot, future observation, or an
observation older than ten seconds.

The logical JSON key contract is exactly:

```json
{
  "schema": 1,
  "boot_id_sha256": "64-lowercase-hex",
  "producer_epoch": "32-lowercase-hex",
  "sequence": 123,
  "observed_monotonic_ns": 123456789,
  "source_generation": 1788264471,
  "source_observed_monotonic_ns": 123450000,
  "observation_id": "64-lowercase-hex",
  "policy_sha256": "64-lowercase-hex",
  "pressure": true,
  "clear_eligible": false,
  "reason_codes": ["higher_priority_busy"]
}
```

On disk it is canonical ASCII JSON with keys sorted recursively, compact
separators, `ensure_ascii=True`, `allow_nan=False`, and exactly one trailing
newline. Any other byte representation, including insignificant whitespace, is
invalid. This makes an equal sequence with different bytes unambiguously
fail-closed. `schema`, `sequence`, `observed_monotonic_ns`,
`source_generation`, and `source_observed_monotonic_ns` are JSON integers, not
booleans. Schema is exactly 1; the other four are in `1..2^63-1`;
`source_observed_monotonic_ns <= observed_monotonic_ns <=` the consumer's
current monotonic clock. `pressure` and `clear_eligible` are JSON booleans.

`boot_id_sha256` is lowercase hex SHA-256 of the canonical lowercase UUID text
read from `/proc/sys/kernel/random/boot_id`, with its one trailing newline
removed and no other whitespace; the preimage is its ASCII bytes. Producer and
consumer reject a noncanonical UUID before hashing. `producer_epoch` is random
for each producer process. `sequence` starts at one in each epoch and increases
by exactly one for every atomic publication. Within one epoch, new
`source_generation` values are strictly increasing Frigate
`service.last_updated` integers; the current value may repeat
across heartbeat publications. `source_observed_monotonic_ns` records when that exact source
generation was first received and must not be refreshed by a duplicate source
snapshot. An equal source generation requires an exactly equal
`source_observed_monotonic_ns`; a greater source generation requires a greater
first-seen timestamp. `observation_id` is 32 random bytes encoded as 64
lowercase hex characters and is unique for every publication. Status exposes
`SHA256(observation_id.encode("ascii")).hexdigest()`; it never hashes decoded
hex bytes.
Heartbeat age is limited to ten seconds and source age to thirty seconds.
Within one producer epoch, a lower sequence, source-generation regression, or
the same sequence with different bytes is unavailable/fail-closed. Re-reading
the exact bytes at the last accepted sequence is a no-op. Exactly
`last_seen_sequence+1` is the only normal next publication. A completely
validated higher sequence with a gap is itself mapped unavailable/critical
because an asserted publication may have been overwritten; the consumer
advances its seen sequence/source checkpoint to that file but does not use it as
clear or asserted evidence. The following exact +1 publication may begin the
ordinary three-distinct-clear recovery. After any invalid episode, re-reading an
older accepted value cannot clear unavailable state. The consumer samples the
file at least once per second in both the idle observer and active progress path,
but the gap rule remains mandatory rather than relying on scheduling. Before the
producer has a valid source generation it publishes no file, so the configured
consumer remains unavailable/fail-closed without inventing a sentinel value.
After the first valid source, a probe or endpoint failure publishes asserted
`higher_priority_unavailable` while preserving the last valid
`source_generation` and its original `source_observed_monotonic_ns`; sequence,
heartbeat time, epoch, and observation ID still advance. Once that preserved
source exceeds thirty seconds, the consumer reports `unavailable` rather than
treating the asserted heartbeat as fresh source telemetry.
The first valid publication carries the new producer epoch. Every observed
producer-epoch change is critical even from normal: it closes admission,
transitions to `yielding` when a model is resident or `recovering` otherwise,
and resets recovery. That first publication cannot count clear; three subsequent
distinct clear generations are required. Duplicate clear source generations
never advance recovery.

`policy_sha256` binds the producer's exact private expectation policy. The host
service requires `FRIGATE_PRIORITY_POLICY_FILE` and `FRIGATE_CONFIG_FILE` as
absolute paths and `FRIGATE_PRIORITY_POLICY_SHA256` as exactly 64 lowercase hex.
It opens both with `O_NOFOLLOW`; the policy parent is mode 0700
and the regular policy file is matching-owner mode 0600 and at most 32 KiB.
The exact logical policy shape is:

```json
{
  "schema": 1,
  "frigate_version": "0.17.2",
  "detection_fps_limit": 80.0,
  "source_max_age_seconds": 30,
  "cameras": {"private_camera_id": 8.0},
  "detectors": ["private_detector_id"],
  "required_embedding_speeds": ["private_embedding_id"],
  "conditional_embedding_pairs": [["private_embedding_a", "private_embedding_b"]],
  "frigate_config_sha256": "64-lowercase-hex",
  "gpu_uuid": "GPU-canonical-lowercase-uuid",
  "nvidia_driver_version": "bounded-version",
  "gpu_index": 0
}
```

There are exactly those twelve top-level keys. `schema`, `gpu_index`, and
`source_max_age_seconds` are JSON integers, not booleans. Schema and source age
equal 1 and 30 respectively; GPU index is in `0..31`.
`detection_fps_limit` is a JSON float, not an integer or boolean, and equals
80.0, so its canonical representation is literally `80.0`. `frigate_version`
is the exact expected live version and matches `[0-9A-Za-z._+-]{1,32}`. Every
private identifier is ASCII `[A-Za-z0-9_.-]{1,128}`. `cameras` has 1..128 unique
keys and JSON-float, non-boolean expected process-FPS values greater than zero
and at most 60.0. `detectors` has 1..32 identifiers and
`required_embedding_speeds` has 1..64; each array is ordinally sorted and
unique. `conditional_embedding_pairs` has 0..32 entries; every entry is an
array of exactly two distinct, ordinally sorted identifiers, and the outer
array is lexicographically sorted and unique. `frigate_config_sha256` is
lowercase 64-hex of the exact regular Frigate config-file bytes and must match
before any healthy decision.
`gpu_index` is in `0..31`, `gpu_uuid` is canonical `GPU-` plus a lowercase UUID,
and `nvidia_driver_version` is printable ASCII of length 1..32. All three must
match the one private-policy-bound device queried by the producer.

Policy bytes are canonical ASCII JSON (`sort_keys=True`, compact separators,
`ensure_ascii=True`, `allow_nan=False`) plus one newline; extra/missing keys,
duplicate JSON keys, noncanonical bytes, unsafe paths/modes/owners, config hash
drift, exact camera/detector set drift, or Frigate version drift asserts
`policy_drift`. The exact file hash must also equal the configured expected
SHA; the producer never silently adopts replacement policy bytes.
`policy_sha256` is SHA-256 of those exact canonical policy
bytes, is present in every signal publication, and is exposed only as that hash
in runtime/gate status. Required embedding keys must exist with positive finite
speeds. For each conditional pair, both keys absent is valid idle, both present
requires positive finite speed/activity, and exactly one present is invalid.
Before any valid source/policy pair, a policy failure leaves the signal absent.
After one has existed, an unreadable, unsafe, noncanonical, or hash-mismatched
policy publishes asserted `policy_drift` using the configured expected policy
hash and the last valid source generation/timestamp; it never publishes the
untrusted replacement hash as authority.

`reason_codes` is a sorted unique list containing one to four values from the
fixed privacy-safe set `higher_priority_busy`, `higher_priority_degraded`,
`higher_priority_unavailable`, and `policy_drift` while pressure is asserted;
it must be empty otherwise. `pressure=true` requires
`clear_eligible=false`; `pressure=false,clear_eligible=true` is one clear
candidate; and `pressure=false,clear_eligible=false` is neutral/pending. Every
other combination is invalid. The runtime never logs those values,

Reason mapping is deterministic. An asserted high-detection streak or any
loaded Ollama model adds `higher_priority_busy`. An asserted skipped-FPS or low-
ratio condition, or present-but-unhealthy detector, embedding, or NVIDIA health
metric, adds `higher_priority_degraded`. A missing, malformed, out-of-range,
inconsistent-duplicate, timed-out, or failed required Frigate/Ollama/NVIDIA
probe adds `higher_priority_unavailable`. Policy bytes/config hash/version or
expected camera/detector/embedding topology drift adds `policy_drift`. When
multiple conditions coexist, `reason_codes` is the ordinally sorted unique
union; no trigger may be relabelled into another category. Neutral and clear
publications always carry an empty list.

The runtime never logs those values,
  the configured path, a camera name, an observation ID, or a raw producer
  payload. The `resource_management.priority_pressure` extension to public
  `/status` contains exactly these keys:

  ```json
  {
    "configured": true,
    "state": "asserted",
    "heartbeat_age_ms": 1234,
    "source_age_ms": 4321,
    "policy_sha256": "64-lowercase-hex",
    "observation_digest": "64-lowercase-hex",
    "transition_observation_digest": "64-lowercase-hex",
    "transition_sequence": 12,
    "controller_phase": "yielding",
    "recovery_reason": "priority_pressure",
    "distinct_clear_count": 0,
    "model_resident": false,
    "model_load_generation": 4,
    "model_unload_generation": 3
  }
  ```

  `configured` and `model_resident` are JSON booleans. `state` is exactly one of
  `disabled|clear|neutral|asserted|unavailable`; a fresh valid signal maps its two
  booleans directly to asserted, clear, or neutral, and invalid or stale input
  maps to unavailable. `controller_phase` is exactly
  `normal|yielding|recovering`. `recovery_reason` is null exactly in `normal` and
  otherwise is exactly one of
  `priority_pressure|resource_pressure|model_admission`. Ages are JSON integer
  milliseconds, not booleans, calculated from the atomic snapshot's monotonic
  time as the floor of the nonnegative difference and capped at 60000 for public
  output. They are null only when there has never been an accepted observation or
  the integration is disabled. If the current file becomes stale, unreadable, or
  invalid after an accepted observation, state becomes unavailable while ages,
  policy hash, and observation digest continue to describe the last accepted
  observation; raw uncapped ages still enforce the ten- and thirty-second limits.
  `policy_sha256`, `observation_digest`, and `transition_observation_digest` are
  otherwise lowercase 64-hex strings. `observation_digest` tracks the latest
  accepted publication. `transition_observation_digest` is latched to the
  accepted publication that most recently incremented `transition_sequence` and
  does not change on same-state heartbeats; it is null when that transition was
  caused by missing, unreadable, malformed, or age-expired input rather than an
  accepted publication. This lets the observer bind assertion N after a later
  heartbeat has arrived.

  `transition_sequence`, `model_load_generation`, and
  `model_unload_generation` are non-boolean JSON integers in `0..2^63-1` and are
  never reset during process lifetime. `transition_sequence` starts at zero and
  increments exactly once under the controller lock when the mapped priority
  state changes or an accepted producer epoch changes, even if its mapped state
  is unchanged. Repeated heartbeats or source generations in the same state do
  not increment it. `distinct_clear_count` is a non-boolean integer in `0..3`:
  it resets to zero on an asserted, neutral, unavailable, invalid, or new-epoch
  event; advances once per strictly increasing clear source generation during
  priority recovery; and saturates at three through the return to normal so the
  causal recovery remains observable. The next reset-inducing priority event
  returns it to zero. All fields, including controller phase/reason, admission
  state in the enclosing resource status, residency, and generations, are read
  beneath the documented controller/model lock order as one atomic snapshot.

The same atomic public snapshot contains a privacy-safe sibling
`resource_management.workload` object with exactly `active`,
`chunk_uncommitted`, and `completion_generation`. The first two are JSON
booleans. When `active=false`, `chunk_uncommitted=false`.
`chunk_uncommitted=true`
only while the backend is processing the current chunk before its structured
result has been accepted into the merge coordinator. It becomes false on yield
unwind, between chunks, and before final publication.
`completion_generation` is a non-boolean process-lifetime integer in
`0..2^63-1`, starts at zero, and increments exactly once only after the full
merged subtitle has been durably published; failed, partial, yielded, or marker-
only work does not change it. This public object never exposes a cursor,
duration, media path, title, fingerprint, or workload identity and is read beneath the
same coordinator/controller/model lock order as the priority snapshot.

Exact cursor proof exists only for the owner-operated Task 11B candidate. The
ordinary/public defaults leave `TASK11B_GATE_RECEIPT_FILE`,
`TASK11B_GATE_TOKEN_SHA256`, `TASK11B_PHASE_A_WORKLOAD_SHA256`, and
`TASK11B_PHASE_B_WORKLOAD_SHA256` empty, which disables the surface completely.
Any nonempty proper subset is a startup error. The two workload hashes must be
distinct lowercase-64-hex values, and the gate-only runtime requires
`CONCURRENT_TRANSCRIPTIONS=1`. It admits exactly Phase A first and Phase B only
after Phase A's durable completion; a missing, out-of-order, concurrent, or
foreign workload hash fails closed before model admission. The gate-only file path must
be absolute beneath a verified owner-matching mode-0700 nonsymlink parent, must
not exist at process start, and the token digest must be lowercase 64-hex and
equal the frozen execution-boundary ownership-label token digest. The runtime
opens the path once with `O_CREAT|O_EXCL|O_APPEND|O_NOFOLLOW`, mode 0600, keeps
that verified inode open, and writes an append-only receipt journal. Each
publication is one maximum-4-KiB canonical ASCII JSON record with sorted keys,
compact separators, no NaN, and exactly one trailing newline. Under the same
coordinator/controller/model lock order, the single writer performs one full
checked `os.write`, fsyncs the journal, and only then exposes the corresponding
state transition to further work. It never truncates, replaces, or rewrites a
record. The owner supervisor tails complete newline-delimited records from the
same revalidated inode, so correctness does not depend on poll frequency; a
partial record, inode replacement, size regression, file over 8 MiB, sequence
gap, duplicate, or mutation invalidates the gate. Each record has schema
`subgen.task11b.runtime-receipt/v1` and exactly `runtime_epoch`,
`gate_token_sha256`, `sequence`, `observed_monotonic_ns`, `workload_sha256`,
`source_generation`, `observation_digest`, `transition_observation_digest`,
`transition_sequence`, `heartbeat_age_ms`, `source_age_ms`, `policy_sha256`,
`priority_state`, `controller_phase`, `recovery_reason`, `admission_open`,
`distinct_clear_count`, `model_resident`, `model_load_generation`,
`model_unload_generation`, `active`, `chunk_uncommitted`, `active_cursor_ms`,
`completed_cursor_ms`, `completion_generation`, `model_identity_sha256`,
`cuda_oom_generation`, and `media_failure_generation` in addition to `schema`.

The receipt epoch/token hashes use the exact already-bound values. The workload
hash is null only before any gate workload has been bound; from admission
through that workload's completion, yield, cancellation, or failure record it
is the lowercase-64-hex digest of the exact private workload identity. A later
workload changes it before admission and causes its own durable receipt. Hashes
are lowercase 64-hex, the epoch is lowercase 32-hex, and sequence/time/
generation are positive or nonnegative non-boolean integers in `0..2^63-1`.
`source_generation`, the three observation/policy digests, and the two ages are
nullable only under the last-accepted rules below; non-null source generation
is positive, non-null digests are lowercase 64-hex, and non-null ages are
non-boolean integers in `0..60000`. Priority/controller/recovery/admission,
clear-count, model-residency, and model-generation values use the exact public
status types and enums.
Sequence starts at one and increments exactly once on each receipt publication.
The gate-only runtime publishes and fsyncs a new record before further work after
the initial gate setup, every accepted priority observation or priority state
transition, and every workload, cursor, model residency/generation, completion,
or failure-generation change. A controller transition caused by unavailable or
invalid input is also published before further work. Its priority fields map
exactly like the public atomic status: after any accepted publication it retains
the last accepted source generation, observation digest, policy hash, and
bounded ages, while `transition_observation_digest` is null when the transition
was not caused by an accepted publication; those last-accepted fields are null
only when no publication has ever been accepted. The priority, model,
failure, and workload fields are captured under the same documented lock order
as one atomic snapshot; the journal therefore retains fast assertion, unwind,
unload, reload, failure, and completion transitions even when no HTTP poll could
observe them.
While active, `active_cursor_ms` is a nonnegative non-boolean integer; while
inactive it is null and `chunk_uncommitted=false`. For each workload,
`completed_cursor_ms` is null until that workload completes and then equals its
terminal cursor when completion generation increments exactly once; admission
of a later workload resets this per-workload field to null without resetting the
process-lifetime generation. Yield/unwind publishes the same active cursor with
`chunk_uncommitted=false`; partial output, marker creation, failure, or restart
can never advance completion. Receipt contents are never
returned by an HTTP route, log, notification, or committed evidence record.

`model_identity_sha256` is null exactly while no model is resident. For a
resident model it is SHA-256 of canonical ASCII JSON plus one newline containing
exactly `catalog_entry_sha256`, `model_policy_sha256`, `model_revision`, and
`selected_model`. The first two values are the lowercase-64-hex hashes of,
respectively, the exact matched catalog entry and that entry's exact `policy`
object, each independently serialized with the same canonical JSON rules and
one newline. The revision and model are the immutable revision and actual
selected model used to construct the fully usable backend. The digest is set
atomically only after a successful single-flight load, becomes null only after
a successful unload, and is recomputed from the same immutable inputs on reload.
Task 11B recomputes it from the frozen catalog, candidate identity, and matching
unloaded-envelope model policy; events before and after recovery cannot qualify
with a different backend, revision, catalog entry, or policy.

A second privacy-safe sibling, `resource_management.runtime_identity`, contains
exactly `epoch` and `started_monotonic_ns`. `epoch` is 16 random bytes encoded as
32 lowercase hexadecimal characters, generated once before any scanner, worker,
profiler, or model activity and never changed during that process.
`started_monotonic_ns` is the positive non-boolean integer host monotonic time
captured with it and also never changes. Neither field is persisted or restored;
a process restart necessarily creates a different epoch. Both are included in
the same atomic status snapshot and let release evidence distinguish an evidence
cursor reset from a forbidden runtime restart.

`resource_management.failure_counters` contains exactly
`cuda_oom_generation` and `media_failure_generation`, both non-boolean process-
lifetime integers in `0..2^63-1` that start at zero, are generated in-process,
are never accepted from configuration, and are never reset.
`cuda_oom_generation` increments exactly once before propagation whenever the
canonical backend exception classifier identifies a caught CUDA out-of-memory
condition. `media_failure_generation` increments exactly once before marker,
deletion, retry-exhaustion, or terminal-failure handling accepts an actual
media-processing failure; a cooperative pressure yield/cancellation does not
increment it. The release gate also scans bounded incremental candidate logs
for the exact case-insensitive alternatives `CUDA out of memory` and
`CUDA error:\s*out of memory`; the independent log path catches native/backend
messages that do not reach the Python classifier. These coarse counters expose
no media identity or exception text.

`model_load_generation` starts at zero and increments exactly once, beneath the
model-load/runtime condition lock, only after a single-flight owner changes the
runtime from no resident model to a fully usable selected backend and before it
notifies joiners. `model_unload_generation` starts at zero and increments
exactly once, under the same lock order, only after a single-flight release owner
has changed a previously resident model to nonresident, the backend confirms
unload, and accelerator/allocator cache release succeeds. A failed load,
failed/partial release, idempotent no-resident unload, stale request, or joined
waiter increments neither counter. Joined callers observe the owner's one
transition. The existing internal `model_release_generation` remains an
admission/single-flight epoch and is not exposed or accepted as unload proof.
`model_resident` and both causal counters are read in the same atomic status
snapshot.

When the path is unset, the priority object is always
`configured=false,state=disabled`; heartbeat/source ages, policy hash, and
  observation and transition-observation digests are null, and transition
  sequence/distinct-clear count are zero. Disabled never means an observed clear. When a path is configured but no
first valid publication exists, state is `unavailable`; the same ages/hashes/
  digest are null and distinct-clear count is zero. Model residency/load/unload
  fields still report their real runtime values.

A fresh asserted signal is critical pressure: it closes admission immediately,
causes the resident-idle observer to unload, and causes an in-progress
uncommitted chunk to unwind at the next stable-ts callback. Missing, malformed,
unsafe, wrong-boot, stale, regressed, or unreadable telemetry is also critical
whenever a path is configured. Recovery requires three consecutive, strictly
increasing, complete, clear source generations plus the existing host, cgroup,
GPU-memory, identity, envelope, margin, and reserve checks. An asserted,
unavailable, invalid, incomplete, regressed, or producer-epoch-change
observation resets the distinct-clear counter to zero; a neutral observation
also resets the counter and keeps admission closed while recovering, but does
not itself trigger a yield from normal. A duplicate does not advance it. A priority yield uses the
existing control exception and canonical model-release owner; it discards the
incomplete chunk, retries the same source cursor, never changes the selected
model, never consumes a media failure, and never creates a marker or deletion
decision. At Frigate's explicit five-minute floor it unloads, waits, and retries
without shrinking below five minutes.

The host-side Frigate producer is a separate low-priority service and is the
only owner of Frigate/Ollama evaluation. Subgen never calls, starts, stops, or
configures either service. The producer polls exact loopback endpoints every
five seconds. `FRIGATE_PRIORITY_ORIGIN` defaults to and, on Ashby's host, equals
exactly `http://127.0.0.1:5000`; `OLLAMA_PRIORITY_ORIGIN` defaults to and equals
exactly `http://127.0.0.1:11434`. A configured origin must be plain HTTP, literal
`127.0.0.1`, one explicit decimal port in `1..65535`, and contain no userinfo,
path, query, or fragment. Frigate requests use fixed `/api/stats` and
`/api/version` paths. Ollama uses fixed `/api/ps` to inspect currently loaded
models; `/api/tags` is prohibited because installed-but-unloaded models are not
pressure. HTTP redirects are rejected. Connect timeout is 1 second, read
timeout is 2 seconds, total request deadline is 3 seconds, and streamed bodies
are capped at 2 MiB for Frigate and 256 KiB for Ollama before JSON parsing.
Responses must be 200 JSON with duplicate-key rejection; timeout, oversize,
wrong content, or any other status is unavailable. Ollama's root must be an
object with a `models` array of 0..128 objects; an empty array is idle and any
nonempty array is busy. Missing/non-array/oversized content is unavailable, and
model names or installed tags are neither required nor exposed. The local NVIDIA subprocess
has a 2-second deadline and 64 KiB stdout/stderr cap. It executes an argument
array, never a shell, equivalent to
`nvidia-smi --id=<policy.gpu_index> --query-gpu=index,uuid,driver_version,compute_mode --format=csv,noheader,nounits`.
Exactly one UTF-8 row with four comma-separated trimmed fields is required.
The first field is a nonnegative base-10 index and, together with UUID and
driver, must exactly equal the private policy; mismatch is
`policy_drift`. Compute mode must be exactly `Default`; another well-formed
value is `higher_priority_degraded`. Timeout, nonzero exit, malformed UTF-8/
field count, missing/multiple rows, or unsupported value is
`higher_priority_unavailable`. Those are the only NVIDIA producer-decision
fields. GPU memory/utilization is excluded, and Xid/OOM remains an immediate
host-gate abort rather than a producer classification. URLs, response bodies, and
private identifiers never enter public status, logs, or evidence. The producer reads the
private 15-camera expectation policy outside Git, and writes only the coarse
signal. That private policy binds the expected Frigate version, camera-map and
configuration fingerprints, and Ashby's conservative total-detection threshold
of 80 FPS into Task 11B evidence; topology or policy drift asserts fail-closed.
Frigate 0.17.2 updates `/api/stats` on a slower source cadence, so heartbeat
polls are not independent decisions: assertion and recovery counters advance
only when `service.last_updated` strictly increases.

For every successful Frigate response, the producer builds one normalized
source-decision snapshot containing the non-boolean integer
`service.last_updated`, finite
nonnegative total `detection_fps`, the exact policy camera set with finite
nonnegative `process_fps` and `skipped_fps`, the exact detector set with finite
positive inference speeds, the policy-relevant embedding values, the bound
version/config/policy identities, and the
NVIDIA query-valid flag, bound GPU index/UUID, driver version, and compute mode.
Aggregate utilization and memory use remain diagnostic and are excluded from
both the decision and normalized snapshot.
Each camera ratio is exactly
`process_fps / policy_expected_process_fps`; the strictly positive denominator
comes from the private policy. The normalized numeric values and sorted key sets
are compared by parsed value, not endpoint byte formatting. If a repeated
`service.last_updated` carries any different normalized decision input, the
producer fails closed with `higher_priority_unavailable`, retains the original
source-observed timestamp, and advances no streak. Ollama is deliberately not
part of that same-generation equality tuple: `/api/ps` is an independent current
priority input, so a newly nonempty model list adds `higher_priority_busy`
immediately even when Frigate's source generation repeats. An exact duplicate
Frigate/NVIDIA snapshot republishes the cached source decision unioned with the
current Ollama decision, with a new sequence, observation ID, and heartbeat
time; it cannot assert a two-generation Frigate rule or count toward recovery.

Numeric telemetry never accepts a JSON boolean. `service.last_updated` must be a
positive integer. Total detection FPS and every camera process/skipped FPS must
be finite nonnegative JSON numbers; missing, wrong-type, boolean, NaN/infinity,
or negative values are unavailable. Detection/process FPS zero is a valid idle/
low value; skipped FPS zero is healthy and any value above zero is degraded.
Detector inference speed and each present required/conditional embedding speed
or activity value use the same type rules except they must be positive for
healthy: an exact numeric zero is present-but-stalled and degraded, while a
negative, nonfinite, boolean, wrong-type, or missing required value is
unavailable. Both members of a conditional embedding pair absent is valid idle;
exactly one absent is unavailable. These mappings are exhaustive, so a zero
cannot be relabelled unavailable to make Phase A ineligible or vice versa.

At producer start, before any endpoint probe, it opens and verifies the exact
mode-0700 owner-matching parent with a directory file descriptor. If the old
signal exists, it removes it only through that directory descriptor after
`lstat` proves an owner-matching regular mode-0600 file; an absent target is
accepted, while a symlink, wrong owner/mode/type, or path substitution stops the
producer without touching it. It fsyncs the parent after removal. Thus a
same-boot restart cannot leave a still-fresh prior-epoch clear file in service.
Before its first valid source generation the producer leaves the required file
absent and the consumer stays unavailable/fail-closed. After that boundary it
asserts immediately on unavailable/invalid Frigate, detector, embedding,
Ollama, or NVIDIA telemetry; any loaded Ollama model; policy drift; or any
distinct camera sample with skipped FPS above zero. The one global detection-
high streak increments on each distinct generation with total
`detection_fps >= 80` and resets below 80. The one global `any_low` streak
increments when any camera ratio is below 0.95 and resets only when every camera
ratio is at least 0.95; different offending cameras across consecutive
generations still form one consecutive streak. Either streak reaching two
asserts pressure. On every distinct complete
generation with total detection FPS below 80, zero skipped FPS, every process
ratio at least 0.98, valid detector/conditional-idle embedding and NVIDIA
telemetry, and an empty Ollama model list, the producer emits one clear
candidate immediately. A first high-detection or low-ratio generation, or any
complete generation in the process-ratio 0.95-through-below-0.98 deadband,
emits neutral/pending: it cannot count as clear or reopen recovery admission.
The two global streaks follow those reset rules; unavailable,
invalid, epoch-change, or policy-drift input resets both streaks. The producer
does not own recovery hysteresis. Duplicate
generations refresh only the producer heartbeat and cannot assert a two-sample
rule, create another clear candidate, or reopen admission; the controller is the
sole owner that counts three consecutive distinct clear candidates. Aggregate GPU utilization is
diagnostic context only and never controls the signal. Each write uses a
mode-0600 temporary file, file fsync, atomic replace, and directory fsync; a
dead writer naturally becomes stale and therefore fails closed.

Task 11A creates and tests the owner-operated `unloaded_gpu_envelope.py` schema/
generator; Task 11B generates an owner-only
`subgen.unloaded-gpu-envelope/v1` artifact only after the highest-qualified
candidate model is known. A different candidate or policy requires a different
artifact. Its exact logical shape is:

```json
{
  "schema": "subgen.unloaded-gpu-envelope/v1",
  "runtime_commit": "40-lowercase-hex",
  "image": {
    "oci_index": "sha256:64-lowercase-hex",
    "config_digest": "sha256:64-lowercase-hex",
    "layer_diff_ids": ["sha256:64-lowercase-hex"]
  },
  "gpu": {
    "uuid": "GPU-canonical-lowercase-uuid",
    "driver_version": "bounded-version"
  },
  "backend": {
    "cuda_version": "bounded-version",
    "ctranslate2_version": "bounded-version",
    "stable_ts_version": "bounded-version",
    "generator_sha256": "64-lowercase-hex"
  },
  "model_policy": {
    "selected_model": "large-v3",
    "model_revision": "hf:40-lowercase-hex",
    "compute_type": "float16",
    "device": "cuda",
    "device_index": 0,
    "task": "translate",
    "language": "en",
    "chunk_seconds": 300,
    "overlap_seconds": 5,
    "fixture_sha256": "64-lowercase-hex",
    "priority_policy_sha256": "64-lowercase-hex"
  },
  "measurement": {
    "cycles": [{
      "cycle_index": 1,
      "container_id_sha256": "64-lowercase-hex",
      "load_generation_before": 0,
      "load_generation_after": 1,
      "inference_completed": true,
      "inference_result_sha256": "64-lowercase-hex",
      "unload_generation_before": 0,
      "unload_generation_after": 1,
      "candidate_bytes_samples": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    }],
    "cycle_count": 3,
    "samples_per_cycle": 10,
    "interval_seconds": 1,
    "margin_bytes": 134217728,
    "max_observed_candidate_bytes": 0,
    "allowed_unloaded_bytes": 134217728
  }
}
```

Every displayed object has exactly the displayed keys. SHA/revision strings use
the literal prefixes and lowercase lengths shown. `layer_diff_ids` has 1..256
entries in image order. GPU UUID is canonical `GPU-` plus a lowercase UUID;
version/compute/language strings are printable ASCII of length 1..64.
`selected_model` is one of `tiny|base|small|medium|large-v3`, `device` is
`cuda`, `device_index` is a non-boolean integer in `0..31`, `task` is
`transcribe|translate`, and chunk/overlap are the non-boolean integers 300 and
5. The fixed measurement metadata means only `cycle_count=3`,
`samples_per_cycle=10`, `interval_seconds=1`, and `margin_bytes=134217728`;
the displayed maximum and allowed values are illustrative derived values.
`cycles` has exactly
three records ordered by `cycle_index=1,2,3`; generation and sample values are
non-boolean integers in `0..2^63-1`. Each clean process begins with both
before-generations exactly zero and ends with both after-generations exactly
one. All three `container_id_sha256` values are distinct, and the exact prior
container is stopped, PID-empty, and removed before the next is created.
`inference_completed` is the JSON boolean true, and
each sample array has exactly ten entries. The recorded maximum equals the
maximum of all 30 samples and allowed bytes equals that maximum plus 134217728
without integer overflow.

Each cycle record also binds a SHA-256 digest of the exact full container ID and
the completed inference result. The container-ID preimage is the canonical
lowercase 64-hex full Docker ID encoded as ASCII with no newline. The inference-
result preimage is the exact regular disposable SRT file after the production
writer closes and fsyncs it; it must be UTF-8 without BOM, use LF line endings,
and have exactly one trailing LF. In each clean disposable container cycle,
load the exact selected model/policy, complete one inference, invoke the
canonical unload/cache release, require load generation and unload generation
each to increment exactly once and atomic status `model_resident=false`, then
take ten one-second host samples. For each sample, resolve the exact full
container ID to its current cgroup PIDs and descendants, require that set to be
unchanged across the query, and run exactly
`nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits`.
Parse each row as exactly three comma-separated, trimmed fields: a positive
base-10 PID, canonical GPU UUID, and nonnegative base-10 MiB integer. Reject
`N/A`, malformed/extra fields, overflow, a duplicate `(pid,gpu_uuid)` row, or a
row for a candidate PID on another GPU. Sum only candidate-PID rows on the bound
UUID as `used_memory_mib * 1048576` bytes. A valid query with no candidate PID entry is exactly zero; an unresolved
cgroup/PID, unknown unit, device mismatch, query failure, or concurrently
changing process set invalidates the sample rather than becoming zero.

The generator enforces exact keys/types/ranges, canonical ASCII JSON with
sorted keys and one trailing newline, and complete identity/policy equality in
its executable tests. The artifact records the 30 samples, their maximum candidate-attributed bytes,
a fixed 128 MiB measurement/reaction margin, and
`allowed_unloaded_bytes = max_observed_candidate_bytes + 134217728`. It is
create-once, mode 0600, fsynced, SHA-256 sealed, and accepted only when all 30
samples and all three cycles are valid. Task 11B revalidates every bound identity
before use. Its independent unload inequality is exactly
`current_candidate_attributed_bytes <= allowed_unloaded_bytes`; aggregate
device memory or disappearance of an unverified PID cannot satisfy it.

The Frigate gate must distinguish a protected cooperative yield from an
uncontrolled camera regression. It first requires a separate real busy or
degraded assertion episode backed by valid Frigate telemetry; fail-closed
`unavailable` pressure cannot satisfy this proof. N must contain at least one of
`higher_priority_busy|higher_priority_degraded`, contain neither
`higher_priority_unavailable` nor `policy_drift`, and pass every required
telemetry/policy validity check. Before assertion N, the exact
selected model must be resident and one bound disposable workload must be
inside an active, uncommitted chunk. The observer records a privacy-safe
workload digest, its source cursor, load/unload generations, exact active and
uncommitted booleans, and absence of any published output or marker. Before the
workload starts, the frozen host supervisor establishes creation watches on the
owner-only disposable output and marker roots. It maintains monotonic cumulative
counts for successful final-output and marker creations; deletion cannot reduce
either count, so zero deltas through recovery prove that no transient artifact
was created and removed. Phase-A time zero is the host
supervisor's `time.monotonic_ns()` captured immediately after it opens the final
post-rename signal path and validates/fstats/parses exact observation N. The
pre-assertion event must precede T0 and the first status consuming N cannot
precede it. All
deadlines below use that one T0. The yield deadline ends at the first atomic
candidate status that exposes N's digest, an incremented pressure-transition
sequence, `yielding|recovering`, and coarse reason `priority_pressure`; it must
be no later than T0+15 seconds. The runtime-unload deadline ends at the first
atomic status with `model_unload_generation = prior + 1` and
`model_resident=false`; it must be no later than T0+30 seconds. Before
independent unload proof, every existing camera/detector/embedding threshold
still aborts immediately. Unload proof requires both the atomic runtime status
and the first valid host-attributed GPU sample at or below the exact matching
unloaded envelope no later than T0+45 seconds; a log substring is insufficient. Only after that
proof may an intrinsic Frigate breach remain masked while the candidate stays
unloaded. From T0 through that GPU proof, the frozen supervisor samples the
original camera/detector/embedding health contract continuously at no more than
two-second gaps. It seals cumulative sample, blind-interval, and threshold-
failure counts; both latter counts must be zero. Masking is ineligible before
both runtime-unload and GPU-envelope proof, and becomes ineligible again as soon
as the model is resident. Model load generation must remain unchanged until three strictly
increasing clear source generations have been consumed, after which the same
already-bound workload must prove admission, model reload, retry from the
recorded cursor, one final output, and completion without any partial output or
marker during the interruption.

The cooperative-yield episode does not count toward publication health time.
After it passes, the gate resets all health evidence and requires a separate,
fresh, uninterrupted 900-second candidate observation with signal state exactly
`clear`, the candidate running, the intended disposable workload active, the
selected model resident, and the original camera/detector/embedding thresholds
enforced throughout. The status transition sequence and its latched transition
digest remain unchanged, every normal status has a null recovery reason, and
each producer snapshot is directly below the private 80 detection-FPS ceiling.
The separate Phase-B workload hash is bound before candidate start. A second
owner-only trace contains every consecutive append-journal receipt from the end
of Phase A through a post-900-second sentinel, and every receipt during the
acceptance interval must retain that exact workload, active state, fixed model
identity, and unchanged completion/load/unload/failure generations. This
lossless trace—not five-second public-status sampling—rules out a failure,
cancellation, replacement, or unload/reload that reverses between samples.
Any
state other than clear, including neutral, asserted, unavailable, or epoch
change, resets that timer. A candidate-absent signal
that remains asserted under normal traffic pauses Subgen rather than authorizing
a weaker gate.

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
    remote pull;
25. legacy monitor, repair timer, and repair service state capture,
    stop/disable verification, candidate isolation, and deletion-off v0.3.0
    restoration policy;
26. strict priority-signal parsing, owner/mode/inode/path validation, duplicate
    source-generation suppression, one-second signal cadence independent of the
    generic five-second resource cache, stale/unavailable fail-closed behavior,
    and the exact asserted/clear hysteresis transitions;
27. append-only gate receipt creation, canonical record bytes, checked
    single-write plus `fsync` ordering, sequence continuity, inode/size/partial-
    record rejection, exact workload transitions, and lossless capture of model,
    cursor, completion, CUDA-OOM, and media-failure generations;
28. process-lifetime CUDA-OOM and media-failure counters increment at every
    classified terminal boundary and never for cooperative yield, admission
    refusal, cancellation, or an unclassified exception; and
29. all four gate-only environment variables are either all empty or all valid;
    Phase A and Phase B bind distinct immutable workload hashes in order, reject
    a foreign/concurrent workload, and prove zero Docker/cgroup/runtime/log/Xid
    failure deltas without relying on five-second public-status sampling.

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
SEGMENTATION_CHUNK_MINUTES=5
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
# GPU_MEMORY_RESERVE_GIB is the positive value recorded by the released audit;
# `auto` is prohibited for this canonical shared-CUDA deployment.
PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json
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

1. remain blocked until future evidence supplies a positive priority reserve,
   the private Frigate expectation policy and producer are installed, the
   owner-only `/run/subgen-priority` directory is mounted read-only into the
   candidate, `PRIORITY_PRESSURE_FILE` equals the exact path above, and the
   representative-traffic gate can run. An empty/missing path is a deployment
   failure on this canonical host, not public-default fallback;
2. capture the exact enabled/active states of the legacy Subgen monitor, repair
   timer, and repair service; back up the v0.3.0 Compose/config, state, model
   cache, OCI identity, generation registry, and units; then stop, disable, and
   verify inactive the legacy monitor, repair timer/service, and old Subgen
   container without touching Frigate or Ollama;
3. before public release, run explicit envelope bootstrap with the exact
   candidate, separate model cache/state/media/output roots, the exact required
   priority path/mount/policy, and a profiling-
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
   minutes of representative traffic, including the separate causal priority-
   assertion/unload/reload proof followed by the uninterrupted 900-second clear
   pass. Abort immediately on any NVIDIA Xid,
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
  segmented transcription, cooperative last-priority memory yielding,
  optional host-owned shared-accelerator priority yielding, and selective
  invalid-media deletion.
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
