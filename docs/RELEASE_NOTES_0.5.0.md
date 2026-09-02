# Subgen English for Plex 0.5.0

Version 0.5.0 makes long-file transcription practical on smaller and shared
machines by processing films and episodes in time-bounded chunks, starting at
5 to 30 minutes based on capacity, shrinking toward five minutes under
pressure, and joining the finished work into one subtitle file. Its RAM and
VRAM admission policy selects the highest-quality safe multilingual Whisper
model when the operator has not chosen one, selects it once before the first
load, keeps it fixed for the process and every retry, and uses segmentation to
bound duration-driven allocations without pretending it can shrink the model
weights themselves.

## Highlights

- `WHISPER_MODEL=auto` evaluates multilingual models from `large-v3` down to
  `tiny`, selects the highest candidate the current admission checks can safely
  load, and keeps that choice fixed for the process and every file retry.
- Repeated measurements from the exact packaged image can authorize a model
  through an immutable `ModelEnvelope`. Generic RAM and allocatable-VRAM tiers
  are conservative fallbacks, not proof that a backend will fit.
- Three fresh exact-device samples stabilize CUDA free memory. The separate GPU
  reserve is then subtracted before selection and checked again inside the load
  gate.
- Long local files use bounded sequential extraction, five-second context
  overlap, seam-aware timestamps, and one atomic SRT/LRC publication. A small
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
- If two consecutive bounded attempts still cannot allocate at the five-minute
  floor, Subgen emits a typed `resource_exhaustion` worker event and retains
  the media. When the optional failure monitor is running, that event becomes
  an exact-generation marker. Memory pressure is never treated as evidence
  that the video itself is corrupt.
- Shared-GPU operators can add a required owner-only priority signal. Its host
  producer watches Frigate, currently loaded Ollama work, and the policy-bound
  NVIDIA device without taking control of any of them. It translates that
  private detail into a coarse clear, neutral, asserted, or unavailable state,
  while Subgen keeps all admission, yield, and recovery decisions in one
  controller.
- `SKIP_STARTUP_SCAN` controls only automatic startup catch-up. An explicit
  `/batch` request still walks the requested path once, submits discovered
  files to the normal queue checks, and never registers a second watcher. It
  does not wait for transcription to finish.
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
  stronger shared-host admission, and stricter media classification. It keeps
  first-failure skip as the public default and narrows optional deletion to
  media that both independent validators conclusively reject.

## Public defaults

For a normal public installation, the safety features are useful without any
Frigate-specific setup. The packaged profiles keep the public 10 GiB
hard/no-extra-swap limit and one transcription at a time. Automatic model
selection, adaptive segmentation, and pressure yielding are enabled.
First-failure marking is enabled when the optional failure monitor is installed
and running. Deletion and the optional shared-host priority signal remain off.
The complete defaults are:

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
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

An empty `PRIORITY_PRESSURE_FILE` keeps this optional integration disabled. A
non-empty absolute path makes the signal mandatory and fail closed: missing,
stale, invalid, or replayed input pauses new work instead of guessing that the
shared device is safe.

The three base Compose profiles also contain no priority-signal bind. A reviewed
shared host adds `docker-compose.priority-pressure.yml`, which mounts only
`/run/subgen-priority` read-only with host-path creation disabled. That keeps the
normal install genuinely optional and lets atomic signal replacements remain
visible when the integration is selected.

On CPU, 4 GiB and 6 GiB capacity profiles have a `small` fallback ceiling. A
9 GiB profile has a `medium` fallback ceiling, but only when fresh host and
cgroup admission still covers the model's nonzero load budget, margin, and
reserve. Worse current use can select a lower model or leave Subgen waiting in
`no_safe_model` recovery. CUDA applies the lower of the system-memory and
allocatable-VRAM fallback ceilings. Exact matching envelope evidence is
authoritative and may qualify a higher model; a tag, total-VRAM figure, or one
idle free-memory reading cannot.

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
files, `SEGMENTATION_CHUNK_MINUTES=5`, startup scanning enabled, a 10 GiB
hard/no-swap runtime limit, and a positive audited `GPU_MEMORY_RESERVE_GIB`;
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
The 10 GiB limit becomes production authority only after the exact candidate
passes the isolated Frigate gate. A 12 GiB cgroup is allowed only for explicit
profiling and cannot authorize the automatic or production model.

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

1. Install the complete v0.5.0 checkout; the source profile now mounts the
   profiler at the same path used by the packaged image.
2. Review `.env.example` and set the v0.5 public defaults above. Keep
   `SUBGEN_MEMORY_LIMIT=10g` unless separate evidence justifies another limit.
   Leave `PRIORITY_PRESSURE_FILE` empty unless a trusted host producer and its
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
candidate still has to pass its isolated acceptance gate on the Frigate host
before publication and the controlled rollout.

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
