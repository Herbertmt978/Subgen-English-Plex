# Subgen English for Plex 0.4.0

Version 0.4.0 stops one bad media-file generation from repeatedly crashing or churning a full-folder scan. The monitor records an exact generation before optional cleanup, and Subgen checks that evidence before opening the file.

## Highlights

- Marker creation and skipping are enabled publicly after the first qualifying processing error or exactly attributed `SIGSEGV`.
- A marker contains the exact case-preserving `/media` path and five-field file identity. It does not contain host paths, tokens, subtitle content, or credentials.
- An exact identity match is skipped before AV/FFmpeg probing. A Sonarr/Radarr replacement at the same path has a different identity and proceeds normally.
- Missing, malformed, oversized, unsafe, or unreadable registry state fails open for transcription and never authorizes deletion.
- The monitor writes and audits the marker before any enabled exact-file deletion. Marker persistence failure blocks deletion for that event.
- Public deletion remains off. The project owner's Plex deployment explicitly uses marker threshold `1` and delete threshold `1`; existing three-failure deletion users use `3` for both.
- Source, packaged CPU, and packaged GPU profiles share the same read-only state mount and v0.4.0 runtime contract.
- GitHub workflows no longer run automatically. Release tests and image builds are performed locally or on the idle simulator PC.

## Back up before upgrading

Back up all of the following before changing a running install:

- `.env`, `monitor.env`, the active Compose file and overrides;
- monitor script, shared modules, systemd unit/timer files, and `SUBGEN_STATE_DIR`;
- the current image tag and immutable digest plus the command used to recreate it; and
- any deployment-specific memory, CPU, UID/GID, mount, notification, and deletion settings.

Do not put the backup beneath the media tree.

## Upgrade

1. Set `SUBGEN_STATE_DIR` in `.env` and ensure every Compose profile mounts it read-only at `/opt/subgen/monitor`.
2. Set `SKIP_MARKED_FAILED_FILES=true`.
3. Run the monitor and Subgen container with the same numeric UID, or provide equivalent read/traverse permissions for the owner-only marker registry.
4. In `monitor.env`, set `AUTO_MARK_FAILED_FILES=true` and choose thresholds before starting:
   - public/report-only: marker `1`, deletion off;
   - existing three-failure deletion: marker `3`, delete `3`;
   - explicit Plex/*Arr first-failure cycling: marker `1`, delete `1`.
5. Validate the selected Compose file, recreate Subgen from the v0.4.0 image, restart the monitor, and confirm HTTP health, monitor heartbeat, registry readability, and normal scan progress.

An enabled delete threshold greater than the marker threshold is rejected. Once a marker is active, the same generation is not reopened, so it cannot reach a later delete count.

## Disposable smoke test

Use temporary media outside the real library:

1. trigger a marker-only first failure and confirm the file remains;
2. confirm the v0.4.0 reader reports the original identity as `matched` and the media probe is not called;
3. replace the temporary file at the same path and confirm the reader reports `stale` and processing continues; and
4. in a separate disposable directory, enable marker/delete threshold `1` and confirm the marker audit precedes exact-file deletion.

Do not deliberately corrupt or delete production media to test the release.

## Rollback

Stop the new monitor, restore the backed-up Compose/override, prior image digest, monitor scripts/modules/unit/env, and recreate the previous container. Restart the previous monitor and recheck HTTP health, scan progress, OOM/restart state, and memory pressure. Keep `subgen_failure_markers.json` as audit evidence; v0.3.0 ignores it. Rollback never requires deleting media.

## Known boundaries

- Marker identity depends on the host and container seeing matching device/inode/size/time metadata. The isolated deployment smoke verifies this before production use.
- Registry reads are intentionally fail-open for transcription; warnings are rate-limited until registry metadata changes.
- Marker creation is owned only by the host monitor. Disabling it stops new markers but does not erase existing evidence.
- No Sonarr/Radarr API search is triggered. Replacement depends on the operator's existing *Arr behavior.
- Ollama coordination, model selection, subtitle naming, queue concurrency, and the existing directory `.subgen_skip` feature are unchanged.

See the [migration guide](./MIGRATION.md#upgrading-from-030-to-040) and [full changelog](../CHANGELOG.md).
