# Configuration Guide

This is not a full Subgen reference. It is the shortlist of settings that actually matter in this setup.

## `cpus: 8.0`

This is the broad "do not get greedy" limit.

It tells Docker not to let the container use much more than eight CPU cores in total. That gives Subgen room to work without letting it sprawl all over the machine.

If the server feels too busy, lower it.
If the server has plenty of headroom, you can raise it, but do it gradually.

On a separate GPU worker you may still want a CPU cap, but it does not need to do as much of the heavy lifting as a CPU-only Plex box.

## `PUID` and `PGID`

These tell the container which Linux user and group it should act as.

If these are wrong, the usual symptoms are boring but annoying:

- Subgen can read files but not write subtitles
- subtitle files are created with awkward ownership
- permissions look wrong even though the container itself is running fine

In most setups these should match the account that already owns the media folders.

## `TRANSCRIBE_OR_TRANSLATE=translate`

This is the setting that changes the whole personality of the setup.

In simple terms:

- `transcribe` means "write subtitles in the language being spoken"
- `translate` means "write subtitles in English"

This repo uses `translate` on purpose. If you change that, you are changing the point of the setup.

## `TRANSCRIBE_DEVICE`

This is the big split between a CPU install and an NVIDIA install.

- `cpu` is the safe default for a plain Linux box
- `cuda` is what you want on an NVIDIA host

If you are moving Subgen onto a separate VM because it has a real NVIDIA GPU, this is one of the settings you should change first.

## `SUBTITLE_LANGUAGE_NAME=en`

This affects how subtitle files are named.

It makes the output clearly show up as English rather than some vague custom label.

## `SHOULD_WHISPER_DETECT_AUDIO_LANGUAGE=True`

This lets Whisper work out the spoken language when needed.

That matters because translation only works properly if the system has a decent guess at what it is hearing.

## `WHISPER_MODEL=medium`

This is one of the main performance levers.

The rough trade-off is:

- smaller model = lighter and faster, but usually less accurate
- larger model = heavier and slower, but usually better

`medium` is the default here because it is a decent middle ground for a home Plex server. It is good enough to be useful without feeling needlessly heavy.

If you want it gentler on the CPU, try `small`.
If you want reliable non-English-to-English translation and the machine can afford it, move up to `large-v3`.

Do not use `large-v3-turbo` for this repo's translation mode. Turbo is excellent at transcription, but upstream Whisper says it was not trained for translation and may return the original language.

## Picking a Whisper model on purpose

This repo is built around one specific outcome:

- make English subtitles for mixed-language media libraries
- translate non-English speech into English when needed

That means the "best" model is not just about speed. It is about whether the model is a good fit for multilingual media and translation-heavy use.

If you want the shortest version:

- CPU box: start with `medium`
- lighter CPU box: try `small`
- NVIDIA box with enough VRAM: use `large-v3`
- translation-first setup: avoid the English-only `.en` models

Here is the practical version:

| Model | Best fit | Hardware feel | Pros | Cons |
| --- | --- | --- | --- | --- |
| `small` | Light-duty home server, testing, or older CPU box | Comfortable on CPU | Faster and lighter than `medium` | More likely to struggle with noisy audio, accents, overlapping speech, or awkward mixes |
| `medium` | General home server use | Good CPU default | Best balance for CPU installs in this repo | Still noticeable on busy servers while working |
| `large-v3-turbo` | Same-language transcription | Happiest on CUDA | Very fast, strong transcription | Not trained for reliable speech translation; can return the source language |
| `large-v3` | Translation-first setup | Best on a stronger NVIDIA GPU | Best fit here for multilingual speech-to-English output | Slower and heavier than Turbo |
| `distil-large-v3` | Fast English ASR on GPU | Good on CUDA | Fast and efficient for English transcription | Not my first pick for this repo because it is aimed at English speech recognition rather than multilingual translation |

A few plain-English rules help more than a giant benchmark table:

- If the server is mostly CPU and also runs Plex, `medium` is the safe place to start.
- If the machine has a real NVIDIA GPU available to Docker and translation matters, use `large-v3`.
- If you only want a very light test run, `small` is fine, but do not judge the whole idea by `small` quality alone.

Two model choices are worth calling out as bad fits for this repo:

- English-only `.en` checkpoints
  These are fine for English transcription, but they are the wrong shape for a setup that needs to hear non-English audio and turn it into English subtitles.
- `distil-large-v3` for a translation-first library
  It is a respectable fast model, but it is mainly positioned as a drop-in English ASR option rather than a multilingual translation workhorse.
- `large-v3-turbo` for a translation-first library
  Turbo was trained without translation data. It is a speed choice for transcription, not a safe choice for generating English text from foreign speech.

