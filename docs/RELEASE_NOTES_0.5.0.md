# Subgen English for Plex 0.5.0

A longer film should take longer to transcribe, not require ever-growing RAM.
That is the main change in version 0.5.0. Subgen now handles long films and
episodes as a sequence of hardware-sized time windows, normally 5 to 30 minutes
each. It keeps only the current window in memory, commits completed subtitle
segments to a private temporary journal, and streams them into one normal
subtitle file at the end. If memory becomes tight, Subgen releases the current
window and model, waits for the more important workload, and retries the same
position with a smaller window.

Available RAM and VRAM still determine which model can load safely. Once a safe
model fits, however, programme length no longer makes the in-memory transcript
grow without bound. When the operator has not chosen a model, Subgen selects the
highest-quality safe multilingual Whisper model for the measured hardware and
keeps that model fixed for the process and every retry. Segmentation bounds the
duration-driven work around those model weights; it does not pretend that the
weights themselves can shrink while loaded.

There is also an optional Home Assistant view for the work ahead. When it is
enabled, Subgen inventories the complete configured library before decoding and
publishes two simple MQTT sensors: **Subgen Items Left** and **Subgen Scan %**.
This is deliberately an opt-in convenience, not a new requirement for running
Subgen.

## What changes in everyday use

- The same release adapts to 4, 6, 9, 12, 16, 24, 32, 64, and 128 GiB
  machines. Setup gives Subgen a hardware-sized limit, keeps a reserve for the
  rest of the machine, and never lets one transcription grow beyond 24 GiB.
- Longer media is processed one time window at a time and still produces one
  ordinary subtitle file. Completed windows do not accumulate in RAM while the
  rest of the film or episode is transcribed.
- Automatic mode chooses the highest-quality model that fresh RAM and VRAM
  checks can safely admit. More capable hardware can therefore improve quality
  or headroom without making long files a special failure case.
- Subgen is deliberately the lowest-priority workload. If another service
  needs the protected memory, Subgen releases unfinished work at a safe boundary,
  waits, and retries the same position with a smaller window.
- With optional application-priority checks enabled, recovery also handles a
  model that is still loaded after a pause. If keeping it loaded prevents the
  recovery budget from fitting, Subgen unloads it before checking again instead
  of waiting indefinitely for memory it is holding itself. The normal safety
  reserves and reload checks still apply.
- The logs now read like a job timeline. They show the selected model and
  memory budget, planned chunks, whole-file progress, real retry starts, the
  final join, and successful completion. Estimates are labelled as estimates
  rather than presented as live usage.
- If a backend returns a word or segment whose start is later than its end,
  the error now includes both times and their difference in seconds, without
  printing the dialogue. Invalid output is still rejected; the extra detail
  helps diagnose the timing problem rather than silently altering subtitles.
- A compact machine-readable receipt accompanies a successful multi-chunk job
  so the private pre-release soak can prove that atomic publication really
  completed. It contains an opaque token and aggregate timing/count data, not a
  filename, title, media path, or subtitle text.
- Operators who enable MQTT can see the complete subtitle backlog and startup
  scan progress in Home Assistant. Subgen starts the watcher before that scan,
  so a new import arriving during a large inventory is still seen, but workers
  wait for the whole baseline before decoding begins.
- When the optional failure monitor is installed, its public default marks the
  first qualifying failed file generation and future scans skip only that exact
  generation. Public deletion remains off. Ashby's Frigate profile may delete
  only an unchanged import that both FFprobe and PyAV conclusively reject as
  invalid media; valid, silent, crashed, or memory-constrained files are kept.

## Technical highlights

- `WHISPER_MODEL=auto` evaluates multilingual models from `large-v3` down to
  `tiny`, selects the highest candidate the current admission checks can safely
  load, and keeps that choice fixed for the process and every file retry.
- Repeated measurements from the exact packaged image can authorize a model
  through an immutable `ModelEnvelope`. Generic RAM and allocatable-VRAM tiers
  are conservative fallbacks, not proof that a backend will fit.
