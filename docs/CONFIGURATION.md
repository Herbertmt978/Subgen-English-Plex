# Configuration guide

Copy `.env.example` to `.env` and change values there. The compose files contain working conservative fallbacks, but keeping local choices in `.env` makes upgrades easier and prevents private values entering Git.

## Conservative defaults

| Setting | Default | Reason |
| --- | --- | --- |
| `WHISPER_MODEL` | `medium` | Multilingual model with better translation quality than the minimum profile. |
| `CONCURRENT_TRANSCRIPTIONS` | `1` | Predictable RAM/VRAM use and failure attribution. Fixed in the compose templates. |
| `WHISPER_THREADS` | `4` | Leaves CPU capacity for Plex and the host. |
| `SUBGEN_CPU_LIMIT` | `4.0` | Prevents the container consuming the whole server. |
| `SUBGEN_MEMORY_LIMIT` | `10g` | Allows model, decoding, and long-file headroom without unbounded growth. |
| `COMPUTE_TYPE` | CPU `int8`; GPU `float16` | Conservative compute type for each device. |
| `MODEL_CLEANUP_DELAY` | CPU `60`; GPU `300` seconds | Avoids constant reloads while eventually releasing model memory. |
| `SUBGEN_BIND_ADDRESS` | `127.0.0.1` | Does not expose the HTTP service to the LAN by default. |

See the [README hardware guide](../README.md#model-and-hardware-guide) for tested profile boundaries and model warnings.

## Paths and identity

### `MEDIA_ROOT`

Host folder mounted at `/media` inside the container. Mount the smallest common root that contains the intended libraries.

```dotenv
MEDIA_ROOT=/srv/media
```

### `TRANSCRIBE_FOLDERS`

Pipe-separated container paths to scan and watch:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies|/media/TV
```

Do not put host-only paths here.

### `SUBGEN_MODEL_PATH`

Host folder that persists downloaded model data:

```dotenv
SUBGEN_MODEL_PATH=./models
```

### `PUID` and `PGID`

Numeric Linux identity used for media and subtitle files. Match the owner/group of the media directories.

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

- `small`: minimum/testing profile; translation works but quality is lower.
- `medium`: public default and best balance for CPU/shared hosts.
- `large-v3`: accuracy-first NVIDIA profile.
- `large-v3-turbo` / `turbo`: do not use for translation.
- `*.en`: do not use for foreign speech.

OpenAI's [Whisper documentation](https://github.com/openai/whisper#command-line-usage) states that Turbo was not trained for translation and recommends a multilingual `medium` or large model for the best translation results.

## Work scheduling

### `CONCURRENT_TRANSCRIPTIONS=1`

This is fixed in the public compose templates. Direct API inference and folder jobs share the same model semaphore, so API traffic cannot silently exceed the concurrency limit.

### `WHISPER_THREADS`

Controls compute threads. It should not exceed the Docker CPU limit. Start at four; lower it to two on a busy shared host.

### `SUBGEN_CPU_LIMIT`

Docker CPU ceiling. This is not a reservation: unused CPU remains available to other services.

### `SUBGEN_MEMORY_LIMIT`

Docker memory and memory-plus-swap ceilings are set to the same value, which prevents a large transcription from forcing the host into sustained swap. If Subgen is OOM-killed, first confirm the file is valid and then increase this limit only if the host has real free RAM.

## Scanning and skip rules

The templates set:

```dotenv
MONITOR=True
PROCESS_ADDED_MEDIA=True
PROCESS_MEDIA_ON_PLAY=False
SKIP_IF_TARGET_SUBTITLES_EXIST=True
SKIP_IF_EXTERNAL_SUBTITLES_EXIST=True
SKIP_VIDEO_EXTENSIONS=.avi
```

Subgen scans configured folders, watches for new files, works in advance rather than on playback, and avoids duplicating external English subtitles. Remove `.avi` from the skip list only if you have tested those files.

## Plex integration

Folder monitoring needs no Plex credentials. Webhook processing needs:

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

Path mapping is a string root replacement. Test a single item before enabling a large queue.

## HTTP access

### `SUBGEN_BIND_ADDRESS`

- `127.0.0.1`: safest; only the Docker host can connect.
- a private LAN IP: use when Plex/Bazarr runs on another trusted host.
- `0.0.0.0`: avoid unless a firewall and authentication boundary are deliberately configured.

### `SUBGEN_API_KEY`

When non-empty, this protects `/asr`, `/batch`, `/detect-language`, `/v1/audio/transcriptions`, and `/v1/audio/translations` through `X-Subgen-Api-Key`.

Plex, Jellyfin, and Emby webhook routes do not use this header. Keep them on a trusted network.

### `HTTP_TIMEOUT_SECONDS=30`

Bounds outbound Plex/Jellyfin calls so an unavailable media server does not hold a worker forever.

## Model cleanup

`CLEAR_VRAM_ON_COMPLETE=True` lets Subgen unload the model after the queue is idle. `MODEL_CLEANUP_DELAY` prevents repeated unload/reload cycles during a burst.

- CPU shared host: 60 seconds.
- Conservative GPU: 300 seconds.
- Large library scan on a dedicated GPU host: up to 900 seconds.

## Failure monitor

These values live in `monitor.env`:

```dotenv
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=3
SUBGEN_REPAIR_MIN_CRASH_COUNT=3
SUBGEN_REPAIR_ACTION=report
```

The monitor records exact host/container paths. Duplicate filenames in different directories remain separate. A candidate must still resolve beneath `MEDIA_ROOT`, must not be a symlink, and must be a regular file.

### Optional repeated-offender deletion

Deletion is off in both recovery paths by default. To opt in after reviewing report-only state:

```dotenv
AUTO_DELETE_FAILED_FILES=true
SUBGEN_REPAIR_ACTION=delete
```

Keep both thresholds at three or higher. Deletion is permanent; this project does not move files to a recycle bin.

### Email alerts

SMTP and relay settings are optional. Blank values leave local event reporting enabled without sending email. Never commit `monitor.env`.

## Source override

The source CPU compose file mounts:

```yaml
- ./subgen_override.py:/subgen/subgen.py:ro
- ./language_code.py:/subgen/language_code.py:ro
```

The GHCR image bakes these files into the image and therefore does not need those mounts.
