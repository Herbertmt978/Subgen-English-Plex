<div align="center">

# Subgen English for Plex

### Missing subtitles should not make a media library unusable.

[![Tests](https://github.com/Herbertmt978/Subgen-English-Plex/actions/workflows/test.yml/badge.svg)](https://github.com/Herbertmt978/Subgen-English-Plex/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/Herbertmt978/Subgen-English-Plex)](https://github.com/Herbertmt978/Subgen-English-Plex/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

[Quick start](#quick-start) · [Hardware guide](#model-and-hardware-guide) · [Safety](#safety-and-trust) · [Configuration](./docs/CONFIGURATION.md) · [Install guide](./docs/INSTALL.md)

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
| Automatic deletion | Disabled. The monitor defaults to `AUTO_DELETE_FAILED_FILES=false`; the repair timer defaults to report-only. Deletion requires an explicit opt-in and three matching failures on one exact path. |
| Network exposure | Port 9000 binds to `127.0.0.1`. Set a private LAN address only when another trusted host must connect. |
| API protection | Compute endpoints can require `SUBGEN_API_KEY`. Plex/Jellyfin/Emby webhook routes remain unauthenticated and should stay on a trusted network. |
| Network calls | Docker pulls the image; the first job downloads the selected model. Configured Plex, Jellyfin, Emby, email, or completion-webhook integrations also make network calls. |
| Telemetry | This fork adds no telemetry. |
| Reversibility | `docker compose down` stops the service. Removing the container does not remove media or generated subtitles. |

Before enabling deletion, run report-only for long enough to inspect `monitor/subgen_failed_files.txt` and confirm the paths are correct for your library.

## Quick start

Requirements:

- Linux with Docker Engine and Docker Compose v2
- 64-bit x86 hardware
- at least 8 GB system RAM for a small test profile; 16 GB is the recommended starting point
- NVIDIA Container Toolkit only when using the CUDA compose file

```bash
git clone https://github.com/Herbertmt978/Subgen-English-Plex.git
cd Subgen-English-Plex
cp .env.example .env
```

Edit `.env` and set at least:

```dotenv
MEDIA_ROOT=/path/to/your/media
TRANSCRIBE_FOLDERS=/media/Movies|/media/TV
PUID=1000
PGID=1000
```

The paths in `TRANSCRIBE_FOLDERS` are container paths beneath `/media`. Then start the prebuilt package:

```bash
mkdir -p ./models
docker compose -f docker-compose.ghcr.yml up -d
curl --fail http://127.0.0.1:9000/status
```

The first subtitle job downloads the model, so it starts more slowly than later jobs.

<details>
<summary><b>Run directly from the checked-out source</b></summary>

Use the source compose file when you want the local Python files bind-mounted into the upstream Subgen image:

```bash
docker compose up -d
```

Use `docker-compose.ghcr.yml` for the simpler packaged install. Both use the same conservative defaults from `.env`.

</details>

## See it work

```console
$ curl --silent http://127.0.0.1:9000/status
{"version":"Subgen 2026.07.1, stable-ts 2.19.1, faster-whisper 1.2.1 (Docker)"}

$ docker logs --follow subgen
... WORKER START : [DETECT_LANGUAGE] ...
... Detected language: Spanish
... WORKER START : [TRANSCRIBE] ...
... WORKER FINISH: [TRANSCRIBE] ...
```

For translation mode, the resulting external subtitle is named as English, for example:

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

Both packaged compose files use this repository's GHCR image. The image inherits the CUDA-capable `mccloud/subgen:latest` base and bakes in `subgen_override.py` plus `language_code.py`; the GPU compose file additionally enables `gpus: all`, CUDA, and `float16`.

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

Folder monitoring works without Plex credentials. Subgen scans `TRANSCRIBE_FOLDERS` at startup and watches them for changes.

For Plex webhook events, set these in `.env`:

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

The helper monitor records exact failing paths and protects against duplicate basenames, directories, symlinks, and paths outside `MEDIA_ROOT`.

Safe report-only setup:

The supplied systemd units assume the repository is installed at `/opt/subgen` and runs as `mediauser:media`. Edit `User`, `Group`, `WorkingDirectory`, and `ExecStart` first if your installation differs.

```bash
cp monitor.env.example monitor.env
sudo cp systemd/subgen-monitor.service /etc/systemd/system/
sudo cp systemd/subgen-repair.service systemd/subgen-repair.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service subgen-repair.timer
```

The defaults do not delete media:

```dotenv
AUTO_DELETE_FAILED_FILES=false
SUBGEN_REPAIR_ACTION=report
```

<details>
<summary><b>Enable repeated-offender deletion</b></summary>

Deletion is irreversible. It is intended for operators who prefer removing a repeatedly crashing source file so a full library scan can continue.

After reviewing report-only output, set:

```dotenv
AUTO_DELETE_FAILED_FILES=true
AUTO_DELETE_MIN_FAILURES=3
SUBGEN_REPAIR_ACTION=delete
SUBGEN_REPAIR_MIN_CRASH_COUNT=3
```

Then restart the monitor and repair timer:

```bash
sudo systemctl restart subgen-monitor.service subgen-repair.timer
```

Only the exact regular file is eligible. The cleanup code does not recursively delete directories or create empty subtitle skip markers.

</details>

## How it works

1. Subgen scans or receives a media event.
2. It checks for existing English subtitles and selects one audio track.
3. Whisper detects the speech language on that same track.
4. The worker transcribes English speech or translates foreign speech, then writes an English `.srt`.
5. Structured lifecycle events let the monitor attribute a failure to an exact path.

<details>
<summary><b>Repository components</b></summary>

| Path | Purpose |
| --- | --- |
| `subgen_override.py` | Current Subgen runtime plus translation, queue, API, and lifecycle fixes. |
| `language_code.py` | Language-code mapping used by the runtime. |
| `docker-compose.ghcr.yml` | Recommended packaged CPU deployment. |
| `docker-compose.yml` | Source bind-mount deployment. |
| `docker-compose.gpu.yml` | Packaged GHCR deployment with NVIDIA CUDA enabled. |
| `monitor_subgen_failures.py` | Live failure monitor and threshold enforcement. |
| `repair_subgen_failures.py` | Periodic report/delete pass for repeated exact offenders. |
| `tests/` | CPU-only regression suite with ML dependencies mocked. |

</details>

## Operations

```bash
# Status
curl --fail http://127.0.0.1:9000/status
docker compose ps

# Logs
docker logs --tail 100 subgen

# Stop and remove the container (media and subtitles remain)
docker compose -f docker-compose.ghcr.yml down
```

Models remain in `SUBGEN_MODEL_PATH`. Remove that directory manually only if you also want to reclaim the downloaded model storage.

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

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the complete local checks. Security issues should follow [SECURITY.md](./SECURITY.md).

## Prior art and licence

This is a derivative deployment of [McCloudS/Subgen](https://github.com/McCloudS/subgen). The upstream project provides the core Plex/Jellyfin/Emby/Bazarr integration and Whisper runtime; this repository maintains a focused English-translation configuration and additional operational safeguards.

Licensed under the [MIT License](./LICENSE), preserving the upstream copyright notice.
