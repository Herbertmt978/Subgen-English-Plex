# Installation guide

The [README quick start](../README.md#quick-start) is enough for a normal installation. This page covers the decisions and checks that are useful when deploying on a real media server.

## 1. Choose a deployment

| Deployment | Command | Use it when |
| --- | --- | --- |
| Packaged CPU | `docker compose -f docker-compose.ghcr.yml up -d` | Recommended first installation. The published GHCR image contains the facade, helper, and `subgen_core` package. |
| Source CPU | `docker compose up -d --build` | You want the checked-out facade, helper, and `subgen_core` package mounted read-only. Rebuild so packaged dependencies match the checkout. |
| Packaged NVIDIA | `docker compose -f docker-compose.gpu.yml up -d` | The same packaged runtime with NVIDIA GPU access through NVIDIA Container Toolkit. |

For Intel/AMD graphics or explicitly selected mixed GPUs, follow the
[device setup guide](DEVICE_BUNDLE.md). It covers the optional Linux Vulkan
image, Windows-native runtime and verified model preparation. Installing a
Vulkan driver alone does not add that backend to the ordinary image.

The public default is `WHISPER_MODEL=auto`, one transcription, adaptive local-
file segmentation, pressure yield, and a generated finite hard/no-extra-swap
memory limit. CPU `int8` and NVIDIA `float16` remain profile-specific. Automatic selection uses
exact ModelEnvelope evidence when it matches the immutable runtime; otherwise
the public profiles use conservative fallback ceilings. Read the
[hardware guide](../README.md#model-and-hardware-guide) before fixing a model.

The packaged profiles need no source-code mounts and default to the
release-tagged `v0.5.0` image. Set `SUBGEN_IMAGE` only when deliberately testing
another release tag or immutable digest. The source profile mounts every Python
component, including the isolated profiler and `subgen_core`, explicitly and
read-only so a checkout updates the complete modular runtime together.

## 2. Clone and configure

```bash
git clone https://github.com/Herbertmt978/Subgen-English-Plex.git
cd Subgen-English-Plex
install -m 600 .env.example .env
python3 configure_capacity.py
mkdir -p ./models
install -d -m 700 ./monitor
```

The configurator refuses to write a limit unless `docker info` verifies
enforceable Linux cgroup memory and no-extra-swap controls. On a ballooned VM,
rootless user slice, or nested daemon, bind policy to the verified guaranteed
floor instead of a temporary or parent allocation, for example:

```bash
python3 configure_capacity.py --guaranteed-memory-gib 20
```

It writes `.subgen-capacity.yml` with one literal integer-MiB value for both
memory and memory-plus-swap. All three Compose profiles extend that generated
fragment, so configuration fails until the step succeeds and a shell variable
cannot silently replace the boundary. Rootless Docker is accepted only with
cgroup v2, the systemd cgroup driver, and an explicit verified floor. Nested
daemons cannot always be detected, so their floor must be supplied explicitly.

A machine sold or configured with 4 GiB may report slightly less usable RAM
to Linux. Setup uses that measured value, not the label on the machine. It
requires at least 2,816 MiB for Subgen after preserving the host reserve. For
example, about 3.82 GiB of usable RAM produces a 2,816 MiB limit and keeps at
least 1 GiB outside it. This only permits setup: fresh runtime checks still
decide whether a model and its next chunk fit alongside other workloads.

The configurator also checks that `.env` is a regular owner-only file on
POSIX. Keep that file out of cloud-synchronised and shared directories because
it can contain Plex and API credentials.

At minimum, edit these values in `.env`:

```dotenv
MEDIA_ROOT=/srv/media
SUBGEN_MODEL_PATH=./models
SUBGEN_STATE_DIR=./monitor
TRANSCRIBE_FOLDERS=/media/Movies|/media/TV
SKIP_MARKED_FAILED_FILES=true
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
GPU_MEMORY_RESERVE_GIB=auto
PUID=1000
PGID=1000
```

`MEDIA_ROOT` is a host path. It is mounted at `/media` inside the container, so `TRANSCRIBE_FOLDERS` must use paths beneath `/media`. The value may be one media file, one folder, or a pipe-separated mixture of files and folders; Plex and *Arr services are not required.

For example, a one-file startup run can use:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies/Example.mkv
```

For example:

| Host folder | Container folder |
| --- | --- |
| `/srv/media/Movies` | `/media/Movies` |
| `/srv/media/TV` | `/media/TV` |

Only mount a root that contains libraries Subgen is allowed to read and modify.

### ModelEnvelope artifacts

The three base Compose profiles deliberately declare no host ModelEnvelope
binds. A normal public install therefore needs no evidence directory or dummy
files: missing evidence produces one bounded reason and conservative fallback
selection. Exact-evidence and canonical deployments opt in by combining their
chosen base with the supplied `docker-compose.model-envelopes.yml` overlay. The
overlay declares these exact read-only bind mounts:

| Artifact | Exact owner-only host path | Container path |
| --- | --- | --- |
| Parent metadata | `/var/lib/subgen/model-envelopes/v1` | `/opt/subgen/model-envelopes` |
| Catalog | `/var/lib/subgen/model-envelopes/v1/catalog.json` | `/opt/subgen/model-envelopes/catalog.json` |
| OCI identity | `/var/lib/subgen/model-envelopes/v1/image-identity.json` | `/opt/subgen/model-envelopes/image-identity.json` |

Before using the overlay, install real artifacts produced for that exact
immutable image. Do not create dummy evidence or copy artifacts from another
image. The host parent and both files must be owned by the numeric UID seen as
the Subgen runtime EUID, normally the configured `PUID`; keep the parent mode
`0700` and both regular leaves mode `0600`. The read-only parent bind preserves
that UID and directory mode for strict container-side validation, while the
leaf binds retain the exact artifact paths. Its long bind entries set
`create_host_path: false`, so a missing host parent or leaf prevents startup
instead of becoming a Docker-created directory. Symlinks and group/other access
are rejected.

The identity schema is `subgen.model-envelope.identity/v1`; the catalog schema
is `subgen.model-envelope.catalog/v1`. Before each profiler or overlay-enabled
automatic runtime start, compare the candidate image's OCI configuration digest
and ordered rootfs layer diff IDs from `docker image inspect` byte-for-byte with
the identity file. A tag or registry manifest digest alone is not runtime
identity. Missing, unsafe, malformed, integrity-invalid, or non-matching
evidence reports a bounded reason and uses conservative public fallback.
Canonical shared CUDA instead fails closed.

Only the deployment owner should run
`/subgen/profile_model_envelopes.py`. Run one explicit model per clean isolated
process for at least three cold cycles, write a distinct staged catalog, review
it, and then install it atomically; the profiler never rewrites the identity
file or live catalog. Use `--help` inside the exact packaged image for the
required explicit identity, media, model, reserve, margin, runtime, and policy
arguments. A canonical shared-CUDA deployment must set
`CANONICAL_SHARED_CUDA=True` and a positive audited
`GPU_MEMORY_RESERVE_GIB`; `auto` is not accepted there.

For the later Linux exact-evidence gate, combine the overlay with the selected
base. This packaged-CPU example validates the merge, starts it, verifies the
container-visible owner and modes, and—after one disposable automatic-model job
has reached model selection—requires a successful exact catalog and identity
load:

```bash
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml config --quiet
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml up -d
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml exec -T subgen sh -ec '
expected_uid="${PUID:-}"
case "$expected_uid" in
  ""|*[!0-9]*) echo "PUID must be a configured numeric UID" >&2; exit 1 ;;
esac
test "$(stat -c %u /opt/subgen/model-envelopes)" -eq "$expected_uid"
test "$(stat -c %a /opt/subgen/model-envelopes)" = 700
for leaf in catalog.json image-identity.json; do
  test "$(stat -c %u "/opt/subgen/model-envelopes/$leaf")" -eq "$expected_uid"
  test "$(stat -c %a "/opt/subgen/model-envelopes/$leaf")" = 600
done
'
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml exec -T subgen python -c 'import json, urllib.request; status=json.load(urllib.request.urlopen("http://127.0.0.1:9000/status")); resource=status["resource_management"]; assert resource["envelope_disposition"] == "exact_match" and resource["decision_provenance"] == "envelope", resource'
```

The metadata check deliberately reads the configured numeric `PUID`; a
`docker compose exec` process can run under a different UID, so `id -u` there
does not identify the long-running Subgen process. The subsequent `/status`
`exact_match` result is the proof that Subgen accepted the catalog and identity
under its actual runtime EUID.

Use the same overlay with `docker-compose.yml` or `docker-compose.gpu.yml` when
that is the selected base. Ordinary fallback smoke tests use only the base file.

## 3. Check permissions

Find the user and group that own the media:

```bash
id
stat -c '%u:%g %n' /srv/media
```

Put the appropriate numeric IDs in `.env`. The container needs read access to media, write access to the directory where each `.srt` will be created, and read/traverse access to `SUBGEN_STATE_DIR`. If the host monitor is enabled, run it with the same numeric UID as `PUID` (or grant equivalent explicit access) because the marker registry is owner-only and mounted read-only into the container.

## 4. Validate before starting

```bash
docker compose -f docker-compose.ghcr.yml config --quiet
```

For NVIDIA:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
docker compose -f docker-compose.gpu.yml config --quiet
```

The CUDA test image version is only a connectivity check; Subgen supplies its own runtime image.

## 5. Start and verify

Packaged CPU:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Source CPU:

```bash
docker compose up -d --build
```

NVIDIA:

```bash
docker compose -f docker-compose.gpu.yml up -d
```

Then check:

```bash
docker ps --filter name=subgen
curl --fail http://127.0.0.1:9000/status
docker logs --tail 100 subgen
```

The status response reports the stable overlaid runtime version `2026.07.1`.
That is intentionally distinct from the project, image, and release version
`0.5.0`. The first job downloads the selected model into
`SUBGEN_MODEL_PATH`; do not treat that initial delay as a hang unless the logs
stop changing or show an error.

For a long local file, the expected human-facing sequence is:

```text
Starting file: Movie Name.mkv
RAM control for Movie Name.mkv:
  Memory available: 10.0 GiB
  Memory reserved for system/priority tasks: 2.0 GiB
  Subgen memory in use / limit: 6.0 GiB / 10.0 GiB
  Model suitable: medium
  Model using: medium — 5.5 GiB RAM requirement (conservative estimate plus safety margin; not live RSS)
  Available for subtitle chunks: 3.0 GiB working headroom
File split into 3 planned chunks: Movie Name.mkv (adaptive sizing may change the final count)
Chunk 1/3 started — 0% of file complete
Chunk 1/3 finished — 33% of file complete
...
Joining chunks 1–3
Chunks joined
File finished successfully: Movie Name.mkv
```

The planned count may change as Subgen shrinks or regrows its working window.
The join count is final. A run is not complete unless `Joining chunks`, `Chunks
joined`, and `File finished successfully` appear in that order.

### Optional Home Assistant MQTT inventory

This integration is off by default. Use it only when Home Assistant's MQTT
integration and an MQTT broker are already available. No manual Home Assistant
sensor YAML is needed: Subgen publishes retained discovery messages for
**Subgen Items Left** and **Subgen Scan %**.

Add the broker settings to the owner-only `.env` file:

```dotenv
MQTT_INVENTORY_ENABLED=True
MQTT_HOST=192.168.1.30
MQTT_PORT=1883
MQTT_USERNAME=subgen
MQTT_PASSWORD=replace-with-a-secret
MQTT_CLIENT_ID=subgen-inventory
MQTT_TOPIC_PREFIX=subgen
MQTT_DISCOVERY_PREFIX=homeassistant
MQTT_INVENTORY_NODE_ID=subgen_inventory
MQTT_INVENTORY_LIBRARY_NAMES=Movies|TV
MQTT_INVENTORY_SCAN_TIMEOUT_SECONDS=21600
```

The six-hour scan watchdog may be set from 60 to 86,400 seconds. Do not shorten
it below the time needed to inspect the whole library under normal storage load.
If it expires, Subgen records an incomplete scan, opens the worker barrier, and
continues transcription rather than hanging the queue.

Recreate the chosen deployment. A source checkout must be rebuilt so the MQTT
client dependency is present:

```bash
docker compose up -d --build
```

On startup, confirm the logs show that the watcher is active, every configured
entry is inventoried, and the inventory finishes before the first decode. New
imports arriving during the scan are still queued by the watcher. In Home
Assistant, confirm both Subgen sensors appear under one Subgen device and that:

- **Subgen Scan %** reaches 100 after every counted candidate has been visited;
  `scan_complete=true` and `scan_errors=0` together confirm a clean pass.
- **Subgen Items Left** matches the aggregate of the per-library `items_left`
  attributes and falls after a subtitle is published successfully.
- The state is refreshed every 60 seconds, while scan boundaries and successful
  completions appear immediately.
- Attributes contain aggregate library labels and counts only. Blank
  `MQTT_INVENTORY_LIBRARY_NAMES` produces `Library 1`, `Library 2` labels;
  direct-file entries use labels such as `Direct file 1`. No label is derived
  from a path, and no filename or full path is exposed.

Treat any custom `MQTT_INVENTORY_LIBRARY_NAMES` values as published data: they
appear in retained MQTT state and Home Assistant sensor attributes. Do not use
private paths, film or show titles, or other sensitive text. Leaving the value
blank keeps the safe generic labels.

Discovery, state, and `online`/`offline` availability are retained, so the
sensors survive broker or Home Assistant restarts. A broker outage is
non-blocking and appears in Subgen's logs without failing transcription.

If multiple Subgen instances use the same broker, assign a distinct
`MQTT_CLIENT_ID`, `MQTT_TOPIC_PREFIX`, and `MQTT_INVENTORY_NODE_ID` to each one.
They can share `MQTT_DISCOVERY_PREFIX=homeassistant`. Never paste broker
credentials into logs or an issue report.

For a disposable smoke, point `MEDIA_ROOT` at a temporary directory containing
one short supported file, set `TRANSCRIBE_FOLDERS=/media`, and keep deletion
off. Confirm one final non-empty `.srt` is atomically published, then replace
the file at the same path and confirm generation-scoped marker state does not
block it. Long local files are processed one capacity-derived 5-to-30-minute
window at a time and retry toward five minutes under pressure; segmentation
does not make model weights fit, so model admission must succeed separately.

## 6. Optional Plex webhook

Plex and Jellyfin server/token settings are blank by default. Leaving them blank disables those integrations; standalone file and folder processing needs no media-server settings.

Plex webhooks require an active [Plex Pass for the server owner or administrator](https://support.plex.tv/articles/115002267687-webhooks/).

For webhook-driven `library.new` or `media.play` events, set your own values, for example:

```dotenv
SUBGEN_BIND_ADDRESS=192.168.1.50
PLEX_SERVER=http://192.168.1.20:32400
PLEX_TOKEN=replace-with-your-token
```

Configure the Plex webhook URL as:

```text
http://192.168.1.50:9000/plex
```

Keep this on a trusted LAN. The Plex route is intentionally compatible with Plex's webhook request and does not use `SUBGEN_API_KEY`.

### Path mapping

Plex returns the path it knows for a media item. If that path is not the same path Subgen sees inside the container, configure a root replacement:

```dotenv
USE_PATH_MAPPING=True
PATH_MAPPING_FROM=/path/reported/by/plex
PATH_MAPPING_TO=/media
```

Check one real item before scanning an entire library.

## 7. Optional API key

The service binds to loopback by default. If a trusted remote tool needs `/asr`, `/batch`, `/detect-language`, or the OpenAI-compatible audio endpoints, generate a key:

```bash
openssl rand -hex 32
```

Store it only in `.env`:

```dotenv
SUBGEN_API_KEY=generated-value
```

Clients send it as `X-Subgen-Api-Key`. Do not publish the key in an issue or compose file.

## 8. Optional failure monitor

The monitor is conservative by default:
`AUTO_MARK_FAILED_FILES=true`, `AUTO_MARK_MIN_FAILURES=1`,
`AUTO_DELETE_INVALID_MEDIA=false`, and
`AUTO_DELETE_MIN_FAILURES=1`. `AUTO_DELETE_FAILED_FILES=false` remains only as
a deprecated 0.5.x compatibility alias. Repair is always report/evidence-only,
including when legacy configuration requests `SUBGEN_REPAIR_ACTION=delete`.

The host helpers require Python 3.10+, Docker CLI/socket access, an existing service account, media traverse/read permissions, and write access to `SUBGEN_STATE_DIR`. Deletion additionally requires permission to remove the exact media entry.

```bash
cp monitor.env.example monitor.env
sudo install -d -m 700 -o mediauser -g media /opt/subgen/monitor
```

The supplied units assume:

- repository: `/opt/subgen`
- service user: `mediauser`
- service group: `media`

If those values do not match your host, edit `User` and `Group` and replace every `/opt/subgen` occurrence in `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` before copying the units.

The state directory must be local, owned by the service user, and not group/world writable. The monitor refuses a symlink state directory. Point the Compose `SUBGEN_STATE_DIR` at this same directory and ensure the container `PUID` matches the monitor service's numeric UID so Subgen can read the owner-only marker registry. Keep the repository together: the monitor imports both `subgen_ops_safety.py` and `subgen_failure_markers.py` from `/opt/subgen`.

```bash
sudo cp systemd/subgen-monitor.service /etc/systemd/system/
sudo cp systemd/subgen-repair.service systemd/subgen-repair.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service subgen-repair.timer
```

Check:

```bash
systemctl status subgen-monitor.service
systemctl status subgen-repair.timer
cat /opt/subgen/monitor/subgen_failed_files.txt
```

Do not enable monitor deletion until reports show correct exact paths and a
disposable smoke proves the classification. See
[optional invalid-media monitor deletion](./CONFIGURATION.md#optional-invalid-media-monitor-deletion).
Only unchanged current media for which both bounded typed FFprobe and isolated
PyAV return conclusive `invalid_format` may be removed. Silent,
indeterminate, timeout/permission, validator crash, disappearance,
inference/resource/OOM/pressure/SIGSEGV, generic/log-regex, legacy-intent, and
stale-replacement cases remain. The monitor is the sole automatic deletion
owner; repair never deletes media or empty subtitle markers.

When upgrading existing monitor state, start the monitor once with deletion
off and let it policy-block and preserve old untyped/path-only intents while it
fingerprints current file generations. A replacement at the same path gets a
new identity and is not authorized by stale evidence.

## 9. Optional shared-host priority producer

Skip this section for an ordinary installation and keep
`PRIORITY_PRESSURE_FILE=` blank. Use it only when Subgen shares an NVIDIA device
with a reviewed Frigate workload that must always win. Ollama can be added as a
second higher-priority source by explicitly setting `OLLAMA_PRIORITY_ORIGIN`.
The configured integration is fail-closed: stopping the producer, losing a
required or enabled probe, or making the signal unsafe causes Subgen to unload
or wait rather than compete.

The supplied unit assumes the same `/opt/subgen`, `mediauser`, and `media` values
as the other helpers. Its `User` must have the same numeric UID as the container
`PUID`. Edit the unit before installing it if your account or checkout differs.

Prepare the private files without committing their contents:

```bash
cp priority-monitor.env.example priority-monitor.env
sudo install -d -m 700 -o mediauser -g media /var/lib/subgen-priority/private
sudo install -m 600 -o mediauser -g media \
  /path/outside-the-checkout/frigate-priority-policy.json \
  /var/lib/subgen-priority/private/frigate-priority-policy.json
sudo install -m 600 -o mediauser -g media priority-monitor.env /opt/subgen/priority-monitor.env
```

Fill in the exact policy SHA-256 and Frigate config path in the installed
environment. The policy parent must remain mode `0700` and the canonical policy
file mode `0600`; keep the draft and policy outside the Git checkout and Docker
build context. Their expected camera, detector, embedding, Frigate version,
config hash, NVIDIA UUID, driver, and GPU index are private deployment inputs.
See the [shared-host signal configuration](./CONFIGURATION.md#shared-host-priority-signal)
for its exact schema. Do not put those values in GitHub issues or logs.

Leave `OLLAMA_PRIORITY_ORIGIN` commented or blank when Ollama is absent or
intentionally stopped. Set it to the supplied literal-loopback example only
when Ollama shares the GPU; after that, both a loaded model and lost Ollama
telemetry block Subgen until the source recovers.

Install and start only the producer first:

```bash
sudo cp systemd/subgen-priority-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-priority-monitor.service
sudo systemctl status subgen-priority-monitor.service
sudo stat -c '%U %G %a %n' /run/subgen-priority /run/subgen-priority/pressure.json
```

The supplied unit has `Before=docker.service`. Enable it before rebooting so
systemd creates `/run/subgen-priority` before standard Docker restores any
container that uses the priority overlay. Do not make Docker require the
producer to stay healthy: an unavailable producer must leave only Subgen
fail-closed, not hold back Frigate or the whole Docker engine. If this host uses
a differently named or rootless container-engine unit, add equivalent ordering
before enabling automatic container restore.

The directory must be owned by the configured service account with mode `700`;
the file must have mode `600`. A policy or source that has never validated
correctly leaves the file absent, which is a failed preflight rather than a
reason to start the container anyway.

After the producer is healthy, set the container path and add the opt-in parent
bind to the selected base profile:

```dotenv
PRIORITY_PRESSURE_FILE=/run/subgen-priority/pressure.json
```

```bash
docker compose -f docker-compose.gpu.yml -f docker-compose.priority-pressure.yml config --quiet
docker compose -f docker-compose.gpu.yml -f docker-compose.priority-pressure.yml up -d
curl --fail http://127.0.0.1:9000/status
```

The overlay deliberately mounts `/run/subgen-priority`, not `pressure.json`, so
atomic replacements remain visible. It also refuses to create a missing host
path. Preserve the systemd runtime-directory inode across producer restarts;
recreating the directory while the container is running would leave Docker
attached to the old inode.

## Upgrade

Back up `.env`, `monitor.env`, `priority-monitor.env`, the active
Compose/overlay files, monitor state,
model cache, and—when exact evidence is installed—the parent metadata plus both
external ModelEnvelope files and the exact current image identity before
changing anything. If the priority producer is installed, also retain its
private policy, expected SHA-256, installed unit, enabled/active state, and the
exact Frigate config identity. Review the
[v0.4.1 to v0.5.0 migration](./MIGRATION.md#upgrading-from-041-to-050), prepare
the opt-in exact read-only overlay only if applicable, and leave deletion off
through a disposable smoke.

```bash
git fetch --tags --prune origin
git switch --detach v0.5.0

 # Keep the base used by this installation. This example is packaged CPU.
compose_args=(-f docker-compose.ghcr.yml)

 # Keep each overlay that this installation already validated and uses.
 # compose_args+=(-f docker-compose.model-envelopes.yml)
 # compose_args+=(-f docker-compose.priority-pressure.yml)

docker compose "${compose_args[@]}" config --quiet
docker compose "${compose_args[@]}" pull
docker compose "${compose_args[@]}" up -d
curl --fail http://127.0.0.1:9000/status
```

For packaged NVIDIA, use `compose_args=(-f docker-compose.gpu.yml)`; for the
source-bind deployment, use `compose_args=(-f docker-compose.yml)`. Do not
replace the selected base with another profile or omit an active ModelEnvelope
or priority overlay during an upgrade. If the priority overlay is active,
verify its producer is enabled and `/run/subgen-priority/pressure.json` is valid
before recreating Subgen.

For predictable production deployments, use the release tag or an immutable
digest instead of an unreviewed branch and read [CHANGELOG.md](../CHANGELOG.md)
before upgrading. Public rollback restores v0.4.1 with deletion disabled. The
preserved Frigate v0.3.0 Compose/config, cache, state, and OCI identity form a
separate deployment-specific rollback and are not interchangeable with the
public path.

## Stop or uninstall

```bash
docker compose -f docker-compose.ghcr.yml down
sudo systemctl disable --now subgen-monitor.service subgen-repair.timer subgen-priority-monitor.service
```

This does not remove media, generated subtitles, models, monitor state, the
private priority policy, or external ModelEnvelope artifacts. Remove
`./models`, `/opt/subgen/monitor`, `/var/lib/subgen-priority/private`,
`/var/lib/subgen/model-envelopes/v1`, and generated `.srt` files manually only
if you intend to delete them and have retained any required audit evidence.

## Troubleshooting checklist

1. `docker compose ... config --quiet` succeeds.
2. `PUID` and `PGID` can write beside the media file.
3. Every file or folder in `TRANSCRIBE_FOLDERS` uses a container path, not a host path.
4. Plex webhook paths either match the container or have explicit path mapping.
5. `WHISPER_MODEL` is `auto` or one recognized fixed model; Turbo and `.en` checkpoints are unsupported here.
6. Only one transcription is running; an explicit recognized model remains fixed for the whole file and is never silently downgraded.
7. `.subgen-capacity.yml` was generated from this Docker engine or its lower guaranteed VM/rootless/nested floor; fresh host/cgroup headroom still passes admission before a model loads.
8. CUDA checks the configured device's stabilized allocatable free VRAM after reserve; it never treats total VRAM or one idle sample as authority.
9. An ordinary fallback smoke uses only its base Compose file. An exact-evidence Linux smoke uses the supplied overlay and passes the metadata and exact-load gate above.
10. Invalid chunk, host-reserve, or GPU-reserve settings are corrected; startup rejects them rather than guessing.
11. `SUBGEN_STATE_DIR` resolves to the same host directory for the monitor and Compose, and the container `PUID` can read its marker registry.
12. If `PRIORITY_PRESSURE_FILE` is non-empty, the producer is healthy first, its parent/file are mode `0700`/`0600` under the matching UID, and the parent-only overlay renders with `create_host_path: false`.
13. A long-file run reaches `Joining chunks`, `Chunks joined`, and `File finished successfully` in order; missing later lines mean publication or workload completion did not finish.
