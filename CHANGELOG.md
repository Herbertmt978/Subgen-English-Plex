# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Changed

- Compose profiles expose `SKIP_STARTUP_SCAN` through `.env` with a catch-up-safe public default of `False`; watcher-only installations can now persist `True` without a temporary Compose file.

## [0.4.1] - 2026-08-30

### Fixed

- Owner-only delete quarantines on NFSv4 are accepted when the filesystem inherits a set-group-ID bit (`2700`); group or other access remains rejected.
- First-failure deletion no longer remains blocked solely because a private NFS directory reports harmless special mode bits in addition to `0700` access permissions.

## [0.4.0] - 2026-08-30

### Added

- A versioned, bounded `subgen_failure_markers.json` contract that binds an exact case-preserving `/media` path to device, inode, size, modification time, and change time.
- First-failure marker creation and audit in the host monitor, with exact processing-error and `SIGSEGV` attribution.
- Read-only marker-state mounts and matching runtime settings in the source, packaged CPU, and packaged GPU Compose profiles.
- Focused tests for marker schema safety, marker-before-delete ordering, replacement self-unblocking, fail-open registry reads, pre-probe enforcement, and package/workflow parity.

### Changed

- Public marker creation and skipping default to the first qualifying failure; public media deletion remains disabled.
- All file enqueue paths now check an exact-generation marker in the canonical media queue before AV/FFmpeg probing. Watchdog and direct-file scanner branches no longer pre-probe media.
- Packaged CPU and GPU profiles default to `v0.4.0`.
- GitHub test and image workflows are manual-only. Maintainers run tests and image builds locally or on the idle simulator PC to avoid hosted-runner usage.

### Fixed

- A bad media generation can no longer trigger full-folder failure churn indefinitely when deletion is blocked: its durable marker suppresses only that generation.
- Sonarr/Radarr replacements at the same path are no longer suppressed by path-only evidence; a changed file identity proceeds normally.
- Marker write or registry validation failures block an enabled deletion attempt for that event, preserve corrupt registry evidence, and remain non-blocking for transcription reads.
- Enabled configurations with a delete threshold above the marker threshold are rejected because the later delete count would be unreachable after the marker becomes active.

## [0.3.0] - 2026-07-12

### Added

- Canonical `subgen_core` owners for queueing, optional Plex/Jellyfin clients, media policy and scanning, model lifecycle, and transcription.
- Standalone contracts for direct files, startup folders, monitoring, `/batch`, `/asr`, route inventory, and the executable compatibility bootstrap.
- Shared Linux filesystem safety primitives for private state, file-generation fingerprints, durable delete intents, and crash-recoverable same-filesystem quarantine.

### Changed

- `subgen_override.py` is now the executable FastAPI composition root and compatibility facade instead of the canonical owner of extracted algorithms.
- Optional Plex and Jellyfin server/token settings default to blank; explicitly configured integrations remain available.
- Packaged and source-bind documentation now covers the complete modular runtime, an isolated end-to-end subtitle smoke test, direct single-file inputs, optional integrations, model planning, persistent files, webhook trust boundaries, ops-repository migration, architecture ownership, and contributor test-patching guidance.

### Fixed

- Packaged images and the read-only source profile include `subgen_core`, and GHCR publishing watches package changes.
- The packaged build and source runtime use the same immutable Subgen 2026.06.6 upstream-base digest, verified with this repository's 2026.07.1 override, instead of silently following the mutable upstream `latest` tag.
- Packaged CPU and GPU Compose profiles default to the versioned `v0.3.0` GHCR image, with an explicit `SUBGEN_IMAGE` override for controlled testing.
- Repeated repair runs suppress unchanged candidate events and stale re-deletion, retry failed audit writes from an ordered atomic queue, preserve original event timestamps, and bound locked event-log rotation to a safe 5 MiB single backup.
- Monitor and repair deletion now reject symbolic-link and traversal candidates, reset thresholds for same-path replacements, persist and directory-sync five-field file identities plus operation tokens, quarantine before unlink, recover interrupted operations exactly once, and honour current deletion kill switches during recovery.
- Operational state and logs reject unsafe state-directory leaves, symlinks, hardlinks, foreign ownership, and group/world-writable state directories; supplied systemd services use an owner-only umask.
- Delayed model cleanup now rechecks queue/direct activity while holding the model load lock, preventing a newly arriving inference from racing model unload.
- Legacy `/asr` requests for one video now keep distinct queue identities across task, language, output, word timestamps, prompt, encoding choice, and uploaded audio.

## [0.2.0] - 2026-07-10

### Added

- Current Subgen 2026.07.1 runtime and a comprehensive regression suite.
- Conservative CPU and NVIDIA deployment profiles configured through `.env`.
- A model/hardware guide for multilingual-to-English translation.
- Exact-path structured lifecycle monitoring, repeated-crash reporting, optional API authentication, CI, contributing guidance, and a security policy.

### Changed

- The public standard is now Whisper `medium`, one transcription, four threads, a 10 GB memory ceiling, and loopback-only HTTP binding.
- NVIDIA users explicitly opt into `large-v3` with a matching 16–20 GB container allocation.
- Failure repair is report-only unless deletion is explicitly enabled.
- Detection and transcription use the same selected audio track, including when media metadata incorrectly claims the audio is English.

### Fixed

- Translation output is consistently named as English.
- Worker exceptions propagate into structured failure handling.
- Duplicate basenames cannot be confused during cleanup.
- Direct API requests cannot bypass the configured inference concurrency limit.
- `SKIP_VIDEO_EXTENSIONS` is honoured by startup scans and file monitoring.
- OpenAI-compatible requests with different prompts no longer share a queued result.
- Prompted legacy `/asr` requests keep distinct identities across prompt and video-path changes.
- Structured failure events retain the real mapped media path when queue keys are synthetic.

## [0.1.0] - 2026-04-26

- Initial public release.

[Unreleased]: https://github.com/Herbertmt978/Subgen-English-Plex/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/Herbertmt978/Subgen-English-Plex/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Herbertmt978/Subgen-English-Plex/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Herbertmt978/Subgen-English-Plex/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Herbertmt978/Subgen-English-Plex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Herbertmt978/Subgen-English-Plex/releases/tag/v0.1.0
