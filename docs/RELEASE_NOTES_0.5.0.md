# Subgen English for Plex 0.5.0

Version 0.5.0 makes long-file transcription predictable by processing one capacity-derived window at a time, with automatic windows of 5 to 30 minutes that shrink and retry the same source interval down to five minutes under memory pressure, before merging source-time ownership and atomically publishing one subtitle file instead of letting allocations grow with duration.

Segmentation bounds duration-driven audio and inference work but cannot make model weights fit in RAM or VRAM, so loading retains a separate admission policy and every explicit model remains fixed for the whole file rather than being silently downgraded during retry.

## Highlights

- `WHISPER_MODEL=auto` evaluates multilingual models from `large-v3` down to
  `tiny`, selects one before the first load, and keeps it fixed for the process.
- Repeated measurements from the exact packaged image can authorize a model
  through an immutable `ModelEnvelope`. Generic RAM and allocatable-VRAM tiers
  are conservative fallbacks, not proof that a backend will fit.
- Three fresh exact-device samples stabilize CUDA free memory. The separate GPU
  reserve is then subtracted before selection and checked again inside the load
  gate.
- Long local files use bounded sequential extraction, five-second context
  overlap, midpoint ownership with seam-clipped timestamps, and one atomic
  SRT/LRC publication. A small timing disagreement can no longer abort the
  whole file. If two overlap decodes produce the same phrase and Subgen cannot
  prove whether it is a duplicate or real repeated speech, it keeps both rather
  than risk deleting words.
- Cooperative pressure handling abandons only the in-progress window, releases
  the model and caches at a safe boundary, waits without counting a file
  failure, and retries the same cursor with a smaller window.
- The first qualifying terminal failure still marks and skips only the exact
  file generation. A replacement at the same path proceeds normally.
- Optional deletion is narrower: only the monitor may delete unchanged current
  media after typed FFprobe and isolated PyAV both conclusively report an
  invalid format and the marker has been durably written.
- `repair_subgen_failures.py` is now report/evidence-only. Its legacy `delete`
  action is accepted for migration but never removes media or empty subtitle
  markers.

## Compared with earlier releases

- **v0.4.0** introduced first-failure markers and pre-probe skips bound to an
  exact file generation, so a bad generation stopped churning scans while a
  replacement at the same path self-unblocked.
- **v0.4.1** kept that behavior and corrected secure quarantine compatibility
  for NFSv4 filesystems that inherit a harmless set-group-ID bit on an otherwise
  owner-only directory.
- **v0.5.0** adds adaptive resource, model, and media safety: bounded sequential
  transcription, pressure yield/retry, exact-runtime model evidence, fresh
  host/GPU admission, dual-validator media classification, and monitor-only
  invalid-media deletion.

## Public defaults

The packaged profiles keep the public 10 GiB hard/no-extra-swap limit and one
transcription at a time. Their new defaults are:

```dotenv
MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json
MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json
WHISPER_MODEL=auto
SEGMENTATION_ENABLED=True
SEGMENTATION_CHUNK_MINUTES=auto
MEMORY_PRESSURE_YIELD=True
MEMORY_PRESSURE_RESERVE_GIB=auto
GPU_MEMORY_RESERVE_GIB=auto
AUTO_MARK_FAILED_FILES=true
AUTO_MARK_MIN_FAILURES=1
AUTO_DELETE_INVALID_MEDIA=false
AUTO_DELETE_FAILED_FILES=false
AUTO_DELETE_MIN_FAILURES=1
SUBGEN_REPAIR_ACTION=report
```

On CPU, 4 GiB and 6 GiB capacity profiles have a `small` fallback ceiling. A
9 GiB profile has a `medium` fallback ceiling, but only when fresh host and
cgroup admission still covers the model's nonzero load budget, margin, and
reserve. Worse current use can select a lower model or leave Subgen waiting in
`no_safe_model` recovery. CUDA applies the lower of the system-memory and
allocatable-VRAM fallback ceilings. Exact matching envelope evidence is
authoritative and may qualify a higher model; a tag, total-VRAM figure, or one
idle free-memory reading cannot.

