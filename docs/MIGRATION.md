# Migrating from subgen-frigate-ops

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
