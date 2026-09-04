# Configuration guide

Install `.env.example` as an owner-only `.env`, run
`python3 configure_capacity.py`, and change the remaining values there. Every
Compose profile extends the literal `.subgen-capacity.yml` fragment; the
configurator must verify the selected Linux Docker engine before writing that
finite boundary.

## Conservative defaults

| Setting | Default | Reason |
| --- | --- | --- |
| `WHISPER_MODEL` | `auto` | Choose the highest admitted multilingual model once; explicit recognized models remain fixed. |
| `SEGMENTATION_ENABLED` | `True` | Bound long local-file work to sequential windows with adaptive retry. |
| `SEGMENTATION_CHUNK_MINUTES` | `auto` | Use the 5/10/20/30-minute capacity tier; explicit values must be integers from 5 through 60. |
| `MEMORY_PRESSURE_YIELD` | `True` | Release and retry an uncommitted window when sustained pressure needs memory. |
| `MEMORY_PRESSURE_RESERVE_GIB` | `auto` | Keep an automatic host reserve while retaining the mandatory cgroup floor. |
| `PRIORITY_PRESSURE_FILE` | blank | Optional required signal from a trusted shared-host producer; blank disables this integration. |
| `GPU_MEMORY_RESERVE_GIB` | `auto` | Public GPU floor; canonical shared CUDA requires a positive audited value. |
| `MODEL_ENVELOPE_CATALOG` | `/opt/subgen/model-envelopes/catalog.json` | Read-only exact-runtime measurement catalog; public fallback applies when evidence is unusable. |
| `MODEL_ENVELOPE_IDENTITY` | `/opt/subgen/model-envelopes/image-identity.json` | Read-only OCI config digest plus ordered rootfs layer identity. |
| `CONCURRENT_TRANSCRIPTIONS` | `1` | Predictable RAM/VRAM use and failure attribution. Fixed in the compose templates. |
| `WHISPER_THREADS` | `4` | Leaves CPU capacity for Plex and the host. |
| `SUBGEN_CPU_LIMIT` | `4.0` | Prevents the container consuming the whole server. |
| `.subgen-capacity.yml` | generated integer MiB | Literal hard/no-extra-swap ceiling derived from verified stable Docker capacity; automatic generation stops at 24 GiB. |
| `COMPUTE_TYPE` | CPU `int8`; GPU `float16` | Conservative compute type for each device. |
| `MODEL_CLEANUP_DELAY` | CPU `60`; GPU `300` seconds | Avoids constant reloads while eventually releasing model memory. |
| `SKIP_STARTUP_SCAN` | `False` | Performs a catch-up scan before watching for new files. |
| `MQTT_INVENTORY_ENABLED` | `False` | Keeps optional Home Assistant inventory reporting disabled until a broker is configured. |
| `MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS` | `21600` | Opens the decode barrier after six hours if a startup inventory cannot finish; transcription continues with the scan marked incomplete. |
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

### Shared-host priority signal

`PRIORITY_PRESSURE_FILE=` is intentionally blank in the public environment and
all three base Compose profiles. Blank means disabled; it is not interpreted as
an observed clear state. A non-empty value must be an absolute canonical
container path and requires `MEMORY_PRESSURE_YIELD=True`. Once configured, a
missing, stale, malformed, unsafe, wrong-boot, replayed, or unreadable signal is
fail-closed and keeps admission shut until the controller observes the required
distinct clear generations.

The supplied host producer is deliberately separate from Subgen and from the
failure/deletion monitor. Its uncommitted `priority-monitor.env` contains:

```dotenv
FRIGATE_PRIORITY_SIGNAL_FILE=/run/subgen-priority/pressure.json
FRIGATE_PRIORITY_ORIGIN=http://127.0.0.1:5000
OLLAMA_PRIORITY_ORIGIN=http://127.0.0.1:11434
FRIGATE_PRIORITY_POLICY_FILE=/var/lib/subgen-priority/private/frigate-priority-policy.json
FRIGATE_CONFIG_FILE=/etc/frigate/config.yml
FRIGATE_PRIORITY_POLICY_SHA256=replace-with-exact-lowercase-sha256
```

Create the policy from a private draft with exactly these twelve fields.
`schema=1`, `detection_fps_limit=80.0`, and `source_max_age_seconds=30` are
fixed v0.5 schema constants, not tunable examples. The identifiers, hashes,
expected camera FPS, driver version, and GPU index below are illustrative and
must be replaced with values from the reviewed live host:

