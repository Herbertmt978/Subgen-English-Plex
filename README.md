<div align="center">

# Subgen English for Plex

### Missing subtitles should not make a media library unusable.

[![Release](https://img.shields.io/github/v/release/Herbertmt978/Subgen-English-Plex)](https://github.com/Herbertmt978/Subgen-English-Plex/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

[Quick start](#quick-start) · [Standalone use](#standalone-without-plex-or-arr) · [Hardware guide](#model-and-hardware-guide) · [Safety](#safety-and-trust) · [Configuration](./docs/CONFIGURATION.md) · [Install guide](./docs/INSTALL.md) · [Ops migration](./docs/MIGRATION.md)

</div>

---

This project generates English subtitles locally, translates non-English speech into English, and watches media folders for new files. It is a focused, tested deployment of [McCloudS/Subgen](https://github.com/McCloudS/subgen), with stricter translation, queue, monitoring, and recovery behaviour for Plex-style libraries.

The public v0.5 default chooses the highest safe multilingual Whisper model once, processes long local files in bounded sequential windows, and yields model/audio memory when the host is under pressure. It keeps one transcription at a time and the existing 10 GiB container limit.

> [!IMPORTANT]
> Whisper output can contain errors or hallucinated text. Treat generated subtitles as an accessibility aid, not an authoritative transcript for legal, medical, or safety-critical use.

## Safety and trust

Review this before mounting a media library:

| Concern | Public default |
| --- | --- |
| Media access | The container can read the mounted media root and write `.srt` files beside media. Mount only the libraries it needs. |
| Failed-file handling | The first qualifying terminal failure marks and skips only that exact generation. Automatic deletion remains disabled (`AUTO_DELETE_INVALID_MEDIA=false` and deprecated `AUTO_DELETE_FAILED_FILES=false`). When enabled, only a fresh unchanged dual-invalid media classification can be deleted by the monitor; repair is always report/evidence-only. |
| Network exposure | Port 9000 binds to `127.0.0.1`. Set a private LAN address only when another trusted host must connect. |
| API protection | Compute endpoints can require `SUBGEN_API_KEY`. Plex, Jellyfin, Emby, and Tautulli webhook routes remain unauthenticated and should stay on a trusted network. |
| Network calls | Docker pulls the image; the first job downloads the selected model. Configured Plex, Jellyfin, Emby, email, or completion-webhook integrations also make network calls. |
| Telemetry | This fork adds no telemetry. |
| Persistent files | The checkout, `.env`, model cache, generated `.srt` files, and optional monitor state remain until you remove them deliberately. |
| Reversibility | `docker compose -f docker-compose.ghcr.yml down` stops the recommended packaged deployment. Optional host systemd helpers stop separately. Removing the container does not remove media or generated subtitles. |

Before enabling invalid-media deletion, run with both deletion switches false long enough to inspect `monitor/subgen_failed_files.txt` and confirm the paths and typed classifications are correct for your library.

## Quick start

Requirements:

- Linux with Docker Engine and Docker Compose v2
- 64-bit x86 hardware
- enough RAM for the selected fallback or exact envelope; 4 GiB and 6 GiB profiles ceiling at `small`, while 9 GiB can admit `medium` only when fresh headroom also fits
- several gigabytes of free disk space for the image and model cache, with more if you retain multiple models
- NVIDIA Container Toolkit only when using the CUDA compose file

```bash
git clone https://github.com/Herbertmt978/Subgen-English-Plex.git
cd Subgen-English-Plex
cp .env.example .env
mkdir -p ./models ./smoke-test
install -d -m 700 ./monitor
```

For the first run, put one short supported non-AVI file, such as `.mkv` or `.mp4`, in `./smoke-test`. A non-English clip without embedded or external English subtitles verifies the translation path as well as transcription. Use media you are entitled to process.

Edit `.env` and set:

```dotenv
MEDIA_ROOT=./smoke-test
TRANSCRIBE_FOLDERS=/media
PUID=1000
PGID=1000
```

Replace `PUID` and `PGID` with `id -u` and `id -g` for the account that owns the test file and `./monitor`. `TRANSCRIBE_FOLDERS` uses container paths beneath `/media`. The empty state directory is mounted read-only into Subgen; if the optional host monitor writes markers later, it must run under the same numeric UID or otherwise grant that UID read/traverse access.

The three base Compose profiles intentionally do not bind ModelEnvelope
evidence. With the configured container paths absent, automatic selection
reports a bounded missing-evidence reason and uses conservative public fallback
ceilings, so this quick start requires no host artifact setup or dummy files.
Validate the isolated configuration before exposing a library:

```bash
docker compose -f docker-compose.ghcr.yml config --quiet
docker compose -f docker-compose.ghcr.yml up -d
curl --fail --retry 30 --retry-connrefused --retry-delay 2 http://127.0.0.1:9000/status
```

The status endpoint proves HTTP readiness. It reports the stable overlaid
runtime version `2026.07.1`; the project, image, and release version is `0.5.0`.
The first subtitle job also downloads the selected model, so follow the worker
before judging the result:

```bash
docker logs --follow subgen
```

After `WORKER FINISH: [TRANSCRIBE]`, confirm an English sidecar exists:

```bash
find ./smoke-test -type f -name '*.en.srt' -print
```

The exact filename is configurable; the default resembles `Movie Name.subgen.<selected-model>.en.srt`. Once this passes, replace `MEDIA_ROOT` with the smallest common host root for the intended libraries and set container paths such as `TRANSCRIBE_FOLDERS=/media/Movies|/media/TV`.

<details>
<summary><b>Run directly from the checked-out source</b></summary>

Use the source compose file when you want the local executable facade, language helper, and `subgen_core` package bind-mounted read-only into the upstream Subgen image:

```bash
docker compose up -d
```

Use `docker-compose.ghcr.yml` for the simpler packaged install. Packaged profiles default to this release's `v0.5.0` image instead of a moving `latest` tag; `SUBGEN_IMAGE` is available for a deliberate tag or digest override. Both use the same v0.5 resource defaults from `.env`.

</details>

## Standalone without Plex or *Arr

Plex, Jellyfin, Emby, Bazarr, Sonarr, Radarr, and the rest of the *Arr stack are optional. File and folder processing works with every integration setting left blank.

`TRANSCRIBE_FOLDERS` accepts a single container file path:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies/Example.mkv
```

It also accepts a pipe-separated mixture of folders and files:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies|/media/TV/Season-01/Episode-01.mkv
```

Every entry must be a container path beneath the mounted `/media` root. Files are processed directly during the startup scan; folders are walked at startup. `MONITOR` is optional for standalone use: it controls ongoing watching of directory entries, while direct file entries are not registered as watched directories. The supplied Compose profiles enable `MONITOR=True`; a custom runtime can set `MONITOR=False` for startup-only processing.

The Compose profiles expose `SKIP_STARTUP_SCAN` through `.env` and default it to `False`. Set it to `True` only for deliberate watcher-only operation after the existing library has been processed; media added while Subgen is stopped will not be caught up until a later startup scan.

The default `TRANSCRIBE_OR_TRANSLATE=translate` behavior still translates non-English speech into English subtitles. The `/batch` and `/asr` endpoints are also available without a media server, subject to `SUBGEN_API_KEY` when configured.

## See it work

```console
$ docker logs --follow subgen
... WORKER START : [DETECT_LANGUAGE] ...
... Detected language: Spanish
... WORKER START : [TRANSCRIBE] ...
... WORKER FINISH: [TRANSCRIBE] ...
```

For translation mode, the resulting external subtitle is labelled as English. With the default naming settings, for example:

```text
Movie Name.subgen.<selected-model>.en.srt
```

Existing English subtitles are skipped by default.

## Model and hardware guide

### Automatic model and chunk policy

The public profile uses `WHISPER_MODEL=auto`, one transcription, four threads,
and a 10 GiB hard/no-extra-swap container limit. Automatic selection enumerates
`large-v3`, `medium`, `small`, `base`, then `tiny` and admits the highest
candidate whose current host/cgroup and, for CUDA, device headroom covers its
nonzero load budget plus margin and reserve.

These CPU examples are fallback ceilings, not guarantees:

| Constrained capacity example | CPU fallback ceiling | Automatic core window |
| --- | --- | ---: |
| 4 GiB | `small` | 10 minutes |
| 6 GiB | `small` | 10 minutes |
| 9 GiB | `medium`, subject to fresh admission | 20 minutes |

At exactly 4 GiB, the documented `small` acceptance example assumes 1 GiB
current cgroup use and a 512 MiB floor. At 9 GiB, the `medium` example assumes
2 GiB current use and a 0.9 GiB floor. Higher current use or lower host
`MemAvailable` can select a lower candidate or leave Subgen waiting without
marking the file.

Automatic local-file windows are 5, 10, 20, or 30 minutes from capacity. Under
pressure, Subgen retries the same uncommitted source interval and halves toward
a five-minute floor; after three healthy windows it grows toward the original
baseline. Segmentation bounds duration-driven allocations. It cannot make the
selected model's weights fit; model admission must pass separately.

Any recognized explicit `WHISPER_MODEL` remains fixed. Subgen warns when it is
above the automatic ceiling and still requires fresh load admission; it is
never silently downgraded within a file.

### CUDA fallback and exact ModelEnvelope evidence

CUDA first applies the lower of the system-memory ceiling and these generic
allocatable-VRAM ceilings after the separate reserve:

| Allocatable VRAM | Generic CUDA fallback ceiling |
| --- | --- |
| below 2 GiB | `tiny` |
| 2 GiB to below 3 GiB | `base` |
| 3 GiB to below 7 GiB | `small` |
| 7 GiB to below 12 GiB | `medium` |
| 12 GiB or more | `large-v3` |
| unavailable or inconsistent | no promotion above `small` |

These tiers are fallback-only. Exact repeated `ModelEnvelope` evidence from the
same packaged OCI config digest, ordered rootfs layer diff IDs, runtime,
immutable model revision, compute/device, decoder, concurrency, and chunk
policy is authoritative. It can qualify a model above a generic tier only when
fresh admission also fits its measured incremental peaks and positive margins.

CUDA selection uses the minimum free-memory result from three fresh samples of
the exact configured device, subtracts `GPU_MEMORY_RESERVE_GIB`, and rechecks
inside the load/reload gate. It never sums devices. A canonical shared-CUDA
deployment must set `CANONICAL_SHARED_CUDA=True` and a positive audited reserve;
missing, stale, invalid, or non-matching evidence closes admission there.

### External ModelEnvelope artifacts

The artifacts are deployment-owned inputs, not files generated by the ordinary
runtime. Ordinary public deployments use one of the base Compose profiles
without these binds. An exact-evidence or canonical deployment adds the
supplied `docker-compose.model-envelopes.yml` overlay to its selected base:

```bash
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml config --quiet
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml up -d
```

The overlay binds all three paths:

| Artifact | Exact host path | Read-only container path |
| --- | --- | --- |
| Parent metadata | `/var/lib/subgen/model-envelopes/v1` | `/opt/subgen/model-envelopes` |
| Catalog | `/var/lib/subgen/model-envelopes/v1/catalog.json` | `/opt/subgen/model-envelopes/catalog.json` |
| OCI identity | `/var/lib/subgen/model-envelopes/v1/image-identity.json` | `/opt/subgen/model-envelopes/image-identity.json` |

The host parent and both regular files must be owned by the numeric UID seen as
the Subgen runtime EUID, normally the configured `PUID`. Keep the parent mode
`0700` and both leaves mode `0600`. The read-only parent bind preserves that
directory owner and mode for strict container-side validation; the exact leaf
binds retain the two artifact paths. The overlay uses long bind syntax with
`create_host_path: false`, so a missing host parent or leaf fails instead of
being replaced by a Docker-created directory. Symlinks, group/other access,
duplicate or unknown JSON fields, malformed/non-ASCII values, catalog integrity
failures, and exact identity/runtime/policy mismatches are rejected. Public auto
reports one bounded reason and uses conservative fallback; canonical shared
CUDA fails closed.

Only the packaged owner tool at `/subgen/profile_model_envelopes.py` writes a
distinct staged catalog. It profiles one explicit model in a clean isolated
process for at least three cold cycles and uses the canonical resource owner for
admission and incremental-peak arithmetic. The host must compare both OCI
identity components with `docker image inspect` immediately before every
profiler and every overlay-enabled automatic runtime start. A tag or registry
manifest digest alone is insufficient.

Both packaged Compose files use this repository's v0.5.0 GHCR image. The Docker
build and source profile retain the immutable upstream Subgen 2026.06.6 base
digest with this repository's stable 2026.07.1 runtime override. The packaged
image includes the profiler and complete `subgen_core`; the source profile
mounts both at the same paths. The GPU profile additionally enables CUDA and
`float16`.

Keep `CONCURRENT_TRANSCRIPTIONS=1`. Parallel inference increases RAM/VRAM
pressure and is outside the supported public policy.

### Models not recommended here

- `large-v3-turbo` / `turbo`: fast for transcription, but OpenAI states that Turbo was not trained for translation and can return the source language even when asked to translate.
- Models ending in `.en`: English-only checkpoints cannot translate foreign speech.
- Distilled English-oriented models: useful for fast English transcription, but not the conservative choice for a mixed-language translation library.
- `large-v3` on CPU: valid, but usually too slow and resource-heavy for a shared home server.

For this project's purpose, stay with multilingual `small`, `medium`, or `large-v3`. OpenAI recommends `medium` or `large` for the best translation results in its [Whisper usage guidance](https://github.com/openai/whisper#command-line-usage).

### Frigate deployment boundary

Ashby's Frigate-hosted Subgen is an operator-specific deployment on a shared
RTX 3090. It is not the public hardware recommendation and is not live on
v0.5.0 yet. Frigate and Ollama remain higher priority; Subgen never starts,
stops, reconfigures, or coordinates either service.

The gated target uses `WHISPER_MODEL=auto`, exact read-only catalog/identity
artifacts, `CANONICAL_SHARED_CUDA=True`, startup scan on, and a positive audited
`GPU_MEMORY_RESERVE_GIB`; `auto` is prohibited on that host. A 12 GiB
hard/no-swap cgroup is profiler-only evidence. The automatic/production runtime
must independently qualify the same image and envelope under the eventual
10 GiB hard/no-swap limit before deployment.

Its intended first-failure policy sets `AUTO_DELETE_INVALID_MEDIA=true` and the
legacy alias false only after an isolated disposable proof. Deletion remains
monitor-only; repair stays inactive/report-only. The Plex-hosted Subgen instance
is retired. Public rollback restores v0.4.1, while this Frigate deployment has a
separate preserved v0.3.0 Compose/config, model cache, state, and OCI-identity
rollback.

## Connect Plex

Plex and Jellyfin server/token settings are blank by default. Leaving them blank disables those integrations; standalone file and folder processing needs no media-server settings.

Plex webhooks are optional and require an active [Plex Pass for the server owner or administrator](https://support.plex.tv/articles/115002267687-webhooks/).

Subgen scans `TRANSCRIBE_FOLDERS` at startup and watches them for changes. To enable Plex webhook events, set your own values, for example:

```dotenv
SUBGEN_BIND_ADDRESS=192.168.1.50
PLEX_SERVER=http://192.168.1.20:32400
PLEX_TOKEN=replace-with-your-token
```

Then configure Plex to send webhooks to:

```text
http://192.168.1.50:9000/plex
```

Subgen must see the same media path Plex reports. If Plex reports a different root, map it explicitly:

```dotenv
USE_PATH_MAPPING=True
PATH_MAPPING_FROM=/path/reported/by/plex
PATH_MAPPING_TO=/media
```

Never commit `.env`; it is ignored by Git.

## Failure monitoring and optional cleanup

The host monitor records exact paths and five-field source identities. Subgen
checks the shared schema-v1 registry before opening media and skips only an
exact marker match; a replacement at the same path has a different identity
and proceeds normally. Missing, malformed, oversized, unsafe, or unreadable
registry state fails open for transcription and never authorizes deletion.

Before model load, the runtime obtains two independent typed results: bounded
FFprobe JSON and an isolated bounded PyAV process. Only
`invalid_format + invalid_format` becomes `invalid_media`. A recognized valid
silent container becomes `no_audio` and remains. Disagreement, timeout,
permission/I/O failure, validator crash, disappearance, or an identity change
is indeterminate and remains.

The monitor is the only automatic deletion decision owner. It requires a fresh
`media_validation_failed` event, exact `invalid_media` class, dual-validator
evidence, a still-current identity, enabled policy and threshold, and a durably
written and re-read marker before delegating exact unlink. Generic/file/worker
errors, inference or resource failures, OOM, pressure yield, SIGSEGV, raw log
text, untyped legacy intents, and stale replacements cannot reach deletion.

The optional host helpers require Python 3.10+, Docker CLI/socket access, an
existing service account, media traverse/read permissions, and state-directory
write access. The supplied units assume `/opt/subgen` and `mediauser:media`;
edit their user, group, working directory, environment file, and command paths
before installation when your host differs.

```bash
cp monitor.env.example monitor.env
sudo install -d -m 700 -o mediauser -g media /opt/subgen/monitor
sudo cp systemd/subgen-monitor.service /etc/systemd/system/
sudo cp systemd/subgen-repair.service systemd/subgen-repair.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service subgen-repair.timer
```

The public policy is:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES=5242880
```

`AUTO_DELETE_INVALID_MEDIA` is the canonical opt-in. The deprecated
`AUTO_DELETE_FAILED_FILES=true` alias is accepted through 0.5.x but enables
only the same invalid-media path and warns once. Either setting can enable that
path during migration, so set both false to disable it.

`SUBGEN_REPAIR_ACTION=delete` is also accepted for migration but is always
report/evidence-only and warns. Repair never removes media or legacy empty
subtitle markers. Old crash or untyped pending delete intents are preserved as
policy-blocked evidence rather than resumed. The repair timer deduplicates
unchanged evidence, retries bounded ordered audit writes, and rotates one
owner-only log backup; it is not a second deletion route.

<details>
<summary><b>Enable invalid-media-only monitor deletion</b></summary>

Deletion is irreversible. First review typed report output against disposable
media, then set only:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=true
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

Restart the live monitor. The repair timer may remain enabled for evidence, but
it never deletes. Use a separate disposable media root to prove that a durable
marker audit precedes exact deletion and that a replacement generation is
processed. Never corrupt or delete production media for this test.

</details>

The exact unlink owner still uses Linux descriptor-relative, no-follow
traversal, a same-filesystem private quarantine, durable operation token, and a
five-field identity check. Unsupported platforms fail closed. It deletes only
the exact regular file; it never recursively deletes directories or creates
empty subtitle skip markers.

Keep `SUBGEN_STATE_DIR` on a local filesystem outside `MEDIA_ROOT`, owned by the
monitor service account and not group/world writable. State and event files are
owner-only and the services use `UMask=0077`. The Compose profiles mount the
same directory read-only, so container `PUID` needs matching numeric-UID or
explicit read/traverse access.

## How it works

1. Subgen scans or receives a media event.
2. It checks the shared failure registry before opening media; only the exact marked generation is skipped.
3. Bounded typed FFprobe and isolated PyAV validate the same unchanged generation. Silent or indeterminate media is retained; only dual-invalid media is deletion-eligible.
4. Fresh host/cgroup/GPU admission chooses one automatic model, or validates the explicit fixed model, before loading it.
5. Subgen checks for existing English subtitles, selects one audio track, and detects speech language on that same track.
6. A short file may use the whole-file path. A longer local file uses one capacity-derived 5/10/20/30-minute core at a time; pressure releases the model and retries the same uncommitted cursor down to a five-minute floor.
7. Successful structured results are shifted to source time, owned by midpoint, merged monotonically, and atomically published once as English SRT/LRC. No per-window subtitle is created.
8. Structured lifecycle events let the monitor attribute a terminal failure to the exact source generation and durably mark it before any enabled invalid-media-only deletion.

Uploaded `/asr` and OpenAI-compatible byte buffers retain their existing
whole-request APIs and never enter local-file segmentation.

At the code level, `subgen_override.py` is the executable FastAPI composition root and compatibility facade. Algorithms live with their canonical owners under `subgen_core`; the facade wires them to configuration, routes, worker dispatch, and legacy imports.

<details>
<summary><b>Repository components</b></summary>

| Path | Purpose |
| --- | --- |
| `subgen_override.py` | Executable FastAPI facade and composition root for configuration, routes, worker dispatch, and compatibility exports. |
| `subgen_core/` | Canonical owners for queueing, optional integration clients, media policy and scanning, model lifecycle, and transcription. |
| `language_code.py` | Language-code mapping used by the runtime. |
| `subgen_failure_markers.py` | Versioned exact-generation marker schema, secure reader, cache, and match decisions. |
| `profile_model_envelopes.py` | Owner-operated isolated profiler that writes staged exact-runtime envelope evidence; it is never called by ordinary scanning or workers. |
| `docker-compose.ghcr.yml` | Recommended packaged CPU deployment. |
| `docker-compose.yml` | Source bind-mount deployment. |
| `docker-compose.gpu.yml` | Packaged GHCR deployment with NVIDIA CUDA enabled. |
| `docker-compose.model-envelopes.yml` | Optional strict ModelEnvelope parent-and-leaf bind overlay for exact-evidence deployments. |
| `monitor_subgen_failures.py` | Live failure monitor and threshold enforcement. |
| `repair_subgen_failures.py` | Periodic report/evidence pass; legacy delete input is accepted but never removes media. |
| `subgen_ops_safety.py` | Shared fail-closed state, fingerprint, quarantine, and exact-file deletion primitives. |
| `tests/` | CPU-only regression suite with ML dependencies mocked. |

</details>

## Operations

```bash
# Status
curl --fail http://127.0.0.1:9000/status
docker compose -f docker-compose.ghcr.yml ps

# Logs
docker logs --tail 100 subgen

# Stop and remove the container (media and subtitles remain)
docker compose -f docker-compose.ghcr.yml down

# Host helpers are independent of Compose
sudo systemctl disable --now subgen-monitor.service subgen-repair.timer
```

Models remain in `SUBGEN_MODEL_PATH`. Remove that directory manually only if you also want to reclaim the downloaded model storage.

The checkout, `.env`, generated subtitles, models, and optional monitor state are deliberately retained. See the [complete stop and uninstall steps](./docs/INSTALL.md#stop-or-uninstall) before removing them.

## FAQ

**Does this replace Bazarr?**

It can, if your goal is generating subtitles locally. Bazarr remains useful when you prefer downloading existing subtitles from providers.

**Does it work without Plex webhooks?**

Yes. Folder scanning and monitoring are enough for many installations.

**Can Intel or AMD graphics run the CUDA profile?**

No. Use the CPU compose file unless the Subgen/faster-whisper stack adds and documents another supported accelerator. The included GPU profile is NVIDIA CUDA only.

**Why use automatic model selection?**

Container capacity alone does not describe current headroom or exact backend
cost. Automatic selection keeps conservative fallback ceilings for ordinary
installs and uses immutable repeated `ModelEnvelope` evidence when available,
while still fixing one model for the whole file.

## Development

Use Python 3.10 or newer.

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the complete local checks. Security issues should follow [SECURITY.md](./SECURITY.md).

## Prior art and licence

This is a derivative deployment of [McCloudS/Subgen](https://github.com/McCloudS/subgen). The upstream project provides the core Plex/Jellyfin/Emby/Bazarr integration and Whisper runtime; this repository maintains a focused English-translation configuration and additional operational safeguards.

Licensed under the [MIT License](./LICENSE), preserving the upstream copyright notice.
