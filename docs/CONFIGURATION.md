# Configuration guide

Copy `.env.example` to `.env` and change values there. The compose files contain working conservative fallbacks, but keeping local choices in `.env` makes upgrades easier and prevents private values entering Git.

## Conservative defaults

| Setting | Default | Reason |
| --- | --- | --- |
| `WHISPER_MODEL` | `auto` | Choose the highest admitted multilingual model once; explicit recognized models remain fixed. |
| `SEGMENTATION_ENABLED` | `True` | Bound long local-file work to sequential windows with adaptive retry. |
| `SEGMENTATION_CHUNK_MINUTES` | `auto` | Use the 5/10/20/30-minute capacity tier; explicit values must be integers from 5 through 60. |
| `MEMORY_PRESSURE_YIELD` | `True` | Release and retry an uncommitted window when sustained pressure needs memory. |
| `MEMORY_PRESSURE_RESERVE_GIB` | `auto` | Keep an automatic host reserve while retaining the mandatory cgroup floor. |
| `GPU_MEMORY_RESERVE_GIB` | `auto` | Public GPU floor; canonical shared CUDA requires a positive audited value. |
| `MODEL_ENVELOPE_CATALOG` | `/opt/subgen/model-envelopes/catalog.json` | Read-only exact-runtime measurement catalog; public fallback applies when evidence is unusable. |
| `MODEL_ENVELOPE_IDENTITY` | `/opt/subgen/model-envelopes/image-identity.json` | Read-only OCI config digest plus ordered rootfs layer identity. |
| `CONCURRENT_TRANSCRIPTIONS` | `1` | Predictable RAM/VRAM use and failure attribution. Fixed in the compose templates. |
| `WHISPER_THREADS` | `4` | Leaves CPU capacity for Plex and the host. |
| `SUBGEN_CPU_LIMIT` | `4.0` | Prevents the container consuming the whole server. |
| `SUBGEN_MEMORY_LIMIT` | `10g` | Public hard/no-extra-swap ceiling; segmentation bounds duration-driven work within it. |
| `COMPUTE_TYPE` | CPU `int8`; GPU `float16` | Conservative compute type for each device. |
| `MODEL_CLEANUP_DELAY` | CPU `60`; GPU `300` seconds | Avoids constant reloads while eventually releasing model memory. |
| `SKIP_STARTUP_SCAN` | `False` | Performs a catch-up scan before watching for new files. |
| `SUBGEN_BIND_ADDRESS` | `127.0.0.1` | Does not expose the HTTP service to the LAN by default. |
| `SUBGEN_IMAGE` | release tag `v0.5.0` | Keeps packaged CPU/GPU deployments on the documented release; blank uses the default. |
| `AUTO_MARK_MIN_FAILURES` | `1` | Mark and skip the first qualifying exact-generation terminal failure. |
| `AUTO_DELETE_INVALID_MEDIA` | `false` | Canonical deletion opt-in remains off. |
| `AUTO_DELETE_FAILED_FILES` | `false` | Deprecated alias, narrowed to invalid-media-only deletion. |
| `AUTO_DELETE_MIN_FAILURES` | `1` | First conclusive dual-invalid failure if deletion is explicitly enabled. |
| `SUBGEN_REPAIR_ACTION` | `report` | Repair is report/evidence-only for both accepted action values. |