```json
{
  "schema": 1,
  "frigate_version": "0.17.2",
  "detection_fps_limit": 80.0,
  "source_max_age_seconds": 30,
  "cameras": {"camera_a": 8.0},
  "detectors": ["detector_a"],
  "required_embedding_speeds": ["embedding_speed_a"],
  "conditional_embedding_pairs": [["embedding_activity_b", "embedding_speed_b"]],
  "frigate_config_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "gpu_uuid": "GPU-123e4567-e89b-42d3-a456-426614174000",
  "nvidia_driver_version": "replace-with-exact-driver",
  "gpu_index": 0
}
```

The final file is canonical ASCII JSON with sorted keys, compact separators,
and exactly one newline. JSON integers and floats are intentionally distinct:
`schema`, source age, and GPU index are integers; `80.0` and every expected
camera FPS are floats. Identifier arrays and each conditional pair are sorted
and unique. Keep both the draft and canonical policy outside the Git checkout
and Docker build context. Prepare the draft as an owner-only file beneath
`/var/lib/subgen-priority/private`, then generate the final bytes there and
record their hash:

```bash
sudo install -d -m 700 -o mediauser -g media \
  /var/lib/subgen-priority/private
sudo install -m 600 -o mediauser -g media \
  /path/outside-the-checkout/private-policy-draft.json \
  /var/lib/subgen-priority/private/private-policy-draft.json
sudo -u mediauser python3 - \
  /var/lib/subgen-priority/private/private-policy-draft.json \
  /var/lib/subgen-priority/private/frigate-priority-policy.json <<'PY'
import json
from pathlib import Path
import sys

def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value

draft = json.loads(
    Path(sys.argv[1]).read_text(encoding="utf-8"),
    object_pairs_hook=reject_duplicates,
    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
)
payload = json.dumps(
    draft,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=True,
    allow_nan=False,
).encode("ascii") + b"\n"
Path(sys.argv[2]).write_bytes(payload)
PY
sudo chmod 600 /var/lib/subgen-priority/private/frigate-priority-policy.json
sudo -u mediauser sha256sum \
  /var/lib/subgen-priority/private/frigate-priority-policy.json
```

The producer performs the full schema, range, topology, identity, canonical-byte,
and configured-hash checks; canonicalising an invalid draft does not make it a
valid policy.

Only plain HTTP literal `127.0.0.1` origins with explicit ports are accepted.
The producer reads Frigate stats and its official plain-text version endpoint,
Ollama's currently loaded model list, and the policy-bound NVIDIA identity. It
does not use `/api/tags`, GPU utilisation, or memory use as a priority shortcut,
and it never coordinates another service.

The private policy's parent must be outside the checkout, owned by the producer
service account with mode `0700`; the draft and exact canonical policy file
must have the same owner and mode `0600`. Keep its SHA-256 only in
`priority-monitor.env`. Do not publish camera,
detector, embedding, config, GPU, or policy details. The Frigate config is
stream-hashed through a no-follow regular-file descriptor and must match the
hash bound by the policy.

The systemd unit creates and preserves `/run/subgen-priority` as a mode `0700`
directory. The producer writes `pressure.json` as mode `0600` by file-fsync,
atomic replacement, and directory-fsync. Its `User` must own those paths; the
container `PUID` must be that same numeric UID so the read-only bind is
traversable. Start the producer before creating the container, then add the
opt-in overlay:

```bash
docker compose -f docker-compose.gpu.yml -f docker-compose.priority-pressure.yml config --quiet
docker compose -f docker-compose.gpu.yml -f docker-compose.priority-pressure.yml up -d
```

The overlay mounts the parent directory, not the file, so atomic replacements
remain visible. It sets `bind.create_host_path: false`; an absent host directory
therefore fails rather than becoming an unsafe Docker-created replacement.

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
evidence is authoritative. Without it, the gross CPU ceiling is the highest
model whose fallback load budget and margin fit both the host total after its
automatic reserve and the cgroup limit after its mandatory floor. Unknown or
unbounded capacity with no physical fallback cannot promote above `small`.

For planning, the generated stable-capacity matrix is:

| Stable Docker capacity | Minimum automatic host reserve | Generated cgroup limit | Gross CPU candidate | Automatic core window |
| ---: | ---: | ---: | --- | ---: |
| 4 GiB | 1024 MiB | 3072 MiB | `small` | 5 minutes |
| 6 GiB | 1024 MiB | 5120 MiB | `small` | 10 minutes |
| 9 GiB | 1536 MiB | 7680 MiB | `medium` | 10 minutes |
| 12 GiB | 2048 MiB | 10240 MiB | `medium` | 20 minutes |
| 16 GiB | 2560 MiB | 13824 MiB | `large-v3` | 20 minutes |
| 24 GiB | 3840 MiB | 20736 MiB | `large-v3` | 30 minutes |
| 32 GiB | 5120 MiB | 24576 MiB | `large-v3` | 30 minutes |
| 64 GiB | 9984 MiB | 24576 MiB | `large-v3` | 30 minutes |
| 128 GiB | 19712 MiB | 24576 MiB | `large-v3` | 30 minutes |

