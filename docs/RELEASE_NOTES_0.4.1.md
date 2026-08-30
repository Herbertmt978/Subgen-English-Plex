# Subgen English for Plex 0.4.1

Version 0.4.1 is a deletion-safety compatibility patch for NFSv4 media libraries. It keeps the v0.4.0 first-failure exact-generation marker behavior and allows the existing secure quarantine workflow to operate when NFS inherits a set-group-ID bit on a newly created owner-only directory.

## What changed

- A quarantine owned by the monitor service account is private when its ordinary access bits are exactly `0700`, even if NFS adds a special set-group-ID bit and reports `2700`.
- Quarantines with any group or other access remain blocked.
- Directory ownership, no-follow traversal, exact file identity, allowed quarantine contents, durable intent, tombstone, recovery, and marker-before-delete checks are unchanged.
- Public deletion remains disabled. Operators must still opt in explicitly.

## Back up before upgrading

Back up the active Compose file and overrides, `monitor.env`, the installed monitor and shared safety modules, the complete monitor state directory, and the current immutable image digest. Do not place the backup beneath the media tree.

## Upgrade

1. Replace the host monitor's `subgen_ops_safety.py` with the v0.4.1 file alongside `monitor_subgen_failures.py`.
2. Pull `ghcr.io/herbertmt978/subgen-english-plex:v0.4.1` or pin its verified immutable digest.
3. Validate the selected Compose file, recreate Subgen, and restart the monitor.
4. Confirm `/status` returns HTTP 200, the monitor heartbeat advances, the service account owns its state files, and the configured marker/delete thresholds are unchanged.

## Disposable smoke test

Use a temporary media tree, never a production title. Create an owner-only test directory beneath a set-group-ID parent, trigger one qualifying failure with marker/delete threshold `1`, and confirm the marker audit precedes deletion. Also verify that a deliberately group-accessible quarantine is rejected and the candidate remains.

## Rollback

Stop the monitor, restore the backed-up v0.4.0 monitor/shared modules and prior image digest, recreate the previous container, and restart the previous monitor. Retain the marker registry and failure state as audit evidence. On NFSv4, v0.4.0 may continue to mark and skip a failed generation while blocking its deletion.

## Known boundaries

- This patch does not weaken ownership or ordinary access-bit checks; it only ignores POSIX special mode bits when evaluating whether a directory grants group or other access.
- NFS export identity mapping must still make the monitor service account the reported quarantine owner.
- No Sonarr/Radarr API search is triggered. Replacement continues to depend on the operator's existing *Arr behavior.
- Do not validate deletion by corrupting or deleting real media.

See the [v0.4.0 marker release notes](./RELEASE_NOTES_0.4.0.md) and [full changelog](../CHANGELOG.md).