The three base Compose profiles do not bind host ModelEnvelope evidence. That
makes the normal public path directly runnable: absent evidence produces one
bounded reason and automatic selection uses conservative fallback. Exact-
evidence or canonical deployments add `docker-compose.model-envelopes.yml`, an
opt-in read-only overlay that binds the host parent
`/var/lib/subgen/model-envelopes/v1` plus its `catalog.json` and
`image-identity.json` leaves at the three exact container paths. The parent must
be mode `0700`, each regular leaf mode `0600`, and all three must be owned by the
numeric UID seen as the Subgen runtime EUID. The parent bind preserves that
metadata for strict container-side validation, and `create_host_path: false`
prevents Docker from inventing missing host paths. The identity schema is
`subgen.model-envelope.identity/v1`; the catalog schema is
`subgen.model-envelope.catalog/v1`. Missing, unsafe, malformed, integrity-
invalid, or non-matching evidence produces one bounded reason. Public auto then
uses fallback policy; canonical shared CUDA fails closed.

Only the owner-operated `/subgen/profile_model_envelopes.py` may write a staged
catalog. It profiles one explicit model per clean process for at least three
cold cycles, uses the canonical admission owner, and never rewrites the
identity artifact or the running catalog. Before every profiler or automatic
overlay-enabled container start, compare the candidate's OCI configuration
digest and ordered rootfs layer diff IDs with the owner-only identity file. Do
not use a mutable tag or registry manifest alone as runtime identity.

## Operator-specific Frigate deployment

Ashby's Frigate deployment is not a public default and is not upgraded by this
release preparation. It shares an RTX 3090 with Frigate and Ollama, both of
which remain higher priority and are never stopped, reconfigured, or lifecycle-
managed by Subgen.

That deployment will combine the GPU base with the supplied ModelEnvelope
overlay and use `WHISPER_MODEL=auto`, the exact read-only catalog and identity
files, startup scanning enabled, a 10 GiB hard/no-swap runtime limit, and a
positive audited `GPU_MEMORY_RESERVE_GIB`; `auto` is prohibited there.
The 10 GiB limit becomes production authority only after the exact candidate
passes the isolated Frigate gate. A 12 GiB cgroup is allowed only for explicit
profiling and cannot authorize the automatic or production model.

After evidence, its intended failure policy is first-failure marking plus
`AUTO_DELETE_INVALID_MEDIA=true`, with the deprecated
`AUTO_DELETE_FAILED_FILES=false`. Deletion remains monitor-only; repair stays
inactive/report-only. The earlier Plex-hosted Subgen instance remains retired.
The preserved Frigate v0.3.0 Compose/config, model cache, and OCI identity are
its operational rollback set, distinct from the public v0.4.1 rollback below.

## Back up before upgrading

Back up the active Compose file and overlays, `.env`, `monitor.env`, installed
monitor/repair scripts and systemd units, `SUBGEN_STATE_DIR`, model cache, and
the current image tag plus immutable digest. If ModelEnvelope artifacts exist,
back up the parent owner/mode metadata and both complete owner-only files
together with the OCI config digest and ordered layer diff IDs they describe.
Keep all backups outside the media tree.

Record whether monitor and repair units are enabled and active. Set both
deletion booleans false and repair to `report` before changing versions.

## Upgrade

1. Install the complete v0.5.0 checkout; the source profile now mounts the
   profiler at the same path used by the packaged image.
2. Review `.env.example` and set the v0.5 public defaults above. Keep
   `SUBGEN_MEMORY_LIMIT=10g` unless separate evidence justifies another limit.
3. For ordinary public fallback, use the selected base profile without host
   evidence setup. For exact evidence, prepare the parent and real catalog plus
   identity leaves with strict owner/modes and add the supplied overlay. Do not
   manufacture trusted evidence from a tag or copy artifacts from a different
   image/runtime/policy.
4. In `monitor.env`, add `AUTO_DELETE_INVALID_MEDIA=false`, retain the legacy
   alias as `false`, set both thresholds to `1`, and leave repair on `report`.
5. Validate the selected base Compose file and, when applicable, its merge with
   `docker-compose.model-envelopes.yml`; recreate Subgen from v0.5.0, restart
   the monitor if used, and verify `/status`, model-decision provenance, scan
   progress, marker readability, and zero restart/OOM growth.