Fresh host/cgroup use, the host reserve, and the mandatory cgroup floor must
still leave enough admission bytes; a planning row alone does not authorize a
load. Runtime effective capacity is `min(host total, finite cgroup limit)`, so
an oversized cgroup can never inflate model or chunk policy above the host.

The host reserve is outside the generated Subgen limit. Inside that single
cgroup budget, the interpreter, runtime, resident model, decoded audio, and one
active window are all charged together. No second pool is reserved for a
chunk: fresh cgroup use is deducted during model admission, and live headroom
governs later pressure handling. The listed window is an initial duration
target, not a byte allowance.
For hosts at and above 32 GiB, the 24 GiB Subgen cap leaves additional memory
outside the container; the table's reserve is the guaranteed minimum rather
than the total RAM available to other work.

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

`SEGMENTATION_CHUNK_MINUTES=auto` maps runtime effective capacity to 5 minutes below
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

The progress log is deliberately explicit about which values are live and which
are planning evidence. `Memory available` is the guest operating system's fresh
available-memory reading, not installed RAM or a hypervisor usage graph.
`Memory reserved for system/priority tasks` is protected from Subgen. `Subgen
memory in use / limit` is the current cgroup charge and finite hard limit.
`Model suitable` is the automatic quality ceiling, while `Model using` is the
selected fixed model and its measured-envelope or conservative admission
requirement, including margin; that requirement is not live process RSS.
`Available for subtitle chunks` is the smaller current host/cgroup working
headroom after protection. It is not a separately allocated chunk pool or a
promise that Subgen will consume all of it. If a required live term is unknown,
the log says `unavailable` rather than presenting a potentially unsafe number.

The first `File split into ... planned chunks` line is an adaptive plan, so its
total may change. Chunk lines distinguish `started` from `finished` and report
whole-file progress. A pressure retry names the same source position and shows
the previous and smaller working duration. `Joining chunks 1–N` uses the exact
completed count; `Chunks joined` and `File finished successfully` are emitted
only after their respective atomic-publication and durable-completion boundaries.

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

### Generated capacity file

Run `python3 configure_capacity.py` after creating owner-only `.env`. It queries
`docker info`, requires a Linux engine with enforceable cgroup memory and
no-extra-swap controls, verifies that `.env` is a regular private file, and
fails without replacing `.subgen-capacity.yml` when capacity cannot be proven.
Its stable capacity `H` is the Docker-engine total, or an explicitly lower VM,
rootless-user-slice, or nested-daemon floor supplied with
`--guaranteed-memory-gib`. Do not supply installed physical RAM when Docker is
entitled to less. Rootless mode additionally requires cgroup v2 with systemd;
nested daemons cannot always be detected and therefore require the explicit
floor.

The generated fragment contains literal equal `mem_limit` and `memswap_limit`
values plus `oom_score_adj: 1000`. Compose imports it with `extends`; an
inherited shell variable therefore cannot replace the capacity boundary.

The automatic reserve is `ceil-to-256MiB(max(1 GiB, 15% of H))`. The generated
limit is the remaining capacity rounded down to 256 MiB and capped at 24 GiB.
An explicit `MEMORY_PRESSURE_RESERVE_GIB` may only raise this protection. The
Compose profiles set memory and memory-plus-swap to the same generated integer-
MiB value and use `oom_score_adj=1000` as a last-resort Linux OOM preference;
this is not a memory reservation and does not authorize Subgen to preempt other
work.

Generic active pressure is sampled once per second. Crossing the host reserve
yields immediately; pressure retry releases the current model/audio allocation
and waits for recovery. The hard limit remains an emergency boundary, not
model-fit proof: automatic selection also checks fresh host/cgroup admission,
and segmentation bounds only duration-driven allocations. An OOM or load
failure is retained as a resource/runtime problem and never makes media
deletion-eligible.

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

Subgen scans configured folders, watches for new files, works in advance rather than on playback, and avoids duplicating external English subtitles. `SKIP_STARTUP_SCAN=False` is the public default so files added while Subgen was stopped are found before the watcher starts. Set it to `True` only after an intentional backfill when avoiding another full scan is more important than automatic catch-up; the watcher handles new events but does not discover files that arrived while it was offline. The setting applies only to automatic startup catch-up: an explicit `/batch` request walks the requested path once, submits discovered files to the normal queue checks, and does not register another watcher. Its response does not wait for transcription to finish. When marker skipping is enabled, an exact path plus five-field identity match is rejected before media probing; a replacement at the same path proceeds normally. Remove `.avi` from the skip list only if you have tested those files.

