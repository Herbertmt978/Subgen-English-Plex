# Migration guide

## Upgrading from 0.4.1 to 0.5.0

Version 0.5.0 adds automatic resource/model admission, exact-runtime
ModelEnvelope evidence, adaptive local-file segmentation and pressure retry,
typed dual-validator media classification, and a single conservative deletion
owner. Long files no longer require one duration-growing inference allocation:
Subgen processes one capacity-derived 5-to-30-minute window at a time, retries
the same source cursor toward five minutes under pressure, merges timestamps
and ownership, and atomically publishes one final subtitle. Segmentation cannot
make model weights fit; admission still has to cover the fixed selected model.

### Back up and record the rollback point

Before changing configuration, stop any automated upgrade and record:

- `.env`, `monitor.env`, all Compose/overlay files, and installed systemd units;
- the complete private `SUBGEN_STATE_DIR` with ownership and modes;
- the model cache and exact active image tag, digest, OCI configuration digest,
  and ordered rootfs layer diff IDs;
- the owner/mode metadata of `/var/lib/subgen/model-envelopes/v1` plus
  `catalog.json` and `image-identity.json` beneath it, if present; and
- whether startup scanning, marker skipping, monitoring, and deletion were
  enabled.

Keep the backup outside the media tree. Set both deletion inputs false before
the first v0.5.0 start:

```dotenv
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
```

`AUTO_DELETE_FAILED_FILES` is only a deprecated compatibility alias through
0.5.x and can enable only the same invalid-media path. It no longer authorizes
generic, crash, or inference-failure deletion.

### Adopt the v0.5.0 public defaults

Merge the new example values deliberately instead of replacing a working host
configuration wholesale:

