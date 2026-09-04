# Configurable activity and run modes

Status: Approved design, written for review on 2026-09-04

## Summary

Subgen v0.5 will expose two independent, human-readable settings:

- `SUBGEN_ACTIVITY=passive|balanced|max` controls how aggressively Subgen uses
  capacity that remains after all mandatory reserves.
- `SUBGEN_RUN_MODE=adaptive|dedicated` controls whether Subgen cooperates with
  an optional higher-priority application signal or treats the machine as a
  dedicated transcription worker.

`balanced` and `adaptive` are the public defaults. The private Frigate
deployment uses `passive` and `adaptive` because the RTX 3090 is shared with
live camera detection and embeddings.

These settings never disable segmentation, model admission, pressure
monitoring, cgroup limits, RAM or VRAM reserves, allocation-failure handling,
atomic subtitle publication, or generation-bound failure markers. `max` means
maximum safe throughput inside those boundaries, not unrestricted resource
use.

## Problem and intent

The v0.5 runtime already adapts its Whisper model and chunk duration to the
machine, yields unfinished chunks under pressure, and resumes without
publishing a partial subtitle. It does not yet give an ordinary user one clear
way to say whether that spare-capacity work should be cautious or eager, nor a
clear way to distinguish a shared machine from a dedicated transcription
machine.

The outcome is a small policy layer over the existing safety owners. Users get
understandable choices without having to tune memory thresholds, and operators
can still raise reserves or cap chunk duration with the existing expert
settings.

## User-visible configuration

Both values are read once at startup, normalized case-insensitively to the
lowercase canonical value, and reported in `/status` and startup logs. An
empty or unknown value rejects startup with a message listing the accepted
values.

### `SUBGEN_ACTIVITY`

| Value | Working-budget target | Automatic chunk ceiling | Queue cadence | Intended use |
| --- | ---: | ---: | ---: | --- |
| `passive` | 50% of post-reserve capacity | 10 minutes | 5 seconds between files | Shared or latency-sensitive machines |
| `balanced` | 75% of post-reserve capacity | 20 minutes | 1 second between files | Recommended general-purpose default |
| `max` | 100% of post-reserve capacity | 30 minutes | No intentional inter-file delay | Dedicated or throughput-focused machines |

The working-budget target affects chunk planning only. Model selection still
uses the full safely admissible RAM and VRAM budget so `WHISPER_MODEL=auto`
continues to choose the highest-quality safe model. No activity level changes
the mandatory reserve, admission equation, pressure thresholds, three-sample
recovery hysteresis, or one-worker safety default.

The existing hardware-aware baseline remains the starting point. The activity
ceiling can shorten that baseline but cannot lengthen it beyond what capacity
allows. A pressure yield still halves the next attempt down to the five-minute
floor. This keeps long files segmented on every activity level.

### `SUBGEN_RUN_MODE`

| Value | Optional application-priority signal | Genuine RAM/VRAM pressure | Model residency |
| --- | --- | --- | --- |
| `adaptive` | Honoured; Subgen yields to the higher-priority application | Always honoured | May unload while waiting or idle |
| `dedicated` | Disabled by explicit user choice | Always honoured | Retained between queued files when safe |

`adaptive` is recommended and is required when
`CANONICAL_SHARED_CUDA=True`. It can abort only the current uncommitted chunk,
unload the model, wait for recovery, and retry with an equal or smaller window.

`dedicated` is for a machine whose primary job is Subgen. It ignores optional
application-priority publications but never ignores the host, cgroup, PSI,
GPU-reserve, stale-telemetry, or allocation-failure controls. It preserves the
model between queued files to avoid unnecessary reloads. When genuine pressure
appears, it still releases the model and waits exactly as the adaptive mode
does.

Startup rejects `SUBGEN_RUN_MODE=dedicated` when
`CANONICAL_SHARED_CUDA=True` or when a Task 11B shared-host gate is enabled.
If `PRIORITY_PRESSURE_FILE` is also configured, startup rejects the ambiguous
combination and explains that the file must be blank for dedicated mode. This
prevents a user from believing a configured priority signal is active when it
is not.

## Existing expert settings and precedence

The two new settings supply policy defaults; they do not create a second
resource-management system.

