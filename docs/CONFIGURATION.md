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
| `SUBGEN_IMAGE` | release tag `v0.4.0` | Keeps packaged CPU/GPU deployments on the documented release; blank uses the default. |

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
SKIP_MARKED_FAILED_FILES=true
SKIP_VIDEO_EXTENSIONS=.avi
```

Subgen scans configured folders, watches for new files, works in advance rather than on playback, and avoids duplicating external English subtitles. When marker skipping is enabled, an exact path plus five-field identity match is rejected before media probing; a replacement at the same path proceeds normally. Remove `.avi` from the skip list only if you have tested those files.

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

`CLEAR_VRAM_ON_COMPLETE=True` lets Subgen unload the model after the queue is idle. `MODEL_CLEANUP_DELAY` prevents repeated unload/reload cycles during a burst.

- CPU shared host: 60 seconds.
- Conservative GPU: 300 seconds.
- Large library scan on a dedicated GPU host: up to 900 seconds.

## Failure monitor

These values live in `monitor.env`:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=3
SUBGEN_REPAIR_MIN_CRASH_COUNT=3
SUBGEN_REPAIR_ACTION=report
SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES=5242880
```

The monitor records exact, case-preserving host/container paths plus a device/inode/size/time fingerprint for the observed file generation. On the first qualifying failure it atomically writes or refreshes `subgen_failure_markers.json` before any optional delete call. Duplicate filenames in different directories remain separate, and replacing a file at the same path resets its failure threshold and makes the old marker stale. A candidate must still remain beneath `MEDIA_ROOT`, must not pass through a symlink, and must be a regular file. Invalid registry state is preserved for diagnosis rather than overwritten.

The two-minute repair timer persists each candidate's semantic result and evidence signature. If its status, detail, failure evidence, crash count, and resolved path are unchanged, the next process neither appends the same outcome nor deletes that path again. A change to the monitor evidence allows one new result. Failed audit writes enter a bounded FIFO queue in the atomically replaced repair state and are retried, with their original timestamps, before new candidates are processed. A transient head failure retains that event and every later event in order.

`SUBGEN_REPAIR_EVENT_LOG_MAX_BYTES` must be at least 256 bytes and bounds the current `subgen_repair_events.log`; the safe default is 5 MiB (`5242880` bytes). The minimum guarantees that a fixed omission record can advance the FIFO when an original event is too large. Before an append would cross the limit, the current private regular file replaces the single `subgen_repair_events.log.1` backup under an exclusive process lock. The repairer refuses symlinks, hardlinks, non-regular files, and files owned by another Linux user. It never uses a media path for rotation. The equivalent one-run CLI option is `--event-log-max-bytes`.

Monitor and repair deletion are fail-closed Linux operations. They reject lexical traversal, open the media root and every parent directory with `O_NOFOLLOW`, and move the candidate into a random private directory on the same filesystem before final validation and unlink. The persisted fingerprint covers device, inode, size, modification time, and change time. A leaf swap is either restored or preserved in quarantine; it is never mistaken for the observed offender.

The delete intent, fingerprint, and quarantine token are atomically replaced and directory-synced before media is moved. On restart, recovery resumes that token and records `deleted_recovered`; it does not process the same stale monitor candidate again. Setting `AUTO_DELETE_FAILED_FILES=false` or `SUBGEN_REPAIR_ACTION=report` pauses a pending intent without discarding its identity. Platforms without the required descriptor-relative primitives do not delete.

`SUBGEN_STATE_DIR` must be a real local directory owned by the service account and not group/world writable. Use mode `0700`; state, lock, summary, heartbeat, marker, and audit files are forced to `0600`. Do not put operational state beneath `MEDIA_ROOT` or on an untrusted network filesystem. The container reads the same directory through a read-only mount, so its numeric `PUID` must be able to traverse the directory and read `subgen_failure_markers.json`.

### Optional repeated-offender deletion

Deletion is off in both recovery paths by default. To opt in after reviewing report-only state:

```dotenv
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=3
AUTO_DELETE_FAILED_FILES=true
AUTO_DELETE_MIN_FAILURES=3
SUBGEN_REPAIR_ACTION=delete
```

When both features are enabled, `AUTO_DELETE_MIN_FAILURES` cannot exceed `AUTO_MARK_MIN_FAILURES`, because the active marker prevents another Subgen failure from incrementing the later delete count. Existing three-failure deletion users should set both values to `3` before upgrading. A deliberately aggressive Plex/*Arr deployment may set both to `1`, delete the first exact failed generation, and let Sonarr/Radarr replace it. Public deletion remains off. Deletion is permanent; this project does not move files to a recycle bin.

After upgrading legacy state, run the monitor once before changing the repair action to `delete`. It resets unverified path-only counts and fingerprints the current file, so a possible replacement cannot inherit old failures. The repairer blocks deletion when monitor evidence has no generation fingerprint.

### Email alerts

SMTP and relay settings are optional. Blank values leave local event reporting enabled without sending email. Never commit `monitor.env`.

## Source override

The source CPU compose file mounts:

```yaml
- ./subgen_override.py:/subgen/subgen.py:ro
- ./language_code.py:/subgen/language_code.py:ro
- ./subgen_failure_markers.py:/subgen/subgen_failure_markers.py:ro
- ./subgen_ops_safety.py:/subgen/subgen_ops_safety.py:ro
- ./subgen_core:/subgen/subgen_core:ro
- ${SUBGEN_STATE_DIR:-./monitor}:/opt/subgen/monitor:ro
```

These mounts keep the executable facade, language helper, marker/identity contract, and canonical package from the same checkout. The packaged CPU and GPU profiles include those Python components and therefore need no source mounts, but all profiles retain the same read-only state-directory mount.
