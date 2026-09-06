# Subgen English for Plex 0.5.0

Long-file memory management, optional cross-vendor GPUs and clearer progress reporting.

## Why this release exists

A long film should take more time to transcribe, not require an ever-growing
amount of RAM. Earlier versions could finish a short episode but fail on a
long soundtrack using the same Whisper model.

Subgen now processes one manageable section at a time. It saves completed
sections to a private disk journal and joins them at the end. It does
not keep the whole decoded soundtrack or growing transcript in RAM.

That can leave room for a better model. It doesn't remove the model's
own memory requirements: its weights, working buffers and current audio
section must still fit. Smaller sections cannot shrink its weights.

## Before and now

| Release | Main difference |
| --- | --- |
| **0.4.0** | First-failure markers stop repeated attempts on the exact failed file. A replacement with a new fingerprint can be processed normally. |
| **0.4.1** | Keeps that behaviour and repairs secure quarantine handling on NFSv4 directories with an inherited set-group-ID bit. |
| **0.5.0** | Processes long files in bounded sections, chooses models against available memory, yields under pressure, and supports optional Intel/AMD and mixed-GPU workers. Logs and optional Home Assistant sensors explain the work. |

### Long files, smaller working sets

Automatic sections start between five and thirty minutes, based on capacity.
Audio overlaps the joins to help preserve boundary words. Completed sections
stay on disk; the final SRT or LRC appears only after timing validation and
an atomic write. A failed join doesn't leave a partial final subtitle.

Under memory pressure, Subgen releases the model and retries the unfinished
section after recovery, with a shorter section where possible. Completed work
is kept. After three healthy completed sections, the next section can grow
towards its original limit. A measured profile may impose a lower ceiling.

The release also repairs the timing failures found in two film tests. Invalid
timings still fail validation; Subgen doesn't delete spoken lines to force a
pass. Ambiguous repeated phrases at a join are retained rather than silently
discarded. Longer files still need more time and temporary disk space.

### Choose quality, then pace

`WHISPER_MODEL=auto` chooses the highest-quality multilingual model that passes
the RAM and GPU-memory checks, from large-v3 down to tiny. The model stays fixed
through the process and its retries. An explicit model is never silently
downgraded because another application becomes busy. Selected-device automatic
choice uses only models prepared in your local bundle.

Two independent settings control how eagerly Subgen works:

- `SUBGEN_ACTIVITY=passive|balanced|max` targets 50%, 75% or 100% of safe
  post-reserve capacity for chunk planning. It does not reduce model quality.
- `SUBGEN_RUN_MODE=adaptive|dedicated` chooses cooperative background work or
  continuous processing. Defaults are `balanced` and `adaptive`.

For full force, choose `max` and `dedicated`. Dedicated ignores optional
application-priority signals, including camera FPS changes. Real RAM/VRAM
pressure still triggers recovery. Both modes retain chunking, model-fit checks
and emergency protection; neither permits disabling segmentation or yielding.

This is cooperative resource sharing, not instant GPU preemption. A kernel
already running cannot be recalled, and a sudden allocation elsewhere can
still cause trouble. Other applications may run more slowly in dedicated mode.

### Intel, AMD and more than one GPU

The optional Vulkan image and Windows-native package supply the Intel/AMD
worker. NVIDIA uses CUDA. The normal CPU/NVIDIA installation remains available
without Vulkan. See the [setup guide](DEVICE_BUNDLE.md).

`SUBGEN_DEVICES=cuda:0,vulkan:1` is an example, not a fixed two-GPU limit.
Use the indices reported on your machine. Each selected GPU takes separate
chunks using the same verified source checkpoint and weight precision. Every
GPU needs its own model copy; VRAM is not pooled. Integrated graphics consume
shared system RAM, not an additional memory pool.

Faster workers can take more chunks, and results are joined in order. But a
slow integrated GPU can delay the final join, so selecting more GPUs is not
automatically faster. Three-, four- and five-device scheduling has been
simulated, not physically tested on that many GPUs.

Preparation downloads verified OpenAI checkpoints and converts them on CPU.
Paired CUDA/Vulkan preparation verifies source tensors and tokenizer settings
before recording the bundle. It needs more RAM than inference, checks available
capacity and refuses existing output directories. Conversion never runs during
a library scan.