1. Finite cgroup/container limits remain the hard upper boundary.
2. Automatic RAM and VRAM reserves remain non-reducible. Explicit
   `MEMORY_PRESSURE_RESERVE_GIB` and `GPU_MEMORY_RESERVE_GIB` values may only
   raise them.
3. An explicit `SEGMENTATION_CHUNK_MINUTES` remains the initial chunk request.
   It may be shortened by activity policy or pressure recovery but never
   enlarged by either.
4. An explicit `WHISPER_MODEL` remains subject to the normal fresh admission
   check. The activity setting never downgrades or upgrades it.
5. `CONCURRENT_TRANSCRIPTIONS` is not changed by an activity profile. Public
   v0.5 profiles continue to default to one worker.
6. `MEMORY_PRESSURE_YIELD` remains accepted as a compatibility setting only
   when true. False is rejected because neither run mode is permitted to
   bypass genuine memory-pressure protection.
7. An explicit `MODEL_CLEANUP_DELAY` remains authoritative for ordinary idle
   cleanup. Dedicated mode only guarantees residency while queued work exists;
   it does not make the model permanently un-unloadable.

## Architecture and ownership

A small, standard-library-only `subgen_core/execution_policy.py` module owns
parsing and the immutable resolved policy. This is a justified new owner rather
than more conditionals in the already large `subgen_override.py` and
`resource_management.py` modules.

- `subgen_override.py` reads the environment once and stores the resolved
  policy on the runtime object.
- `execution_policy.py` owns the accepted names, defaults, profile constants,
  conflict validation, and status-safe representation.
- `resource_management.py` remains the sole owner of capacity, reserves,
  admission, pressure classification, recovery, and chunk shrinking. It
  consumes the resolved working-budget fraction and automatic chunk ceiling.
- `model_runtime.py` remains the sole owner of model load, release, and
  admission. It consumes whether optional application-priority observations
  are enabled and whether queued-work residency is preferred.
- `transcription.py` remains the owner of file/chunk sequencing and applies the
  resolved inter-file cadence without changing atomic join behavior.
- `human_progress.py` formats the selected policy for logs; it does not make
  policy decisions.

There is no alternate controller, second reserve calculation, or caller-side
fallback.

## Status and human-readable logs

`/status` adds a stable `execution_policy` object beneath
`resource_management` containing:

- `activity`
- `run_mode`
- `working_budget_percent`
- `automatic_chunk_ceiling_minutes`
- `inter_file_delay_seconds`
- `priority_signal_enabled`
- `adaptive_segmentation_enabled`

Startup logs include a compact block such as:

```text
Subgen activity: passive — cautious use of spare capacity
Run mode: adaptive (recommended) — other workloads take priority
Available memory: 12.0 GiB
Memory reserved for system/priority tasks: 4.0 GiB
Working budget selected: 50% of safe post-reserve capacity
Model suitable: large-v3
Optional priority signal: enabled
```

Existing per-file progress remains the primary operational narrative:

```text
Starting file: Movie Name.mkv
File split into 18 planned chunks
Chunk 1/18 — 0% complete
Memory pressure detected; preserving completed chunks and retrying this chunk
Joining chunks 1–18
Chunks joined
File finished successfully
```

Yield messages identify whether the cause is the optional priority signal,
host/cgroup pressure, PSI, GPU reserve, stale GPU telemetry, or an allocation
failure. They must not label every cause as generic “higher-priority memory
pressure.” This is required for human fault-finding.

## Error handling and safety invariants

- A yielded chunk is never committed and is retried from its original media
  cursor.
- Completed chunks remain isolated from the current retry and are joined only
  after every chunk succeeds.
- A subtitle is published with the existing atomic replace path only after a
  successful join.
- A mode or activity parsing error prevents startup rather than silently
  selecting another policy.
- A dedicated-mode configuration conflict prevents startup rather than
  silently disabling a safety input.
- A normal memory or priority yield never creates a failed-file marker.
- Media deletion remains a separate, optional failure-monitor policy and is
  not enabled or altered by either setting.
- Valid media without audio remains skipped, not deleted. Only media proven
  unusable by both FFprobe and PyAV is eligible for the private optional
  invalid-media deletion path.

## Compatibility boundary

The environment remains the public configuration surface and Compose remains
the packaged deployment path. All supplied Compose profiles expose the two new
settings with `balanced` and `adaptive` defaults. Existing installations that
omit them receive those defaults.

