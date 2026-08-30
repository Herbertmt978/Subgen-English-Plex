# Migration guide

## Upgrading from 0.3.0 to 0.4.0

Version 0.4.0 adds a narrow shared failure-marker registry. The monitor writes an exact case-preserving `/media` path plus device, inode, size, modification time, and change time before optional deletion. Subgen reads the registry before media probing. Only the exact marked generation is skipped; a Sonarr/Radarr replacement at the same path self-unblocks. Missing, malformed, oversized, unsafe, or unreadable registry state processes normally and never authorizes deletion.

1. Back up `.env`, `monitor.env`, the active Compose file or override, monitor service/unit files, and the complete `SUBGEN_STATE_DIR`. Record the current image tag or digest for rollback.
2. Add `SUBGEN_STATE_DIR` and `SKIP_MARKED_FAILED_FILES=true` to `.env`. Every v0.4.0 Compose profile mounts that directory read-only at `/opt/subgen/monitor`.
3. Ensure the host monitor and container `PUID` use the same numeric UID, or provide equivalent read/traverse access. The private state directory is normally `0700` and the registry is `0600`.
4. Add `AUTO_MARK_FAILED_FILES=true` to `monitor.env` and choose the thresholds before starting the new monitor:
   - public/report-only default: marker `1`, deletion disabled, delete threshold retained at `3`;
   - existing three-failure deletion: marker `3`, delete `3`;
   - explicit Plex/*Arr first-failure cycle: marker `1`, delete `1`.
5. Never configure an enabled delete threshold above the marker threshold. Once Subgen sees the marker it skips that generation, so no later failure can increment an unreachable delete count; the monitor rejects that configuration.
6. Validate the selected Compose profile, start Subgen and the monitor, and confirm `/status`, monitor heartbeat, registry ownership/readability, and a normal production scan. Do not test deletion against real media.
7. For a disposable smoke, mark a temporary file, prove the original identity is skipped, replace it at the same path, and prove the new identity proceeds.

Rollback restores the backed-up Compose/override, prior image digest, monitor script/module/unit/env, and prior service state. Retain `subgen_failure_markers.json` as audit evidence; v0.3.0 ignores it. Rollback must not delete media.

## Migrating from subgen-frigate-ops

`Subgen-English-Plex` now owns the reusable monitoring and repeated-offender recovery features that previously lived in `subgen-frigate-ops`. Plex and the *Arr stack remain optional: startup files, startup folders, `/batch`, and `/asr` continue to work with every media-server setting blank.

## Behaviour mapping

| Previous ops behaviour | Current behaviour |
| --- | --- |
| Follow Subgen logs and record crash candidates | `monitor_subgen_failures.py` records structured exact paths and file-generation fingerprints. |
| Periodically handle repeated offenders | `repair_subgen_failures.py` runs report-only by default and supports an explicit exact-file delete action. |
| Host-local service and timer | Versioned units live under `systemd/`; edit the documented user, group, and paths before installation. |
| Empty `.srt` skip markers | Retired. Empty subtitle files can mislead players and library tools. Legacy empty markers are removed only while handling their exact offender. |
| `.subgen.repair.json` media sidecars | Retired. Durable state and ordered audit events stay in the private operational state directory. |
| Path-only failure counts | Replaced by case-preserving, file-generation-scoped evidence. A replacement at the same path starts a new threshold. |
| Direct unlink after validation | Replaced by a persisted operation token and private same-filesystem quarantine before unlink. |

## Upgrade sequence

1. Back up the current Compose file, `.env`, `monitor.env`, and monitor state directory.
2. Install the complete successor repository at one path; the monitor and repair scripts both require `subgen_ops_safety.py`.
3. Preserve deployment-local media mounts, model, GPU, memory, notification, Plex, and explicit deletion settings. Do not replace a working host Compose file wholesale with a public example.
4. Create `SUBGEN_STATE_DIR` on a local filesystem, owned by the service account and not group/world writable (mode `0700` is recommended).
5. Start the monitor with both deletion controls off. Confirm exact paths and allow it to reset unverified legacy path-only counts before fingerprinting the current files.
6. Run the repairer in `report` mode and inspect its state/events.
7. Enable deletion only if the operator still wants exact repeated offenders removed after the configured threshold.
8. Verify a direct file or folder job without Plex, then verify any configured Plex webhook separately.
9. Remove old service/timer definitions only after the replacement services and a stability window pass.

Do not copy empty skip markers or `.subgen.repair.json` sidecars into the new design. They are not inputs to the successor. Keep the old repository history available until the released successor has been deployed and verified; archival is safer than deletion.