### Language changes inside a film

Selected-device workers check language during decoding rather than choosing
one language for the whole film. Intel and AMD do this independently; they
don't need NVIDIA as a separate detector. An explicit source-language setting
still takes precedence. The legacy single-device path retains its pre-check.

Use `TRANSCRIBE_OR_TRANSLATE=translate` for English output, or `transcribe` to
keep the spoken language. Use a multilingual model. Whisper does not translate
to arbitrary target languages, and renaming a subtitle doesn't translate it.

Native fixes cover a lost sentence ending, invalid zero-duration cues and
sampling state leaking between requests. Tests check words as well as timing.
Recognition isn't perfect: base substituted one passage in a repeated
mixed-language test, including when decoded on Intel alone. Large-v3 retained
all six checked returns to English in the paired rerun. That is regression
evidence, not a universal speech-quality score.

### Progress you can follow

Logs explain model choice and larger-model refusals, with separate RAM/VRAM
requirements and available capacity. Estimates are labelled as estimates;
measured profiles are labelled separately. Near an admission boundary, rounded
figures include the shortfall in MiB.

```text
Starting file: Example Movie.mkv
Selected model: large-v3 (explicit model choice)
File split into 6 planned chunks
Chunk attempt 1/6 — GPU name [device index] — large-v3 — 00:00:00 to 00:05:00
Chunk attempt 1 — GPU name [device index] — large-v3 — 25%
...
Joining chunks 1–6
Chunks joined
File finished successfully: Example Movie.mkv
```

The planned count can grow when retries use smaller sections. Whole-file
progress advances when chunks commit. RAM reports separate current use,
limits, reserves and requirements. “Available for subtitle chunks” is remaining
headroom, not a second reserved pot of RAM.

### Optional Home Assistant reporting

MQTT adds **Subgen Items Left** and **Subgen Scan %**. Subgen inventories the
configured libraries before decoding and starts its watcher first so imports
arriving during the scan aren't missed. State refreshes every 60 seconds;
important changes publish immediately. Attributes contain aggregate counts,
not private media paths or dialogue.

MQTT is optional. A configurable scan watchdog reports an incomplete scan
rather than holding the transcription queue indefinitely.

### Failed files and deletion

With the optional failure monitor, the first qualifying terminal failure
creates a fingerprint-bound marker. Later scans skip that exact file generation;
a replacement with a different fingerprint is eligible again.

Deletion is **off by default**. `AUTO_DELETE_INVALID_MEDIA` permits it only
when both FFprobe and isolated PyAV conclusively reject unchanged media, after
the marker is durably written. Silent video, memory pressure, inference errors,
native crashes, timeouts and disagreeing probes are not proof of a bad import.
Those files are retained.

`AUTO_DELETE_FAILED_FILES` remains a deprecated alias through 0.5.x, with the
same narrow meaning. **Set both to false** to disable deletion. Repair is
report/evidence-only, including its old delete action. Subgen doesn't request
Sonarr/Radarr downloads; replacement depends on your existing automation.

## How the memory limits work

Run `python3 configure_capacity.py` before Compose. It checks the selected
Linux Docker engine and writes a hard memory limit with no extra swap.
Automatic setup protects at least 1 GiB, normally 15% rounded up to its
capacity step. Subgen's automatic limit stops growing at 24 GiB.

The formula covers intermediate sizes as well as 4/6/9/12/16/24/32/64/128 GiB.
These are capacity test points, not physically certified machines. For example,
the 12 GiB profile leaves 2 GiB outside Subgen and sets a 10 GiB container limit.
The interpreter, model and chunks share that limit, with internal headroom.

For ballooned VMs, supply the guaranteed floor, not a temporary maximum.
Rootless Docker needs enforceable cgroup v2/systemd limits. Setup refuses to
guess when those boundaries can't be established.

Conservative requirements can exceed actual use. Optional CUDA ModelEnvelope
catalogs and shared-RAM native calibration let compatible measurements inform
admission without removing reserves. Native calibration runs three cold tests
and records a conservative peak plus a margin, bound to the actual model,
binaries, device, driver, task and thread count. It caps chunks at the tested
length. Unmatched settings fall back to conservative admission.