Invalid `SEGMENTATION_CHUNK_MINUTES`, `MEMORY_PRESSURE_RESERVE_GIB`, or
`GPU_MEMORY_RESERVE_GIB` values now reject startup instead of being silently
accepted. `SEGMENTATION_ENABLED=False` opts local files out of segmentation but
does not disable model admission, validation, markers, or pressure release and
wait. A yielded whole-file request retries as a whole file and logs that its
duration cannot shrink.

## Disposable smoke test

Use a temporary media and state root, never a production title.

1. Run base Compose validation and require HTTP 200 from `/status`. If exact
   evidence is in scope, also run the installation guide's base-plus-overlay
   Linux metadata and exact-load gate.
2. Process one short valid file and one longer synthetic file; confirm one final
   subtitle, monotonic source timestamps, and no window/temp artifacts.
3. Confirm the status/log decision reports either exact-envelope provenance or
   a bounded public-fallback reason and the expected fixed model.
4. With deletion off, present a valid silent file, an indeterminate validator
   result, and an inference failure; all must remain.
5. In a separate disposable directory only, enable
   `AUTO_DELETE_INVALID_MEDIA=true`, present a dual-invalid sample, and confirm
   the durable marker audit precedes deletion. Replace it at the same path and
   confirm the new generation is processed normally.

Do not corrupt or delete real library media to test this release.

## Compatibility

HTTP routes, required response fields, queue identity, subtitle naming,
language/task behavior, completion webhooks, directory `.subgen_skip`, and the
schema-v1 exact-generation marker contract remain compatible. Uploaded `/asr`
and OpenAI-compatible byte-buffer requests are unchanged and never enter local-
file segmentation, even when segmentation is enabled.

The repository/image/project version is `0.5.0`. The overlaid upstream runtime
status deliberately remains `2026.07.1`; `/status` reports that stable runtime
version rather than the release tag. This is not an incomplete upgrade.

There is no Sonarr/Radarr API integration in v0.5.0. A deleted disposable or
operator-approved invalid file is replaced only through the operator's existing
library automation. Subgen also performs no Ollama lifecycle coordination.

## Deletion safety

`AUTO_DELETE_INVALID_MEDIA=true` is the canonical opt-in. The deprecated
`AUTO_DELETE_FAILED_FILES=true` alias is accepted through 0.5.x but is narrowed
to the same invalid-media-only path and warns once. Either true value can enable
that path during migration, so set both false to disable it.

Silent/no-audio media, disagreement between validators, timeouts, permission or
I/O failures, generic/inference/resource errors, OOM, pressure yield, SIGSEGV,
log-regex matches, legacy/untyped intents, and stale replacements are retained.
Only a typed dual-invalid result for the unchanged current generation can be
marked and then deleted by the monitor. Repair never deletes media or legacy
empty subtitle markers, even when `SUBGEN_REPAIR_ACTION=delete` is requested.

## Rollback

For a public installation, stop v0.5.0, set both deletion booleans false and
repair to `report`, restore the backed-up v0.4.1 Compose/config/scripts and
immutable image digest, and recreate that container. Recheck `/status`, scan
progress, marker readability, and OOM/restart state. Preserve the schema-v1
marker registry and v0.5 evidence; do not delete media as part of rollback.

The Frigate operational rollback is different: it restores the preserved
v0.3.0 Compose/config, model cache, OCI identity, generation registry, and
captured unit states with deletion disabled first. It does not restore public
v0.4.1 and never recreates the retired Plex-hosted instance.

## Known boundaries

- Segmentation cannot reduce the resident model weights or guarantee that the
  backend reaches a progress callback before its first large allocation.
- Generic CPU/GPU tiers are safety-oriented fallback hypotheses. Only repeated
  exact-runtime evidence can promote beyond them.
- Shared-CUDA telemetry loss or reduced higher-priority headroom can leave
  Subgen waiting indefinitely; that is an intentional fail-closed outcome.
- ModelEnvelope files are distribution inputs, not secrets or self-generated
  trust. Operators must preserve their ownership, mode, identity, and complete
  provenance chain. The exact-evidence overlay refuses to create missing host
  paths.
- Marker identity still depends on host/container metadata matching across the
  media bind. Prove this against disposable media before enabling deletion.

See the [configuration guide](./CONFIGURATION.md), [migration guide](./MIGRATION.md),
and [full changelog](../CHANGELOG.md).