If you want the official upstream references, check the [OpenAI Whisper README](https://github.com/openai/whisper), the [Turbo release note](https://github.com/openai/whisper/discussions/2363), and the [faster-whisper README](https://github.com/SYSTRAN/faster-whisper).

## `WHISPER_THREADS=8`

This controls how hard Whisper is allowed to lean on the CPU.

More threads usually means faster work, but also more pressure on the server. If Plex feels sluggish while Subgen is running, this is one of the first settings worth trimming.

## `CONCURRENT_TRANSCRIPTIONS=1`

This means one active job at a time, and that is deliberate.

Running several transcription jobs at once sounds efficient, but on a Plex box it often just makes everything feel busier without much satisfaction in return.

## `COMPUTE_TYPE=int8`

You do not need the low-level explanation here. The short version is:

- it is a lighter CPU-friendly inference mode
- it helps keep resource use sensible
- it is a good fit for this kind of setup

If you switch to `TRANSCRIBE_DEVICE=cuda`, the usual companion setting is `COMPUTE_TYPE=float16`.

## `gpus: all`

This is only relevant on an NVIDIA Docker host.

It tells Docker to actually hand the GPU through to the container. If you set `TRANSCRIBE_DEVICE=cuda` but forget this part, the container will start and then disappoint you.

## `TRANSCRIBE_FOLDERS`

This is the list of folders Subgen watches and scans.

The format uses `|` between entries:

```text
/media/PlexFilmsHD|/media/PlexFilms|/media/PlexTVHD|/media/PlexTV
```

Only list folders you genuinely want it touching.

## `MONITOR=True`

This tells Subgen to keep watching those folders after the first scan.

Without it, you are closer to a one-off pass. With it, new files can still be picked up later.

## `PROCESS_ADDED_MEDIA=True`

This says new media should be processed.

## `PROCESS_MEDIA_ON_PLAY=False`

This says do not wait until somebody presses play before doing the work.

That is usually the better choice if you want subtitles ready in advance instead of discovered at the last possible moment.

## `SKIP_IF_TARGET_SUBTITLES_EXIST=True`

This is the main anti-duplication setting.

It effectively says:

"If English subtitles already exist, do not do the job again."

That saves time and stops the library filling up with duplicate subtitle files.

## `SKIP_IF_EXTERNAL_SUBTITLES_EXIST=True`

This is the second line of defence against duplication.

If the file already has external subtitles, Subgen will usually leave it alone.

## `CLEAR_VRAM_ON_COMPLETE=True`

This comes from the original Subgen style of config.

On a CPU-based setup it is mostly harmless. There is no real need to overthink it here.

## `MODEL_CLEANUP_DELAY`

This tells the script how long to wait before unloading the model after the queue goes quiet. The CPU template uses 30 seconds; the GPU profile uses 900 seconds to avoid repeatedly loading a large model during a library scan.

That helps avoid a silly pattern where the model is constantly unloaded and reloaded if a few files arrive close together.

## `SKIP_VIDEO_EXTENSIONS=.avi`

This setup skips `.avi` files.

That was left in because older containers and oddball encodes are often more trouble than they are worth. If you have AVI files you genuinely care about, you can remove it, but I would do that deliberately rather than casually.

## `NOTIFY_ON_ENGLISH_AUDIO_MISMATCH=True`

This belongs to the custom Python override, not plain stock Subgen.

It means:

- if the file metadata suggests there is English audio
- but Whisper hears another language
- record that fact and optionally send an email

It is mainly there for awkward edge cases, not for the happy path.

## `SUBGEN_API_KEY`

This is optional. When set, the compute-heavy `/asr`, OpenAI-compatible transcription/translation, `/batch`, and `/detect-language` endpoints require the same value in the `X-Subgen-Api-Key` header. Plex, Jellyfin, and Emby webhook routes remain usable without that header.

Keep the value in a private `.env` file, not in source control. Also bind port 9000 to a private interface where possible.

## `HTTP_TIMEOUT_SECONDS=30`

This bounds Plex and Jellyfin HTTP calls so an unavailable media server cannot leave a worker stuck forever.

## Monitor settings

These live in `monitor.env`, not in the main Docker compose file.

## `AUTO_DELETE_FAILED_FILES=true`

This is the setting that deserves the most respect.

It means the monitor will delete files that repeatedly trip the known failure patterns in the Subgen logs.

Why somebody might want it:

- one bad file can jam the queue for ages
- this gives the setup a way to recover by itself

Why somebody might hesitate:

- deleted really means deleted
- a false positive would still hurt

The default is `false`. When enabled, `AUTO_DELETE_MIN_FAILURES=3` requires three observations before deletion. The monitor and repair timer require one exact path, re-check that it is under `MEDIA_ROOT`, refuse symlinks and directories, and delete only a regular file. No fake or empty subtitle is written.

## SMTP settings

These are only needed if you want email alerts:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_TO`
- `SMTP_USE_TLS`

If they are blank, the monitor still records the events. It just does not send any mail.

## `./subgen_override.py:/subgen/subgen.py`

This line is what turns the setup from "stock Subgen with env vars" into "stock container plus custom runtime behaviour".

If you remove it, you are no longer running this repo's behaviour. You are back to the stock Subgen script plus whatever environment variables remain.

That is fine if it is a deliberate choice. It is not the sort of thing you want to remove by accident.