Calibration itself must pass the initial conservative check. A busy small
machine may need to wait for free RAM even if the model usually uses less.
Don't use someone else's profile to bypass that check. See
[Configuration](CONFIGURATION.md) and [device setup](DEVICE_BUNDLE.md).

## Hardware testing: what is proven so far

| Path | Actual evidence |
| --- | --- |
| x86 CPU | All six activity/run combinations completed pressure recovery on a nominal 4 GiB VM, using tiny/int8 and generated speech. Three chunks joined without OOM or restart. |
| RTX 3090 CUDA | Three cold large-v3 measurements and a 65-minute normal application scan; 13 chunks, 882 timing-valid cues, 5.47 GiB peak container use. |
| RTX 5080 CUDA | Large-v3 calibration, all six application pressure/recovery modes, and selected-device mixed-language checks. |
| Intel integrated Vulkan | Eight speech/silence controls and real pressure recovery. The optional Linux package also passed public preparation, three cold tiny calibrations and a normal 610-second folder scan. |
| AMD integrated Vulkan | Eight speech/silence controls, real competing-RAM recovery, and three cold base calibrations of the portable Windows Release runtime. Its normal folder scan joined 610 seconds into 69 valid cues. |
| Intel UHD 770 + RTX 5080 | Same-checkpoint large-v3 with concurrent chunks over 651.48 seconds. Transcription produced 101 cues and translation 92; both retained all six checked English returns, with ordered timings and released workers. |

These observations do not qualify every model or GPU combination. CPU success
on an Intel/AMD machine is not an iGPU test. Vulkan/mixed processing is new and
experimental. Paired pressure recovery also passed: a bounded 12 GiB competing
allocation crossed an intentionally high test reserve, both workers released,
and ten-minute chunks reduced to five minutes. The 20-minute base-model input
finished with 191 valid cues. This tests recovery without exhausting host RAM;
the high test reserve is not a recommended user setting.
The final distribution passed 3,155 Linux tests and 3,059 Windows tests,
all Compose profiles, installed timing/join checks and Windows ZIP verification.
A prior local simultaneous-GPU test ended in an unexplained workstation restart
and is not counted as a pass. Later mixed tests used a separate machine; they
do not establish the cause of that restart.

Three-to-five-device results and broad capacity boundaries use synthetic policy tests.
Peaks are observations, not recommended minimum limits. Generated speech and
short language fixtures are not whole-library benchmarks.

## Other changes worth knowing

- Subgen runs directly as the container's main process so stop/restart signals
  reach its shutdown path. Owned workers and temporary chunk files are released.
- The pinned CUDA language-window correction and native timing/language
  corrections are packaged, not downloaded or patched during scanning.
- Vulkan supplies segment timing, not word timing. Word-based regrouping
  cannot split or clamp those cues; the log explains the limitation.
- Optional priority producers don't assume a camera system or Ollama. A
  configured stale signal requests a pause in adaptive mode. Subgen doesn't
  start, stop or reconfigure those applications.
- Uploaded byte-buffer API requests remain outside local-file segmentation;
  their input cannot shrink on retry.

## Upgrading and rollback

Back up Compose/configuration, marker state, model cache and the current
immutable image identity. Keep backups outside your media library. Disable
both deletion flags and leave repair in report mode while upgrading.

Regenerate capacity, validate Compose and recreate Subgen. Check `/status`,
model choice, scan progress and restart/OOM counters. For selected devices,
follow the setup guide rather than copying private bundles or old native files.

Rollback restores the backed-up v0.4.1 image/configuration with deletion off;
keep marker history. HTTP routes, subtitle naming and schema-v1 markers remain
compatible. Project/image version is 0.5.0; the overlaid runtime status version
remains `2026.07.1`. See [Migration](MIGRATION.md).

## Before publication

Package and hardware checks described above ran before publication. There was
no mandatory 72-hour soak. Future runtime changes require the affected checks
to be rerun. Tests and builds run locally, not GitHub-hosted. Details are in the
[changelog](../CHANGELOG.md).