```dotenv
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
GPU_MEMORY_RESERVE_GIB=auto
MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json
MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

The public memory limit remains 10 GiB hard/no-swap and concurrency remains
one. Generic CPU auto ceilings select no higher than `small` at 4 or 6 GiB;
9 GiB can admit `medium` only when fresh cgroup/host headroom still covers it.
Generic CUDA tiers are fallback ceilings only. Exact matching ModelEnvelope
evidence for the immutable runtime is authoritative, subject to fresh
stabilized allocatable-VRAM admission after reserves. An explicit recognized
model stays fixed for the file and is never silently downgraded.

`SEGMENTATION_ENABLED=False` opts local-file jobs out of segmentation. Uploaded
`/asr`, `/detect-language`, and OpenAI-compatible audio requests remain whole-
request and unsegmented. `/batch` is different: it walks local paths and queues
them through the normal local-file pipeline, so those jobs use segmentation
when it is enabled. Model admission, validation, marker checks, pressure
handling, and deletion safety remain active. Invalid chunk, host-reserve, and
GPU-reserve settings are rejected at startup instead of being guessed.

### Install exact external evidence or choose fallback deliberately

The supplied base Compose profiles mount no host evidence. Ordinary public
deployments can upgrade directly with those base files: missing evidence logs
one bounded reason and uses conservative fallback without host artifact setup.
An exact-evidence or canonical deployment combines the selected base with
`docker-compose.model-envelopes.yml`, which mounts the owner-only parent plus
both exact leaves read-only:

| Exact host path | Exact container path |
| --- | --- |
| `/var/lib/subgen/model-envelopes/v1` | `/opt/subgen/model-envelopes` |
| `/var/lib/subgen/model-envelopes/v1/catalog.json` | `/opt/subgen/model-envelopes/catalog.json` |
| `/var/lib/subgen/model-envelopes/v1/image-identity.json` | `/opt/subgen/model-envelopes/image-identity.json` |

Use a mode-`0700` parent and regular mode-`0600` files owned by the numeric UID
seen as the Subgen runtime EUID, normally the configured `PUID`. The parent bind
preserves that metadata for strict container-side validation; the overlay's
long bind syntax sets `create_host_path: false` for the parent and both leaves.
The identity must use `subgen.model-envelope.identity/v1` and match the candidate
image's OCI configuration digest plus ordered rootfs layer diff IDs. The catalog
must use `subgen.model-envelope.catalog/v1` and match the exact runtime, model
revision, compute/device policy, decoder options, concurrency, and chunk policy.
Symlinks, group/other access, malformed or unknown fields, integrity failures,
duplicate matches, and identity/runtime/policy mismatches are rejected.

Only the owner-operated `/subgen/profile_model_envelopes.py` writes a distinct
staged catalog. Run one explicit model in each clean isolated process for at
least three cold cycles, re-inspect OCI identity immediately before profiling
and every overlay-enabled automatic runtime start, review the staged output,
and install it atomically. The profiler never rewrites the identity artifact or
live catalog. Public auto uses conservative fallback when exact evidence is
missing or unusable. Canonical shared CUDA instead requires
`CANONICAL_SHARED_CUDA=True`, exact matching evidence, and a positive audited
`GPU_MEMORY_RESERVE_GIB`; `GPU_MEMORY_RESERVE_GIB=auto` is prohibited there.

### Migrate failure state safely

The v0.5.0 monitor is the only automatic deletion decision owner. Optional
deletion requires both bounded typed FFprobe and isolated PyAV to classify the
unchanged current media generation as `invalid_format`, then durable marker
write/re-read and exact descriptor-relative unlink. Silent, indeterminate,
timeout/permission, validator crash, disappearance, generic/inference/resource,
OOM, pressure, SIGSEGV, log-regex, legacy-intent, and stale-replacement cases
remain.

`SUBGEN_REPAIR_ACTION=delete` is still parsed for migration, but always reports
and emits a policy warning. Repair never deletes media or empty subtitle
markers. Start the monitor with deletion off so old path-only, untyped, and
repair intents are policy-blocked and preserved as evidence while current
generations are fingerprinted. A replacement at the same path starts a new
generation and self-unblocks.

### Validate, upgrade, and smoke

1. Validate the selected v0.5.0 base profile with `docker compose -f <base> config --quiet`.
2. For ordinary fallback, keep all three evidence binds absent by using only the
   base. For reviewed exact evidence, validate the parent plus both leaves with
   `docker compose -f <base> -f docker-compose.model-envelopes.yml config --quiet`
   and pass the Linux metadata/exact-load gate in the installation guide.
3. Start the runtime with deletion off and confirm `/status` readiness. The
   endpoint continues to report stable overlaid runtime version `2026.07.1`;
   the project, image, and release version is `0.5.0`.
4. Use a disposable short supported file outside the library to prove one
   non-empty subtitle is atomically published. Replace it at the same path and
   prove stale marker state does not block the replacement.
5. Exercise a longer disposable local file and inspect window progress,
   pressure retry, cgroup/OOM state, and absence of partial output.
6. Test deletion, if it is ever enabled, only with disposable typed-invalid
   fixtures and both kill switches. Never use production media as the test.

The public rollback path restores the backed-up v0.4.1 image/Compose,
configuration, systemd units, and monitor state with deletion disabled. Preserve
v0.5.0 marker and event evidence; v0.4.1 ignores fields it does not own. Do not
use a deployment-specific Frigate v0.3.0 backup as the public rollback.

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

`Subgen-English-Plex` now owns the reusable monitoring, typed validation, and
generation-scoped marker features that previously lived in
`subgen-frigate-ops`. Plex and the *Arr stack remain optional: startup files,
startup folders, `/batch`, and `/asr` continue to work with every media-server
setting blank. There is no Sonarr/Radarr API integration and no Ollama lifecycle
coordination.

## Behaviour mapping

| Previous ops behaviour | Current behaviour |
| --- | --- |
| Follow Subgen logs and record crash candidates | `monitor_subgen_failures.py` records structured exact paths, current file-generation fingerprints, and typed validator evidence. Generic or crash candidates never authorize deletion. |
| Periodically handle repeated offenders | `repair_subgen_failures.py` produces reports and migration evidence only. Its legacy `delete` input is accepted but policy-blocked. |
| Host-local service and timer | Versioned units live under `systemd/`; edit the documented user, group, and paths before installation. |
| Empty `.srt` skip markers | Retired. Empty subtitle files can mislead players and library tools. Legacy empty markers are preserved for deliberate manual review; repair never removes them. |
| `.subgen.repair.json` media sidecars | Retired. Durable state and ordered audit events stay in the private operational state directory. |
| Path-only failure counts | Replaced by case-preserving, file-generation-scoped evidence. A replacement at the same path starts a new threshold. |
| Direct unlink after validation | Limited to the live monitor after dual typed-invalid evidence, durable marker write/re-read, a persisted operation token, and private same-filesystem quarantine before unlink. |

## Upgrade sequence

1. Back up the current Compose file, `.env`, `monitor.env`, and monitor state directory.
2. Install the complete successor repository at one path; the monitor and repair scripts both require `subgen_ops_safety.py` and `subgen_failure_markers.py`.
3. Preserve deployment-local media mounts, model, GPU, memory, notification, Plex, and explicit deletion settings. Do not replace a working host Compose file wholesale with a public example.
4. Create `SUBGEN_STATE_DIR` on a local filesystem, owned by the service account and not group/world writable (mode `0700` is recommended).
5. Start the monitor with both deletion controls off. Confirm exact paths and allow it to policy-block and preserve unverified legacy path-only/untyped intents before fingerprinting current files.
6. Run the repairer in `report` mode and inspect its state/events. A legacy `delete` setting remains report-only and must not be treated as cleanup authority.
7. If the operator later enables deletion, prove on disposable fixtures that both typed validators conclude `invalid_format` for the unchanged generation. The live monitor remains the only deletion owner.
8. Verify a direct file or folder job without Plex, then verify any configured Plex webhook separately.
9. Remove old service/timer definitions only after the replacement services and a stability window pass.

Do not copy empty skip markers or `.subgen.repair.json` sidecars into the new design. They are not inputs to the successor. Keep the old repository history available until the released successor has been deployed and verified; archival is safer than deletion.

## Ashby's Frigate deployment boundary

This is not a public default and is not changed merely by completing the public
v0.5.0 migration. The deployment shares an RTX 3090 with Frigate and Ollama;
both remain higher priority and Subgen never stops, reconfigures, or coordinates
their lifecycle. Plex-hosted Subgen remains retired.

That priority is an operating rule rather than CUDA preemption. Subgen can
yield host/cgroup/GPU memory at safe callbacks, but it does not read Frigate FPS
or interrupt an already-running CUDA kernel. The five-minute Frigate policy
shortens the work between those boundaries without guaranteeing zero camera
impact.

The candidate gate combines the GPU base with the supplied ModelEnvelope
overlay and uses `WHISPER_MODEL=auto`, `SEGMENTATION_CHUNK_MINUTES=5`, startup
scanning on, the exact read-only catalog and identity artifacts, and a positive
audited `GPU_MEMORY_RESERVE_GIB`; `GPU_MEMORY_RESERVE_GIB=auto` is prohibited
for that shared-CUDA reserve. This five-minute setting is Frigate-only and
evidence-bound. Public profiles remain `auto`, and the profiler catalog must be
created with the same five-minute policy used by the candidate and production
runtime.
The eventual production boundary is 10 GiB hard/no-swap only after constrained
evidence passes. A 12 GiB run is profiler-only evidence and has no production
authority. The local first-failure policy may set
`AUTO_DELETE_INVALID_MEDIA=true` with threshold one only for current dual-
typed-invalid media; `AUTO_DELETE_FAILED_FILES=false` stays false, monitor is
the sole deletion owner, and repair remains inactive/report-only.

Rollback for that deployment restores its separately preserved v0.3.0
Compose/config, model cache, operational state, and OCI identity. That rollback
must be validated independently and must not be substituted for the public
v0.4.1 rollback described above.