- Three fresh exact-device samples stabilize CUDA free memory. The separate GPU
  reserve is then subtracted before selection and checked again inside the load
  gate.
- Long local files use bounded sequential extraction, a private disk-backed
  transcript journal, five-second context overlap, seam-aware timestamps, and one
  streamed atomic SRT/LRC publication. Completed windows no longer remain as a
  growing Python object or get copied again for every later window. A small
  timing disagreement at a join no longer has to abort the whole file. If two
  overlap decodes contain the same phrase and Subgen cannot prove whether it is
  a duplicate or genuine repeated speech, it keeps both rather than risk
  deleting words.
- Cooperative pressure handling makes Subgen a last-priority workload. It
  abandons only the in-progress chunk, releases the model and caches at a safe
  boundary, waits without counting a file failure, and retries the same source
  position with a smaller chunk. A genuine higher-priority interruption also
  separates consecutive allocation attempts, so two unrelated pressure
  episodes are not mistaken for one repeatedly failing file.
  After three consecutive healthy chunks, the working duration doubles back
  toward its original capacity-based baseline instead of remaining permanently
  reduced.
- Human-facing RAM control reports guest `MemAvailable`, the protected reserve,
  current cgroup use and finite limit, the automatic quality ceiling, the fixed
  model's admission requirement, and current working headroom. A requirement
  is a measured envelope or conservative estimate plus margin, not live model
  RSS; working headroom is not a separately reserved chunk pool. Missing live
  constraints are shown as unavailable rather than guessed.
- If two consecutive bounded attempts still cannot allocate at the five-minute
  floor, Subgen emits a typed `resource_exhaustion` worker event and retains
  the media. When the optional failure monitor is running, that event becomes
  an exact-generation marker. Memory pressure is never treated as evidence
  that the video itself is corrupt.
- Shared-GPU operators can add a required owner-only priority signal. Its host
  producer always watches Frigate and the policy-bound NVIDIA device. Ollama is
  a separate opt-in: leave its origin blank when it is absent or intentionally
  stopped, or configure it so a loaded model—and any loss of that configured
  telemetry—makes Subgen yield. The producer never takes control of those
  services. It translates their private detail into a coarse clear, neutral,
  asserted, or unavailable state, while Subgen keeps all admission, yield, and
  recovery decisions in one controller.
- `SKIP_STARTUP_SCAN` controls only automatic startup catch-up. An explicit
  `/batch` request still walks the requested path once, submits discovered
  files to the normal queue checks, and never registers a second watcher. It
  does not wait for transcription to finish.
- The optional MQTT inventory uses retained Home Assistant discovery, state,
  and availability. It refreshes every 60 seconds and sends important scan and
  completion changes immediately. Sensor attributes contain per-library
  aggregate counts only; media names, full paths, titles, subtitle text, and
  internal path hashes are never published. Labels are generic unless the
  operator explicitly supplies display names such as `Movies|TV`; direct-file
  entries receive generic labels such as `Direct file 1`. Custom library names
  are published in retained MQTT state and Home Assistant attributes, so they
  should never contain private paths or film and show titles. Leaving them
  blank keeps the privacy-safe generic labels.
- A configurable startup-scan watchdog defaults to 21,600 seconds (six hours).
  If the inventory cannot finish, it is reported as incomplete with a scan
  error and transcription continues. MQTT or Home Assistant can therefore
  never hold the subtitle queue indefinitely.
- The packaged container runs Subgen itself as the main process. A normal
  Docker stop, Compose update, or host shutdown now reaches Subgen's graceful
  cancellation and cleanup path instead of leaving a launcher waiting on a
  child process until Docker kills the container.
- On a shared GPU, the model profiler continues reading the higher-priority
  signal while it establishes a stable three-sample VRAM baseline. It cannot
  miss Frigate activity during that wait, and any assertion or unavailable
  signal is delivered to the admission controller before model loading.