See the [README hardware guide](../README.md#model-and-hardware-guide) for planning profiles and model warnings.

## Paths and identity

### `MEDIA_ROOT`

Host folder mounted at `/media` inside the container. Mount the smallest common root that contains the intended libraries.

```dotenv
MEDIA_ROOT=/srv/media
```

### Standalone `TRANSCRIBE_FOLDERS` inputs

Plex and the *Arr stack are optional. With their settings blank, `TRANSCRIBE_FOLDERS` can name one container file to process directly at startup:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies/Example.mkv
```

It can also contain a pipe-separated mixture of folders and files:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies|/media/TV/Season-01/Episode-01.mkv
```

Do not put host-only paths here. Folder entries are walked at startup. `MONITOR` is optional and applies only to ongoing directory watching; a direct file entry is processed at startup but is not watched as a directory. The supplied Compose profiles set `MONITOR=True`; custom runtime environments can use `MONITOR=False` for startup-only processing.

### `SUBGEN_MODEL_PATH`

Host folder that persists downloaded model data:

```dotenv
SUBGEN_MODEL_PATH=./models
```

### `PUID` and `PGID`

Numeric Linux identity used for media and subtitle files. Match the owner/group of the media directories.

### `SUBGEN_STATE_DIR`

Host directory mounted read-only at `/opt/subgen/monitor` so Subgen can consume exact-generation failure markers written by the optional host monitor:

```dotenv
SUBGEN_STATE_DIR=./monitor
SKIP_MARKED_FAILED_FILES=true
```

Keep it on a local filesystem outside `MEDIA_ROOT`. The monitor writes owner-only state, so its service UID and the container `PUID` must be the same numeric UID (or have equivalent explicit read/traverse access). Missing or unreadable marker state fails open: Subgen processes media normally and never treats registry input as deletion authority.

### ModelEnvelope catalog and OCI identity

The three base Compose profiles declare no host evidence binds. That is the
ordinary public missing-evidence path: automatic mode logs one bounded reason
and uses conservative fallback without requiring any host artifact setup. An
exact-evidence or canonical deployment combines its selected base with
`docker-compose.model-envelopes.yml`, which declares one read-only parent bind
plus both exact read-only evidence leaves:

```bash
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml config --quiet
```

| Artifact | Exact host path | Exact container path |
| --- | --- | --- |
| Parent metadata | `/var/lib/subgen/model-envelopes/v1` | `/opt/subgen/model-envelopes` |
| Catalog | `/var/lib/subgen/model-envelopes/v1/catalog.json` | `/opt/subgen/model-envelopes/catalog.json` |
| Image identity | `/var/lib/subgen/model-envelopes/v1/image-identity.json` | `/opt/subgen/model-envelopes/image-identity.json` |

The host parent and both leaves must be owned by the numeric UID seen as the
Subgen runtime EUID, normally the configured `PUID`. The parent must be mode
`0700`; both leaves must be regular mode-`0600` files. The read-only parent bind
preserves its UID and mode for strict validation inside the container; the two
leaf binds retain the exact artifact paths. The overlay sets
`bind.create_host_path: false`, so missing host artifacts fail configuration or
startup instead of becoming Docker-created directories. Symlinks and any
group/other access are rejected. The identity uses schema
`subgen.model-envelope.identity/v1` and contains only the candidate OCI
configuration digest plus its non-empty ordered rootfs layer diff IDs. The
catalog uses schema
`subgen.model-envelope.catalog/v1`, canonical stdlib JSON, a SHA-256 integrity
record, exact runtime/model/device/policy keys, and positive repeated
incremental-peak measurements and margins.

Loading rejects missing/unknown or duplicate fields, non-ASCII/non-finite
values, bool-as-integer values, bad modes/digests, fewer than three runs,
non-positive measurements, duplicate matches, reordered/empty layers, integrity
failure, or any identity/runtime/policy mismatch. It never uses a nearest entry,
wildcard, mutable tag, or registry manifest as identity. Public automatic mode
logs one bounded reason and uses generic fallback; `CANONICAL_SHARED_CUDA=True`
fails closed and admits nothing until the exact evidence and fresh capacity are
valid.

Only `/subgen/profile_model_envelopes.py` writes a staged catalog. The owner
runs one explicit model per clean isolated process, records at least three cold
cycles, and lets the canonical resource module own admission and paired
incremental-peak arithmetic. The ordinary scanner/worker never invokes the
profiler and neither runtime nor profiler rewrites the identity artifact.
Immediately before every profiler or overlay-enabled automatic runtime start,
use host-side `docker image inspect` to compare both identity components byte-
for-byte with the owner-only file.

## Translation behaviour

The compose templates intentionally set:

```dotenv
TRANSCRIBE_OR_TRANSLATE=translate
SUBTITLE_LANGUAGE_NAME=en
SUBTITLE_LANGUAGE_NAMING_TYPE=ISO_639_1
SHOULD_WHISPER_DETECT_AUDIO_LANGUAGE=True
```

This means:

- English speech becomes English subtitles.
- Foreign speech is translated into English.
- Output is named `.en.srt`.
- Whisper checks the selected audio track even when container metadata claims it is English.

### Model selection

Blank or `auto` enumerates multilingual `large-v3`, `medium`, `small`, `base`,
then `tiny` once before the first model load. Exact matching `ModelEnvelope`
evidence is authoritative. Without it, these CPU capacity bands are fallback
ceilings: below 2 GiB `tiny`; 2 to below 4 GiB `base`; 4 to below 8 GiB
`small`; 8 to below 16 GiB `medium`; and 16 GiB or more `large-v3`. Unknown or
unbounded capacity with no physical fallback cannot promote above `small`.

That means the release acceptance profiles use `small` at 4 GiB and 6 GiB and
may use `medium` at 9 GiB. Fresh host/cgroup use, the host reserve, and the
mandatory cgroup floor must still leave enough admission bytes; a tier alone
does not authorize the load.

CUDA derives an allocatable-VRAM fallback ceiling after reserve: below 2 GiB
`tiny`; 2 to below 3 GiB `base`; 3 to below 7 GiB `small`; 7 to below 12 GiB
`medium`; and 12 GiB or more `large-v3`. Missing or inconsistent telemetry
cannot promote above `small`. Automatic CUDA selection uses the lower of its
RAM and VRAM ceilings unless a strict exact envelope qualifies a higher model.

Any non-empty, non-`auto` recognized model is explicit authority. It receives
a warning when it is above automatic policy but is never silently downgraded,
including during pressure retry. It still needs a known conservative load
budget, fresh admission, and stabilized GPU telemetry when applicable.

Do not use `large-v3-turbo`/`turbo` for translation; OpenAI states Turbo was
not trained for that task. Models ending in `.en` cannot translate foreign
speech. See the [Whisper documentation](https://github.com/openai/whisper#command-line-usage).

### Segmentation and pressure settings

`SEGMENTATION_CHUNK_MINUTES=auto` maps effective capacity to 5 minutes below
4 GiB, 10 minutes from 4 to below 8 GiB, 20 minutes from 8 to below 16 GiB,
and 30 minutes at 16 GiB or more. Explicit integers from 5 through 60 are
accepted. Five seconds of overlap on each available side is internal merge
policy and is not configurable.

Long local files are processed sequentially. Sustained pressure abandons only
the current uncommitted window, safely unloads the fixed model, waits, halves
the working duration to a five-minute floor, and retries the same source cursor.
Three healthy completed windows grow toward the baseline. Structured timestamps
are shifted to source time, assigned by midpoint, clipped to their owning core
at each seam, and rendered once into a same-directory private temporary file
before atomic replacement. Matching text from independent overlap decodes is
kept when Subgen cannot prove whether it is duplicate output or real repeated
speech. Segmentation cannot make resident model weights fit.

`SEGMENTATION_ENABLED=False` opts local files out of windowing only. Model
admission, media validation, markers, pressure preflight/release/wait, and
deletion safety remain active. A pressure-yielded request retries as one whole
file and logs that its source duration cannot shrink. Uploaded `/asr` and
OpenAI-compatible byte-buffer requests are unchanged and never use local-file
segmentation in either mode.

Invalid booleans, chunk values outside 5–60, empty/non-finite/non-positive host
or GPU reserves, or `CANONICAL_SHARED_CUDA=True` without CUDA and a positive
explicit GPU reserve reject startup with a configuration error.

## Work scheduling

### `CONCURRENT_TRANSCRIPTIONS=1`

This is fixed in the public compose templates. Direct API inference and folder jobs share the same model semaphore, so API traffic cannot silently exceed the concurrency limit.

### `WHISPER_THREADS`

Controls compute threads. It should not exceed the Docker CPU limit. Start at four; lower it to two on a busy shared host.

### `SUBGEN_CPU_LIMIT`

Docker CPU ceiling. This is not a reservation: unused CPU remains available to other services.

### `SUBGEN_MEMORY_LIMIT`

Docker memory and memory-plus-swap ceilings are set to the same value, which prevents a large transcription from forcing the host into sustained swap. The public default remains `10g`. The limit is an emergency boundary, not model-fit proof: automatic selection also checks fresh host/cgroup admission, and segmentation bounds only duration-driven allocations. An OOM or load failure is retained as a resource/runtime problem and never makes media deletion-eligible.

## Scanning and skip rules

The templates set:

```dotenv
MONITOR=True
PROCESS_ADDED_MEDIA=True
PROCESS_MEDIA_ON_PLAY=False
SKIP_STARTUP_SCAN=False
SKIP_IF_TARGET_SUBTITLES_EXIST=True
SKIP_IF_EXTERNAL_SUBTITLES_EXIST=True
SKIP_MARKED_FAILED_FILES=true
SKIP_VIDEO_EXTENSIONS=.avi
```

Subgen scans configured folders, watches for new files, works in advance rather than on playback, and avoids duplicating external English subtitles. `SKIP_STARTUP_SCAN=False` is the public default so files added while Subgen was stopped are found before the watcher starts. Set it to `True` only after an intentional backfill when avoiding another full scan is more important than automatic catch-up; the watcher handles new events but does not discover files that arrived while it was offline. When marker skipping is enabled, an exact path plus five-field identity match is rejected before media probing; a replacement at the same path proceeds normally. Remove `.avi` from the skip list only if you have tested those files.

## Plex integration

Plex and Jellyfin server/token settings are blank by default. Leaving them blank disables those integrations; standalone file and folder processing needs no media-server settings.

To enable Plex webhook processing, set your own values, for example:

```dotenv
PLEX_SERVER=http://192.168.1.20:32400
PLEX_TOKEN=replace-with-your-token
SUBGEN_BIND_ADDRESS=192.168.1.50
```

If Plex reports a different media root:

```dotenv
USE_PATH_MAPPING=True
PATH_MAPPING_FROM=/plex/path
PATH_MAPPING_TO=/media
```

Plex webhooks require an active [Plex Pass for the server owner or administrator](https://support.plex.tv/articles/115002267687-webhooks/).

Path mapping is a string root replacement. Test a single item before enabling a large queue.

## HTTP access

### `SUBGEN_BIND_ADDRESS`

- `127.0.0.1`: safest; only the Docker host can connect.
- a private LAN IP: use when Plex/Bazarr runs on another trusted host.
- `0.0.0.0`: avoid unless a firewall and authentication boundary are deliberately configured.

### `SUBGEN_API_KEY`

When non-empty, this protects `/asr`, `/batch`, `/detect-language`, `/v1/audio/transcriptions`, and `/v1/audio/translations` through `X-Subgen-Api-Key`.

Plex, Jellyfin, Emby, and Tautulli webhook routes do not use this header. Keep them on a trusted network.

### `HTTP_TIMEOUT_SECONDS=30`

Bounds outbound Plex/Jellyfin calls so an unavailable media server does not hold a worker forever.

## Model cleanup

`CLEAR_VRAM_ON_COMPLETE=True` lets Subgen unload the model after the queue is idle. `MODEL_CLEANUP_DELAY` prevents repeated unload/reload cycles during a burst. Leave it blank in `.env` to retain the selected profile's default:

- CPU shared host: 60 seconds.
- Conservative GPU: 300 seconds.
- Large library scan on a dedicated GPU host: up to 900 seconds.

With pressure yielding enabled, the canonical model runtime may unload sooner at
a safe boundary. A five-second resident-idle observer applies the same exact-
device pressure and telemetry-loss policy even when no progress callback is
active. Recovery requires fresh admission-qualified samples before reload.

## Failure monitor

These values live in `monitor.env`:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES=5242880
```

The monitor records exact case-preserving host/container paths plus a five-field
device/inode/size/modification/change-time identity. It writes or refreshes the
schema-v1 generation marker before any optional unlink. Duplicate basenames in
different directories remain separate, and a replacement makes prior evidence
stale. Invalid registry input is preserved for diagnosis rather than
overwritten.

The runtime's media classifier is the only source of deletion type evidence.
Bounded FFprobe and isolated PyAV each return `audio_present`, `no_audio`,
`invalid_format`, or `indeterminate`. Two `invalid_format` results are required
for `invalid_media`; otherwise any `audio_present` wins, then any `no_audio`,
and all remaining combinations are indeterminate. The source identity is
checked before, between, and after probes and again by the monitor.

Only a canonical `media_validation_failed` event with exact dual-validator
evidence, `failure_class=invalid_media`, the unchanged current identity,
enabled marker/deletion policy, threshold satisfaction, and a durably re-read
processing-error marker can reach monitor unlink. Silent/no-audio media,
timeouts, permission or I/O failures, validator crashes, disagreement, generic
worker/file errors, inference or resource failures, OOM, pressure yield,
SIGSEGV, log-regex matches, legacy untyped intents, and stale replacements are
retained.

`AUTO_DELETE_INVALID_MEDIA` is the canonical switch. The deprecated
`AUTO_DELETE_FAILED_FILES=true` alias is accepted through 0.5.x, warns once,
and enables only the same invalid-media route. Either true value can enable it
during migration, so both must be false to disable deletion.

### Repair and legacy migration

Repair is report/evidence-only in v0.5.0. `SUBGEN_REPAIR_ACTION=delete` remains
accepted but warns and behaves exactly like `report`; it never deletes media or
legacy empty subtitle markers. Old crash or untyped pending delete intents are
preserved as `policy_blocked` evidence rather than resumed. Malformed private
state is preserved byte-for-byte where possible and fails closed.

The two-minute timer deduplicates unchanged candidate evidence. Failed audit
writes enter a bounded FIFO and retain their original timestamps. The event log
rotates under a process lock to one owner-only `.1` backup. The repairer refuses
symlinks, hardlinks, non-regular or foreign-owned state leaves and never uses a
media path for rotation. `SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES` must be at least
256 bytes; the safe default is 5 MiB.

### Optional invalid-media monitor deletion

After a disposable report-only smoke, opt in with:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=true
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

The monitor's exact unlink owner uses Linux descriptor-relative no-follow
traversal, a same-filesystem private quarantine, a durable operation token, and
the persisted five-field identity. A leaf swap is restored or preserved rather
than adopted. Unsupported platforms fail closed. No recursive directory delete
or empty subtitle marker is created.

There is no Sonarr/Radarr API call. Replacement, if desired, remains owned by
the operator's existing library automation. Never validate deletion by
corrupting or removing real media.

`SUBGEN_STATE_DIR` must be a real local mode-`0700` directory owned by the
service account, outside `MEDIA_ROOT` and not on an untrusted network
filesystem. State, lock, summary, heartbeat, marker, and audit files are forced
to `0600`. The container reads the same directory through a read-only mount, so
its numeric `PUID` must be able to traverse it and read the marker registry.

### Email alerts

SMTP and relay settings are optional. Blank values leave local event reporting enabled without sending email. Never commit `monitor.env`.

## Source override

The source CPU compose file mounts:

```yaml
- ./subgen_override.py:/subgen/subgen.py:ro
- ./language_code.py:/subgen/language_code.py:ro
- ./subgen_failure_markers.py:/subgen/subgen_failure_markers.py:ro
- ./subgen_ops_safety.py:/subgen/subgen_ops_safety.py:ro
- ./profile_model_envelopes.py:/subgen/profile_model_envelopes.py:ro
- ./subgen_core:/subgen/subgen_core:ro
- ${SUBGEN_STATE_DIR:-./monitor}:/opt/subgen/monitor:ro
```

These mounts keep the executable facade, language helper, marker contract,
owner-operated profiler, and canonical package from the same checkout. The
packaged CPU and GPU profiles include those Python components and therefore
need no source-code mounts. All three base profiles retain the same read-only
marker state and deliberately omit ModelEnvelope evidence; add the supplied
overlay when strict parent-and-leaf evidence binds are required.
