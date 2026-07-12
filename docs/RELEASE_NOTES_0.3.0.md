# Subgen English for Plex 0.3.0

Version 0.3.0 makes the runtime easier to maintain without changing its core purpose: generate English subtitles locally, translating non-English speech with a multilingual Whisper model.

## Highlights

- Splits queueing, optional Plex/Jellyfin clients, media policy/scanning, model lifecycle, and transcription into canonical `subgen_core` modules. `subgen_override.py` remains the executable FastAPI composition root and compatibility facade.
- Keeps direct file paths, startup folders, `/batch`, and `/asr` fully usable with Plex, Jellyfin, Emby, Bazarr, and the *Arr stack absent.
- Preserves translation-to-English, selected-audio-track consistency, English subtitle naming, prompt-aware task identities, and configured inference concurrency.
- Synchronizes delayed model cleanup with model loading, and prevents same-video `/asr` requests with different inference or output options from sharing the wrong result.
- Defaults public installs to multilingual Whisper `medium`, CPU `int8`, one job, four threads, and a 10 GB memory ceiling. The documented RTX 3090 profile uses `large-v3`, CUDA `float16`, one job, and a 20 GB ceiling when host RAM permits.
- Pins the packaged and source runtime to the verified immutable upstream Subgen 2026.06.6 Linux/AMD64 manifest rather than following `latest`.
- Packages `subgen_core` in the image and mounts it read-only in the source profile. Packaged Compose profiles default to `ghcr.io/herbertmt978/subgen-english-plex:v0.3.0`.

## Safer repeated-offender handling

Deletion remains off by default. When explicitly enabled on Linux, monitor and repair now:

- keep case-distinct paths and duplicate basenames separate;
- bind thresholds to a five-field file-generation fingerprint;
- reset unverified legacy path-only counts and same-path replacements;
- persist and directory-sync a delete intent plus operation token before moving media;
- quarantine the exact directory entry privately on the same filesystem before unlink;
- recover interrupted operations without adopting a replacement path;
- pause recovery whenever the current delete kill switch is off;
- prevent an unchanged repair candidate from being deleted twice; and
- retain ordered audit events with bounded, locked rotation.

Platforms without the required descriptor-relative Linux operations fail closed and remain report-only. Operational state must be kept in a local service-owned directory that is not group/world writable.

## Documentation and migration

The quick start now proves one isolated subtitle end to end before mounting a library. The README includes conservative model/hardware planning, unauthenticated webhook boundaries, Plex Pass requirements, retained files, and host-helper prerequisites.

Reusable functionality from `subgen-frigate-ops` is now maintained here. Empty subtitle skip markers and `.subgen.repair.json` sidecars are intentionally retired; see [the migration guide](https://github.com/Herbertmt978/Subgen-English-Plex/blob/v0.3.0/docs/MIGRATION.md).

## Upgrade notes

1. Back up `.env`, `monitor.env`, the deployment Compose file, model path, and monitor state.
2. Pull the versioned image or checkout and validate the selected Compose profile.
3. Start the monitor once with deletion disabled so legacy path-only counts are reset and current files receive generation fingerprints.
4. Verify a direct standalone file and any configured media-server webhook separately.
5. Re-enable deletion only after reviewing report-only output.

The full change history is in [CHANGELOG.md](https://github.com/Herbertmt978/Subgen-English-Plex/blob/v0.3.0/CHANGELOG.md).