- With the optional failure monitor installed, the first qualifying terminal
  failure marks and skips only the exact file generation. This is the public
  default, so repeated library scans do not churn on a known failure. A
  replacement at the same path proceeds normally.
- Deletion remains optional and is deliberately narrower than skipping. Only
  the monitor may delete unchanged current media, and only after typed FFprobe
  and isolated PyAV both conclusively report an invalid format and the marker
  has been durably written. A valid silent video, transcription failure,
  memory failure, or native crash is retained.
- `repair_subgen_failures.py` is now report/evidence-only. Its legacy `delete`
  action is accepted for migration but never removes media or empty subtitle
  markers.

## Compared with earlier releases

- **v0.4.0** added exact-generation failure markers. After the first qualifying
  failure, the next scan skips that exact file generation instead of repeatedly
  reopening the same bad import. A replacement at the same path has a different
  fingerprint and is eligible for processing.
- **v0.4.1** kept the v0.4.0 behavior and fixed secure quarantine compatibility
  for NFSv4 filesystems that inherit a harmless set-group-ID bit on a new
  owner-only directory.
- **v0.5.0** adds the memory-aware transcription path: adaptive chunks,
  pressure-driven yield and retry, automatic highest-safe model selection,
  stronger shared-host admission, stricter media classification, clearer human
  progress logs, and an optional Home Assistant inventory. It keeps
  first-failure skip as the public default and narrows optional deletion to
  media that both independent validators conclusively reject.

## Public defaults

For a normal public installation, the safety features are useful without any
Frigate-specific setup. The packaged profiles keep one transcription at a time
and require a generated hard/no-extra-swap limit derived from the selected
Docker engine. Automatic model selection, adaptive segmentation, and pressure
yielding are enabled. First-failure marking is enabled when the optional failure
monitor is installed and running. Deletion and the optional shared-host priority
signal remain off. MQTT inventory reporting is also off until a broker is
configured explicitly. The complete runtime defaults are:

```dotenv
MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json
MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MQTT_INVENTORY_ENABLED=False
MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS=21600
MEMORY_PRESSURE_RESERVE_GIB=auto
GPU_MEMORY_RESERVE_GIB=auto
PRIORITY_PRESSURE_FILE=
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

Before running Compose, `python3 configure_capacity.py` measures the selected
Linux Docker engine, protects 15% of its stable memory for other workloads
(never less than 1 GiB), and writes a literal integer-MiB boundary to
`.subgen-capacity.yml`. Every Compose profile extends that file, so a stale
shell setting cannot replace the generated limit. On a ballooned VM, rootless
user slice, or nested daemon, pass the guaranteed floor with
`--guaranteed-memory-gib`; the engine cannot safely infer a future lower parent
limit. The configurator fails closed if Docker cannot prove its memory and
no-extra-swap controls. Rootless use also requires cgroup v2 with systemd.

The automatic profiles are deliberately easy to audit:

| Stable machine/VM memory | Minimum protected reserve | Subgen hard limit | Highest generic model target | Initial window |
| ---: | ---: | ---: | --- | ---: |
| 4 GiB | 1 GiB | 3 GiB | `small` if fresh admission fits; commonly `base` | 5 min |
| 6 GiB | 1 GiB | 5 GiB | `small` | 10 min |
| 9 GiB | 1.5 GiB | 7.5 GiB | `medium` | 10 min |
| 12 GiB | 2 GiB | 10 GiB | `medium` | 20 min |
| 16 GiB | 2.5 GiB | 13.5 GiB | `large-v3` | 20 min |
| 24 GiB | 3.75 GiB | 20.25 GiB | `large-v3` | 30 min |
| 32 GiB | 5 GiB | 24 GiB | `large-v3` | 30 min |
| 64 GiB | 9.75 GiB | 24 GiB | `large-v3` | 30 min |
| 128 GiB | 19.25 GiB | 24 GiB | `large-v3` | 30 min |

These are ceilings, not promises. Fresh host, cgroup, and GPU admission can
choose a smaller model or wait. In particular, the 12 GiB profile is **not** an
8 GiB model allocation plus a separate 3.5 GiB chunk allocation. It protects
2 GiB for the rest of the machine, gives Subgen a 10 GiB hard limit, and keeps
about 1 GiB of internal cgroup headroom. The interpreter, runtime, resident
model, decoded audio, and one active window all share what remains. The
conservative fallback requirement for `medium` is about 5.5 GiB including its
margin, so no more than 3.5 GiB can already be in use when that model is
admitted (`10 - 1 - 5.5`). That 3.5 GiB is an admission boundary, not memory
reserved for a chunk. If current use is even one byte higher, fallback admission
rejects `medium` and tries a smaller model. After loading, actual headroom
determines whether the current window continues, waits, or retries smaller. The
20-minute entry is an initial duration target, not a separate memory allocation.

The Subgen limit stops growing at 24 GiB. More host memory still grows the
protected reserve, but `large-v3` is already the quality ceiling and automatic
windows stop at 30 minutes, so allowing one transcription to consume 50 or
100 GiB would add risk without improving subtitle quality. At 32 GiB and above,
the 24 GiB cap leaves more memory outside Subgen than the minimum reserve column
alone shows.

An empty `PRIORITY_PRESSURE_FILE` keeps this optional integration disabled. A
non-empty absolute path makes the signal mandatory and fail closed: missing,
stale, invalid, or replayed input pauses new work instead of guessing that the
shared device is safe.

The three base Compose profiles also contain no priority-signal bind. A reviewed
shared host adds `docker-compose.priority-pressure.yml`, which mounts only
`/run/subgen-priority` read-only with host-path creation disabled. That keeps the
normal install genuinely optional and lets atomic signal replacements remain
visible when the integration is selected.

CUDA applies the lower of the system-memory and allocatable-VRAM ceilings. Exact
matching envelope evidence is authoritative and may qualify a higher model; a
tag, total-VRAM figure, or one idle free-memory reading cannot. Worse current
use can select a lower model or leave Subgen waiting in `no_safe_model`
recovery.

The three base Compose profiles do not bind host ModelEnvelope evidence. That
makes the normal public path directly runnable: absent evidence produces one
bounded reason and automatic selection uses conservative fallback. Exact-
evidence or canonical deployments add `docker-compose.model-envelopes.yml`, an
opt-in read-only overlay that binds the host parent
`/var/lib/subgen/model-envelopes/v1` plus its `catalog.json` and
`image-identity.json` leaves at the three exact container paths. The parent must
be mode `0700`, each regular leaf mode `0600`, and all three must be owned by the
numeric UID seen as the Subgen runtime EUID. The parent bind preserves that
metadata for strict container-side validation, and `create_host_path: false`
prevents Docker from inventing missing host paths. The identity schema is
`subgen.model-envelope.identity/v1`; the catalog schema is
`subgen.model-envelope.catalog/v1`. Missing, unsafe, malformed, integrity-
invalid, or non-matching evidence produces one bounded reason. Public auto then
uses fallback policy; canonical shared CUDA fails closed.

Only the owner-operated `/subgen/profile_model_envelopes.py` may write a staged
catalog. It profiles one explicit model per clean process for at least three
cold cycles, uses the canonical admission owner, and never rewrites the
identity artifact or the running catalog. Before every profiler or automatic
overlay-enabled container start, compare the candidate's OCI configuration
digest and ordered rootfs layer diff IDs with the owner-only identity file. Do
not use a mutable tag or registry manifest alone as runtime identity.

## Operator-specific Frigate deployment

Ashby's intended Frigate deployment is an operator-specific profile, not a
public default, and an ordinary public upgrade does not enable it. Subgen will
share an RTX 3090 with Frigate and Ollama, but both remain higher priority.
Subgen never stops, reconfigures, or lifecycle-manages either service. A
separate low-priority host service reads their exact local health sources and
publishes only a coarse owner-only priority signal; Subgen does not contain
Frigate camera names, private thresholds, credentials, or Ollama lifecycle
rules.

The producer is supplied as `monitor_frigate_priority.py` with its own example
environment and systemd unit. It accepts only exact literal-loopback Frigate and
Ollama origins, uses Frigate's real plain-text `/api/version` response alongside
strict JSON stats, and checks only the policy-bound NVIDIA identity and compute
mode. It never treats an installed-but-unloaded Ollama model, total VRAM, or one
free-memory reading as evidence that higher-priority work is active.

“Higher priority” is cooperative policy, not CUDA preemption. Subgen samples
the signal at least once per second while idle and during active inference. An
assertion, producer restart, or unavailable signal immediately closes model
admission and releases resident model state at the next safe callback; it cannot
interrupt a CUDA kernel already running. Recovery needs three distinct clear
eligible source generations; the first publication in a new producer epoch
never counts. The Frigate-only five-minute window shortens the work between
callbacks, but no chunk size can promise zero camera impact without the
representative-traffic gate.

That deployment will combine the GPU base with the supplied ModelEnvelope
overlay and use `WHISPER_MODEL=auto`, the exact read-only catalog and identity
files, `SEGMENTATION_CHUNK_MINUTES=5`, startup scanning enabled, and a positive
audited `GPU_MEMORY_RESERVE_GIB`;
`GPU_MEMORY_RESERVE_GIB=auto` is prohibited there. It also requires
`PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json` and the matching
read-only owner-only parent mount from
`docker-compose.priority-pressure.yml`. The host producer names that same leaf
with `FRIGATE_PRIORITY_SIGNAL_FILE`. Its stable parent is mode `0700`, its
atomic publication mode `0600`, and both sides use the same numeric UID. The
five-minute setting is specific to this
shared-GPU deployment and its measured evidence. Public profiles remain `auto`,
and the Frigate profiler catalog must be produced with the same five-minute
policy used by its candidate and production runtime.
The target VM has a 20 GiB guaranteed balloon floor, so
`configure_capacity.py --guaranteed-memory-gib 20` generates a 17 GiB
hard/no-extra-swap Subgen limit. That generated limit becomes production
authority only after the exact candidate passes the isolated Frigate gate. The
older 10/12 GiB candidate evidence belongs to an earlier runtime and cannot
authorize this release.

After the deployment gate passes, Ashby's intended policy differs from the
public default in two important ways: the five-minute chunk floor is pinned for
quicker cooperative yielding, and `AUTO_DELETE_INVALID_MEDIA=true` permits the
monitor to remove a conclusively invalid import so Sonarr or Radarr can replace
it. Generic transcription, memory, and `SIGSEGV` failures are still marked and
retained. The deprecated `AUTO_DELETE_FAILED_FILES` remains `false`, and repair
stays inactive/report-only.

The earlier Plex-hosted Subgen instance remains retired. The preserved Frigate
v0.3.0 Compose/config, model cache, and OCI identity form its operational
rollback set; this is separate from the public v0.4.1 rollback described below.

## Back up before upgrading

Back up the active Compose file and overlays, `.env`, `monitor.env`,
`priority-monitor.env`, installed monitor/repair/priority scripts and systemd
units, `SUBGEN_STATE_DIR`, model cache, and the current image tag plus immutable
digest. If a priority producer exists, retain its outside-checkout private
policy, expected hash, exact Frigate config identity, signal-directory metadata,
and enabled/active state. If ModelEnvelope artifacts exist,
back up the parent owner/mode metadata and both complete owner-only files
together with the OCI config digest and ordered layer diff IDs they describe.
Keep all backups outside the media tree.

Record whether monitor, repair, and priority units are enabled and active. Set
both deletion booleans false and repair to `report` before changing versions.

## Upgrade

This is a behavior-changing upgrade, not just a new image tag. Review the new
model, segmentation, reserve, marker, and deletion settings before recreating
the container. The public path needs no priority producer or host evidence
mount; those are opt-in operator features. Backups matter because the public
rollback restores v0.4.1, while Ashby's Frigate deployment has its own preserved
v0.3.0 rollback set.

1. Install the complete v0.5.0 checkout. The source profile now mounts the
   profiler at the same path used by the packaged image and builds its optional
   dependencies locally, so start it with `docker compose up -d --build` rather
   than reusing an older local image.
2. Install `.env.example` as owner-only `.env`, run
   `python3 configure_capacity.py`, and confirm the generated
   `.subgen-capacity.yml`. For a ballooned VM, rootless engine, or nested daemon,
   pass its guaranteed floor rather than its temporary maximum. Leave
   `PRIORITY_PRESSURE_FILE` empty unless a trusted host producer and its
   read-only owner-only signal directory are deliberately installed.
3. For ordinary public fallback, use the selected base profile without host
   evidence setup. For exact evidence, prepare the parent and real catalog plus
   identity leaves with strict owner/modes and add the supplied overlay. Do not
   manufacture trusted evidence from a tag or copy artifacts from a different
   image/runtime/policy.
4. For a reviewed shared host, install and validate the priority producer first.
   Require its stable mode-`0700` parent and mode-`0600` signal under the
   matching UID, then add `docker-compose.priority-pressure.yml`. A missing
   first publication is a failed preflight, not permission to start anyway.
5. In `monitor.env`, add `AUTO_DELETE_INVALID_MEDIA=false`, retain the legacy
   alias as `false`, set both thresholds to `1`, and leave repair on `report`.
6. Validate the selected base Compose file and, when applicable, its merge with
   the ModelEnvelope and priority overlays; recreate Subgen from v0.5.0, restart
   the failure monitor if used, and verify `/status`, model-decision provenance,
   priority state, scan progress, marker readability, and zero restart/OOM
   growth.

If an existing installation combines `SKIP_STARTUP_SCAN=True` with an automated
`/batch` caller, review that schedule before upgrading. In v0.5.0 the explicit
request is no longer suppressed by the startup-only setting: it deliberately
walks and queues the requested path, then returns without waiting for the queued
transcriptions to finish.

Invalid `SEGMENTATION_CHUNK_MINUTES`, `MEMORY_PRESSURE_RESERVE_GIB`, or
`GPU_MEMORY_RESERVE_GIB` values now reject startup instead of being silently
accepted. `SEGMENTATION_ENABLED=False` opts local files out of segmentation but
does not disable model admission, validation, markers, or pressure release and
wait. A yielded whole-file request retries as a whole file and logs that its
duration cannot shrink.

## Disposable smoke test

Use a temporary media and state root, never a production title.

1. Run base Compose validation and require HTTP 200 from `/status`. If exact
   evidence is in scope, also run the installation guide's base-plus-overlay
   Linux metadata and exact-load gate.
2. Process one short valid file and one longer synthetic file; confirm one final
   subtitle, monotonic source timestamps, and no window/temp artifacts.
3. Confirm the status/log decision reports either exact-envelope provenance or
   a bounded public-fallback reason and the expected fixed model.
4. If the priority producer is configured, use disposable work to prove one
   genuine busy/degraded assertion, prompt cooperative unload, and recovery only
   after three distinct clear source generations. A producer restart or stale
   signal must close admission; do not create synthetic load on a production
   camera GPU for this smoke.
5. With deletion off, present a valid silent file, an indeterminate validator
   result, and an inference failure; all must remain.
6. In a separate disposable directory only, enable
   `AUTO_DELETE_INVALID_MEDIA=true`, present a dual-invalid sample, and confirm
   the durable marker audit precedes deletion. Replace it at the same path and
   confirm the new generation is processed normally.

Do not corrupt or delete real library media to test this release.

## Compatibility

HTTP routes, required response fields, queue identity, subtitle naming,
language/task behavior, completion webhooks, directory `.subgen_skip`, and the
schema-v1 exact-generation marker contract remain compatible. Uploaded `/asr`
and OpenAI-compatible byte-buffer requests are unchanged and never enter local-
file segmentation, even when segmentation is enabled.

The repository/image/project version is `0.5.0`. The overlaid upstream runtime
status deliberately remains `2026.07.1`; `/status` reports that stable runtime
version rather than the release tag. This is not an incomplete upgrade.

There is no Sonarr/Radarr API integration in v0.5.0. A deleted disposable or
operator-approved invalid file is replaced only through the operator's existing
library automation. Subgen also performs no Ollama lifecycle coordination.

## Deletion safety

`AUTO_DELETE_INVALID_MEDIA=true` is the canonical opt-in. The deprecated
`AUTO_DELETE_FAILED_FILES=true` alias is accepted through 0.5.x but is narrowed
to the same invalid-media-only path and warns once. Either true value can enable
that path during migration, so set both false to disable it.

Silent/no-audio media, disagreement between validators, timeouts, permission or
I/O failures, generic/inference/resource errors, OOM, pressure yield, SIGSEGV,
log-regex matches, legacy/untyped intents, and stale replacements are retained.
Only a typed dual-invalid result for the unchanged current generation can be
marked and then deleted by the monitor. Repair never deletes media or legacy
empty subtitle markers, even when `SUBGEN_REPAIR_ACTION=delete` is requested.

## Rollback

Rollback does not remove marker history or media. For a public installation,
stop v0.5.0, set both deletion booleans false and repair to `report`, restore
the backed-up v0.4.1 Compose/config/scripts and immutable image digest, and
recreate that container. Recheck `/status`, scan progress, marker readability,
and OOM/restart state. Preserve the schema-v1 marker registry and v0.5 evidence.

If the optional priority integration is being removed while staying on v0.5,
blank `PRIORITY_PRESSURE_FILE` and recreate the container from its base profile
without the priority overlay before stopping the producer. Stopping only the
producer intentionally leaves a configured consumer fail-closed.

The Frigate operational rollback is different: it restores the preserved
v0.3.0 Compose/config, model cache, OCI identity, generation registry, and
captured unit states with deletion disabled first. It does not restore public
v0.4.1 and never recreates the retired Plex-hosted instance.

## How this release is verified

Automated tests, lint checks, image builds, and publication preparation run on
the local PC or the dedicated simulator. This release does not dispatch a
GitHub Actions workflow or consume GitHub-hosted runner time. The exact built
candidate must pass its isolated acceptance gate and then a continuous 72-hour
private soak on the Frigate VM before any GitHub push, tag, release, or GHCR
publication. The soak covers successful long-file transcription and atomic
joins, naturally occurring adaptive yield/retry behavior, marker handling,
Subgen and Frigate health, and restart, OOM, CUDA, and NVIDIA Xid counters.
Controlled simulator pressure and disposable marker tests remain required
because a quiet production period does not prove those paths. Any failed gate
or candidate-affecting change resets the complete 72-hour window.

## Known boundaries

- Segmentation cannot reduce the resident model weights or guarantee that the
  backend reaches a progress callback before its first large allocation.
- Generic CPU/GPU tiers are safety-oriented fallback hypotheses. Only repeated
  exact-runtime evidence can promote beyond them.
- Shared-CUDA telemetry loss or reduced higher-priority headroom can leave
  Subgen waiting indefinitely; that is an intentional fail-closed outcome.
- A configured priority producer is part of the shared-GPU safety boundary. Its
  missing, stale, restarted, replayed, or invalid publication closes admission;
  Subgen never substitutes a fixed schedule or free-VRAM guess.
- ModelEnvelope files are distribution inputs, not secrets or self-generated
  trust. Operators must preserve their ownership, mode, identity, and complete
  provenance chain. The exact-evidence overlay refuses to create missing host
  paths.
- Marker identity still depends on host/container metadata matching across the
  media bind. Prove this against disposable media before enabling deletion.

See the [configuration guide](./CONFIGURATION.md), [migration guide](./MIGRATION.md),
and [full changelog](../CHANGELOG.md).