Uploaded `/asr` and OpenAI-compatible buffer requests do not enter local-file
segmentation and retain their current behavior. Plex/Jellyfin/Emby webhooks,
MQTT inventory sensors, marker schema v1, subtitle naming, and existing atomic
publication remain unchanged.

This feature changes runtime policy and therefore changes the candidate image
identity. The existing private soak remains useful evidence for the underlying
segmentation path, but it cannot qualify the finished image. The image that
contains these settings must complete its own focused regression and private
72-hour soak before GitHub, GHCR, tag, or release publication.

## Verification and acceptance

Local tests must cover:

- strict parsing, normalization, defaults, and invalid values;
- all six activity/run-mode combinations;
- dedicated-mode rejection on canonical shared CUDA, Task 11B, and configured
  priority input;
- unchanged hard reserves, admission thresholds, PSI thresholds, recovery
  hysteresis, allocation handling, and one-worker default across profiles;
- the 4/6/9/12/16/24/32/64/128 GiB capacity matrix;
- highest-quality safe automatic model selection being independent of activity;
- activity-dependent chunk planning with the five-minute retry floor;
- adaptive priority yield versus dedicated priority bypass;
- genuine host/cgroup/GPU pressure yield in both modes;
- model residency between queued files in dedicated mode and cleanup after the
  queue becomes idle;
- atomic multi-chunk join, no partial output, and no marker on ordinary yield;
- `/status`, startup logs, and reason-specific yield logs;
- all supplied Compose profiles and `.env.example` exposing the public
  defaults exactly once.

No GitHub-hosted runner is used for development or qualification. Heavyweight
and cross-hardware tests run locally or on the approved simulator. The private
Frigate candidate uses `passive/adaptive`, deletion off, the explicit 4 GiB RAM
reserve, the explicit 8 GiB VRAM reserve, one worker, and the five-minute shared
GPU chunk cap. Acceptance requires 72 uninterrupted hours, representative
multi-chunk success, no candidate or Frigate restart/OOM drift, no CUDA/Xid
fault, no partial subtitle, no false marker, and healthy camera/detector/
embedding evidence.

## Non-goals

- Changing the highest-quality-safe automatic model rule.
- Increasing concurrency automatically.
- Letting any profile lower mandatory reserves or disable segmentation.
- GPU kernel preemption or forcibly reclaiming memory from another process.
- Persisting individual unfinished Whisper chunks across a container restart.
- Adding activity controls to Home Assistant in v0.5; MQTT remains status-only.
- Publishing before the corrected private soak and hardware matrix pass.

## Design alternatives considered

1. One combined `SUBGEN_MODE` enum was rejected because it conflates throughput
   preference with shared-versus-dedicated machine ownership.
2. Only exposing the existing low-level knobs was rejected because users would
   need to understand reserve equations and could create contradictory states.
3. Two orthogonal settings was selected because it keeps the human choice
   simple while reusing the existing safety owners.

## Design records

### Task intent

- Outcome: users can choose cautious, balanced, or maximum safe throughput and
  adaptive or dedicated execution without disabling long-file protection.
- Success evidence: deterministic policy tests, hardware matrix, readable
  status/logs, and a clean private soak of the finished image.
- Stop condition: no public push or release until all acceptance evidence is
  complete.
- Primary risks: ambiguous precedence, accidental reserve weakening, priority
  input silently ignored, and invalidating the existing soak identity.

### Baseline read set and usage

- Required: the approved memory-aware segmentation design, v0.5 plan,
  `subgen_override.py`, `resource_management.py`, `model_runtime.py`,
  `transcription.py`, `human_progress.py`, supplied Compose profiles, current
  configuration documentation, and current tests.
- Acknowledged: the live Frigate deployment is shared CUDA and therefore must
  remain adaptive.
- Missing: none for the approved design.
- Decision: continue to implementation planning after written-spec review.

### Impact statement

- Affected layers: environment parsing, immutable runtime policy, chunk
  planning, priority consumption, queued-work model residency, status/logging,
  Compose packaging, tests, README, and release notes.
- Canonical owners remain explicit; the new policy module maps user intent but
  does not duplicate resource decisions.
- Compatibility: omitted settings receive safe defaults; lower-level overrides
  remain bounded by the invariants above.
- ADR signal: public configuration contract and dedicated/shared-host runtime
  boundary. The accepted ADR should be recorded only after implementation and
  qualification evidence exist.