## Optional MQTT and Home Assistant inventory

`MQTT_INVENTORY_ENABLED=False` leaves this feature completely disabled. When
enabled, Subgen uses Home Assistant MQTT discovery to create **Subgen Items
Left** and **Subgen Scan %** as diagnostic sensors. The MQTT dependency is
packaged in the built image but imported only when this feature is enabled.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MQTT_INVENTORY_ENABLED` | `False` | Enable the inventory scan barrier and MQTT publisher. |
| `MQTT_HOST` | blank | Broker hostname or IP address; required when enabled. |
| `MQTT_PORT` | `1883` | Broker TCP port. |
| `MQTT_USERNAME` | blank | Optional broker username. |
| `MQTT_PASSWORD` | blank | Optional broker password; a password requires a username. |
| `MQTT_CLIENT_ID` | `subgen-inventory` | MQTT client identifier. Make it unique per running Subgen instance. |
| `MQTT_TOPIC_PREFIX` | `subgen` | Root for retained state and availability topics. Make it unique per instance. |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant discovery root; normally shared by every instance. |
| `MQTT_INVENTORY_NODE_ID` | `subgen_inventory` | Stable Home Assistant device/unique-ID namespace. Make it unique per instance. |
| `MQTT_INVENTORY_LIBRARY_NAMES` | blank | Optional pipe-separated display labels in `TRANSCRIBE_FOLDERS` order, for example `Movies|TV`. These values are published in MQTT and Home Assistant attributes, so never put private paths or media titles in them. Blank uses privacy-safe `Library 1`, `Library 2` labels. |
| `MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS` | `21600` | Startup scan watchdog, from 60 through 86,400 seconds. |

The enabled startup sequence deliberately differs from the ordinary catch-up
scan:

1. Subgen starts the configured directory watcher so imports arriving during a
   long inventory are not lost.
2. It counts supported media candidates across every configured folder or file
   entry, without keeping the whole library listing in RAM.
3. It inspects that complete inventory for existing target subtitles and builds
   the total still needing work.
4. The worker begins decoding after the scan finishes successfully. If the
   bounded watchdog expires first, it releases the barrier and records the
   inventory as incomplete so subtitle work can continue.

This full pass is required for a trustworthy denominator, so enabling MQTT
inventory overrides `SKIP_STARTUP_SCAN=True`. Subgen never derives a published
label from a configured path. Folder entries use `Library 1`, `Library 2`, and
so on unless the operator supplies `MQTT_INVENTORY_LIBRARY_NAMES`; direct-file
entries receive generic labels such as `Direct file 1`. Duplicate labels are
disambiguated.
Operator-supplied library names are published as labels in the retained MQTT
state and Home Assistant sensor attributes. They are not private local aliases:
do not use paths, film or show titles, or other sensitive text. Keeping
`MQTT_INVENTORY_LIBRARY_NAMES` blank preserves the generic, privacy-safe labels.
The retained JSON state contains only aggregate `scanned`, `total`, and
`items_left` values per label, plus aggregate `scan_complete` and `scan_errors`
diagnostics. It never publishes a media filename, full path, title, subtitle
text, or in-process path hash.

The two sensors share the same retained JSON state. **Subgen Items Left** falls
after a queued item publishes its subtitle successfully. **Subgen Scan %**
reports inspected supported-media candidates divided by the counted total. A
100% value means every counted candidate was visited; `scan_complete=true` and
`scan_errors=0` together prove that the pass completed cleanly. Home Assistant
discovery, state, and `online`/`offline` availability are retained. State is
refreshed on an exact 60-second cadence and important changes such as scan
start, scan finish, and successful item completion are sent immediately.
Move events are followed at their destination, and deleting or moving away a
pending item removes it from `items_left`.

MQTT is a diagnostic side channel, not a transcription dependency. Invalid
optional configuration disables the publisher, and a broker outage does not
stop subtitle work. If the inventory cannot finish within
`MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS`, Subgen marks the scan incomplete,
increments `scan_errors`, opens the barrier, and continues decoding. This is a
fail-open safety path: the sensor attributes show that the inventory is not a
proven complete count rather than holding the subtitle queue forever.

The broker credentials belong only in the owner-only `.env` file. Do not place
them in Compose YAML, logs, screenshots, or issue reports. When two instances
share a broker, using unique client, topic, and node identifiers prevents MQTT
disconnect loops, retained-state collisions, and Home Assistant entity
collisions.

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
