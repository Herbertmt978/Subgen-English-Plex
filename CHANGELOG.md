# Changelog

All notable changes to this project are documented here.

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
- Structured failure events retain the real media path when queue keys are synthetic.

## [0.1.0] - 2026-04-26

- Initial public release.

[0.2.0]: https://github.com/Herbertmt978/Subgen-English-Plex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Herbertmt978/Subgen-English-Plex/releases/tag/v0.1.0
