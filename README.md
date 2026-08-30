<div align="center">

# Subgen English for Plex

### Missing subtitles should not make a media library unusable.

[![Release](https://img.shields.io/github/v/release/Herbertmt978/Subgen-English-Plex)](https://github.com/Herbertmt978/Subgen-English-Plex/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

[Quick start](#quick-start) · [Standalone use](#standalone-without-plex-or-arr) · [Hardware guide](#model-and-hardware-guide) · [Safety](#safety-and-trust) · [Configuration](./docs/CONFIGURATION.md) · [Install guide](./docs/INSTALL.md) · [Ops migration](./docs/MIGRATION.md)

</div>

---

This project generates English subtitles locally, translates non-English speech into English, and watches media folders for new files. It is a focused, tested deployment of [McCloudS/Subgen](https://github.com/McCloudS/subgen), with stricter translation, queue, monitoring, and recovery behaviour for Plex-style libraries.

The conservative default is the multilingual Whisper `medium` model on CPU, `int8`, four threads, and one transcription at a time. It is deliberately slower than an aggressive setup, but it supports the translation job this repository exists to do.

> [!IMPORTANT]
> Whisper output can contain errors or hallucinated text. Treat generated subtitles as an accessibility aid, not an authoritative transcript for legal, medical, or safety-critical use.

## Safety and trust

Review this before mounting a media library:

| Concern | Public default |
| --- | --- |
| Media access | The container can read the mounted media root and write `.srt` files beside media. Mount only the libraries it needs. |
| Failed-file handling | The first qualifying failure creates an exact path-and-generation marker, and Subgen skips only that generation before media probing. Automatic deletion remains disabled (`AUTO_DELETE_FAILED_FILES=false`); the repair timer remains report-only. |
| Network exposure | Port 9000 binds to `127.0.0.1`. Set a private LAN address only when another trusted host must connect. |
| API protection | Compute endpoints can require `SUBGEN_API_KEY`. Plex, Jellyfin, Emby, and Tautulli webhook routes remain unauthenticated and should stay on a trusted network. |
| Network calls | Docker pulls the image; the first job downloads the selected model. Configured Plex, Jellyfin, Emby, email, or completion-webhook integrations also make network calls. |
| Telemetry | This fork adds no telemetry. |
| Persistent files | The checkout, `.env`, model cache, generated `.srt` files, and optional monitor state remain until you remove them deliberately. |
| Reversibility | `docker compose -f docker-compose.ghcr.yml down` stops the recommended packaged deployment. Optional host systemd helpers stop separately. Removing the container does not remove media or generated subtitles. |

Before enabling deletion, run report-only for long enough to inspect `monitor/subgen_failed_files.txt` and confirm the paths are correct for your library.

## Quick start

Requirements:

- Linux with Docker Engine and Docker Compose v2
- 64-bit x86 hardware
- 16 GB system RAM for the default `medium` profile; 8 GB is only for the lower-quality `small` test profile
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

Replace `PUID` and `PGID` with `id -u` and `id -g` for the account that owns the test file and `./monitor`. `TRANSCRIBE_FOLDERS` uses container paths beneath `/media`. The empty state directory is mounted read-only into Subgen; if the optional host monitor writes markers later, it must run under the same numeric UID or otherwise grant that UID read/traverse access. Validate the isolated configuration before exposing a library:

```bash
docker compose -f docker-compose.ghcr.yml config --quiet
docker compose -f docker-compose.ghcr.yml up -d
curl --fail --retry 30 --retry-connrefused --retry-delay 2 http://127.0.0.1:9000/status
```

The status endpoint proves HTTP readiness. The first subtitle job also downloads the model, so follow the worker before judging the result:

```bash
docker logs --follow subgen
```

After `WORKER FINISH: [TRANSCRIBE]`, confirm an English sidecar exists:

```bash
find ./smoke-test -type f -name '*.en.srt' -print
```

The exact filename is configurable; the default resembles `Movie Name.subgen.medium.en.srt`. Once this passes, replace `MEDIA_ROOT` with the smallest common host root for the intended libraries and set container paths such as `TRANSCRIBE_FOLDERS=/media/Movies|/media/TV`.

<details>
<summary><b>Run directly from the checked-out source</b></summary>

Use the source compose file when you want the local executable facade, language helper, and `subgen_core` package bind-mounted read-only into the upstream Subgen image:

```bash
docker compose up -d
```

Use `docker-compose.ghcr.yml` for the simpler packaged install. Packaged profiles default to this release's `v0.4.1` image instead of a moving `latest` tag; `SUBGEN_IMAGE` is available for a deliberate tag or digest override. Both use the same conservative defaults from `.env`.

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
Movie Name.subgen.medium.en.srt
```

Existing English subtitles are skipped by default.

## Model and hardware guide

### Recommended profiles

The RAM and VRAM figures below are planning budgets, not guarantees. File length, codec, concurrent services, and runtime versions all affect peak usage. Start with one transcription and measure your own host before increasing anything.

| Profile | Suggested hardware | Model | Device / compute | Container limits | Use it when |
| --- | --- | --- | --- | --- | --- |
| Minimum test | 4 modern CPU threads, 8 GB RAM | `small` | CPU / `int8` | 2 CPUs, 2 threads, 6 GB RAM | You are proving the setup or accept lower accuracy and long runtimes. |
| **Balanced default** | 6+ modern CPU threads, 16 GB RAM | `medium` | CPU / `int8` | 4 CPUs, 4 threads, 10 GB RAM | You need dependable multilingual-to-English translation on a shared server. |
| Conservative NVIDIA | NVIDIA GPU with 8+ GB VRAM, 16 GB RAM | `medium` | CUDA / `float16` | 4 CPUs, 4 threads, 10 GB RAM | You want the default quality with much faster processing. |
| Accuracy-first NVIDIA | NVIDIA GPU with 12+ GB VRAM, 24 GB RAM | `large-v3` | CUDA / `float16` | 4–6 CPUs, one job, 16–20 GB RAM | Translation quality matters most and the host has measured headroom. |

The default profile in `.env.example` is the bold row: `medium`, four CPU threads, one job, and a 10 GB memory ceiling.

OpenAI lists approximate model VRAM requirements of about 2 GB for `small`, 5 GB for `medium`, and 10 GB for the large family. Runtime overhead is why this guide recommends more VRAM than the model alone. Faster-whisper's published CPU benchmark also shows the `small` model at roughly 1.5 GB RAM with `int8`, before Subgen, decoding, and long-file overhead. See the [OpenAI Whisper model table](https://github.com/openai/whisper#available-models-and-languages) and [faster-whisper benchmarks](https://github.com/SYSTRAN/faster-whisper#benchmark).

### RTX 3090 accuracy recommendation

For an NVIDIA RTX 3090, use the accuracy-first profile when host RAM permits a 20 GB container memory ceiling. Set these `.env` values:

```dotenv
WHISPER_MODEL=large-v3
SUBGEN_MEMORY_LIMIT=20g
```

Start the GPU profile with `docker compose -f docker-compose.gpu.yml up -d`; it supplies the matching inference settings:

```dotenv
TRANSCRIBE_DEVICE=cuda
COMPUTE_TYPE=float16
CONCURRENT_TRANSCRIPTIONS=1
```

### Change profiles through `.env`

Minimum CPU test:

```dotenv
WHISPER_MODEL=small
WHISPER_THREADS=2
SUBGEN_CPU_LIMIT=2.0
SUBGEN_MEMORY_LIMIT=6g
```

Conservative NVIDIA:

```dotenv
WHISPER_MODEL=medium
WHISPER_THREADS=4
SUBGEN_CPU_LIMIT=4.0
SUBGEN_MEMORY_LIMIT=10g
MODEL_CLEANUP_DELAY=300
```

Start it with:

```bash
docker compose -f docker-compose.gpu.yml up -d
```

Both packaged compose files use this repository's GHCR image. Its Docker build and the source profile use an immutable digest for the upstream Subgen 2026.06.6 base that is verified with this repository's 2026.07.1 override; they do not silently follow the upstream `latest` tag. The packaged image includes the executable `subgen_override.py` facade, `language_code.py`, the failure-marker/identity contract, and the canonical `subgen_core` package; the GPU compose file additionally enables `gpus: all`, CUDA, and `float16`.

Accuracy-first NVIDIA:

```dotenv
WHISPER_MODEL=large-v3
WHISPER_THREADS=6
SUBGEN_CPU_LIMIT=6.0
SUBGEN_MEMORY_LIMIT=20g
MODEL_CLEANUP_DELAY=900
```

Keep `CONCURRENT_TRANSCRIPTIONS=1`. Parallel inference increases RAM/VRAM pressure and makes a shared media server less predictable.

### Models not recommended here

- `large-v3-turbo` / `turbo`: fast for transcription, but OpenAI states that Turbo was not trained for translation and can return the source language even when asked to translate.
- Models ending in `.en`: English-only checkpoints cannot translate foreign speech.
- Distilled English-oriented models: useful for fast English transcription, but not the conservative choice for a mixed-language translation library.
- `large-v3` on CPU: valid, but usually too slow and resource-heavy for a shared home server.

For this project's purpose, stay with multilingual `small`, `medium`, or `large-v3`. OpenAI recommends `medium` or `large` for the best translation results in its [Whisper usage guidance](https://github.com/openai/whisper#command-line-usage).

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

The helper monitor records exact failing paths and protects against duplicate basenames, directories, symlinks, and paths outside `MEDIA_ROOT`. On the first qualifying processing error or exactly attributed `SIGSEGV`, it atomically writes a marker containing the case-preserving `/media` path and five-field file identity. Subgen checks that registry before AV/FFmpeg probing and skips only an exact identity match. A Sonarr/Radarr replacement at the same pathname has a different identity and is processed normally. Missing, malformed, oversized, or unreadable registry state fails open for transcription and never authorizes deletion.

Safe report-only setup:

The optional host helpers require Python 3.10+, Docker CLI/socket access, an existing service account, media traverse/read permissions, and state-directory write access. Deletion additionally needs permission to remove the exact media entry.

The supplied systemd units assume the repository is installed at `/opt/subgen` and runs as `mediauser:media`. If your deployment differs, edit `User` and `Group` and replace every `/opt/subgen` occurrence in `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` before copying them.

```bash
cp monitor.env.example monitor.env
sudo install -d -m 700 -o mediauser -g media /opt/subgen/monitor
sudo cp systemd/subgen-monitor.service /etc/systemd/system/
sudo cp systemd/subgen-repair.service systemd/subgen-repair.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service subgen-repair.timer
```

The defaults do not delete media:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=3
SUBGEN_REPAIR_ACTION=report
SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES=5242880
```

The repair state suppresses duplicate log events when a candidate's status, detail, failure fingerprint, crash count, and resolved path are unchanged between timer runs. A semantic change emits one new event. Failed audit writes are kept in a bounded FIFO queue in the atomically replaced repair state and retried before the next candidate pass with their original timestamps. The event log rotates under a process lock to `subgen_repair_events.log.1` before the current log would exceed 5 MiB; only one private regular-file backup is retained, and symlink or hardlink log paths are refused.

Both deletion paths use Linux descriptor-relative operations. They open each directory without following symbolic links, atomically move the candidate into a random `0700` quarantine directory beside it, compare device, inode, size, modification time, and change time against the persisted failure fingerprint, then unlink only inside that private directory. A durable operation token and delete intent are synced before the move, so a process or VM interruption resumes the same quarantine instead of adopting a replacement at the original path. Unsupported platforms fail closed and remain report-only.

Failure counts belong to a file generation, not a pathname forever. Replacing a deleted or repaired file at the same path starts again at one failure, and the periodic repair pass will not act twice on unchanged monitor evidence. Turning either delete setting off pauses any persisted intent; recovery never overrides the current kill switch.

Keep `SUBGEN_STATE_DIR` on a local filesystem, owned by the monitor service account, and not group/world writable. Do not place it inside the media tree or on an untrusted shared mount. State and event files are owner-only; the supplied services also set `UMask=0077`. The Compose profiles mount this directory read-only at `/opt/subgen/monitor`. Because the marker registry is owner-only, the monitor service and container `PUID` must use the same numeric UID (or equivalent explicit read/traverse permissions) and point at the same host state directory.

<details>
<summary><b>Enable repeated-offender deletion</b></summary>

Deletion is irreversible. It is intended for operators who prefer removing a repeatedly crashing source file so a full library scan can continue.

After reviewing report-only output, set:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=3
AUTO_DELETE_FAILED_FILES=true
AUTO_DELETE_MIN_FAILURES=3
SUBGEN_REPAIR_ACTION=delete
SUBGEN_REPAIR_MIN_CRASH_COUNT=3
```

When marker creation and deletion are both enabled, the delete threshold cannot exceed the marker threshold: an active marker stops future rescans, so a later delete count would be unreachable. Existing three-failure deletion installs must therefore set both monitor thresholds to `3` before upgrading.

The project owner's Plex deployment intentionally uses `AUTO_MARK_MIN_FAILURES=1` and `AUTO_DELETE_MIN_FAILURES=1`. That removes the exact bad generation on its first qualifying failure and relies on Sonarr/Radarr to redownload a replacement. This is an explicit operator choice; it is not the public deletion default.

Then restart the monitor and repair timer:

```bash
sudo systemctl restart subgen-monitor.service subgen-repair.timer
```

Only the exact regular file is eligible. The cleanup code does not recursively delete directories or create empty subtitle skip markers.

If upgrading old monitor state, start the monitor once before enabling the repair action. For safety, it resets legacy path-only counts to zero and fingerprints the file currently at that path; old counts are never transferred to a possible replacement. The repairer refuses deletion when a generation fingerprint is absent.

</details>

## How it works

1. Subgen scans or receives a media event.
2. It checks the shared failure registry before opening media; only the exact marked generation is skipped.
3. It checks for existing English subtitles and selects one audio track.
4. Whisper detects the speech language on that same track.
5. The worker transcribes English speech or translates foreign speech, then writes an English `.srt`.
6. Structured lifecycle events let the monitor attribute a failure to an exact path and persist a marker before optional deletion.

At the code level, `subgen_override.py` is the executable FastAPI composition root and compatibility facade. Algorithms live with their canonical owners under `subgen_core`; the facade wires them to configuration, routes, worker dispatch, and legacy imports.

<details>
<summary><b>Repository components</b></summary>

| Path | Purpose |
| --- | --- |
| `subgen_override.py` | Executable FastAPI facade and composition root for configuration, routes, worker dispatch, and compatibility exports. |
| `subgen_core/` | Canonical owners for queueing, optional integration clients, media policy and scanning, model lifecycle, and transcription. |
| `language_code.py` | Language-code mapping used by the runtime. |
| `subgen_failure_markers.py` | Versioned exact-generation marker schema, secure reader, cache, and match decisions. |
| `docker-compose.ghcr.yml` | Recommended packaged CPU deployment. |
| `docker-compose.yml` | Source bind-mount deployment. |
| `docker-compose.gpu.yml` | Packaged GHCR deployment with NVIDIA CUDA enabled. |
| `monitor_subgen_failures.py` | Live failure monitor and threshold enforcement. |
| `repair_subgen_failures.py` | Periodic report/delete pass for repeated exact offenders. |
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

**Why not default to `small`?**

`small` is useful for testing, but `medium` is the more conservative quality choice for a repository whose main job is translating varied real-world speech into English.

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
