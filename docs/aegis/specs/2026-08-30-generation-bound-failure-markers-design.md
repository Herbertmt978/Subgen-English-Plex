# Generation-Bound Failure Markers

Status: `approved by user`
Date: `2026-08-30`

## Intent

When one media-file generation repeatedly fails or crashes Subgen, persist an exact marker before deletion is attempted. Subgen must skip that marked generation before opening it, while automatically accepting a replacement file at the same pathname.

Success evidence:

- a matching generation is rejected before `has_audio` or media parsing;
- an identity-changing replacement at the same path is queued normally;
- the monitor persists and audits the marker before attempting deletion;
- public marker skipping is enabled after the first qualifying failure;
- the Plex deployment marks and deletes after the first matching failure;
- the existing full test and packaging checks pass;
- GitHub release `v0.4.0` and its GHCR image are published and verified;
- Plex is migrated to the immutable `v0.4.0` image with a tested rollback path.

## Approved Behaviour

### Public repository defaults

- `AUTO_MARK_FAILED_FILES=true`
- `AUTO_MARK_MIN_FAILURES=1`
- `SKIP_MARKED_FAILED_FILES=true`
- `AUTO_DELETE_FAILED_FILES=false`
- `AUTO_DELETE_MIN_FAILURES=3`

Marker creation is non-destructive and enabled by default after the first qualifying failure. This immediately stops a known-bad generation from causing another full-folder scan. Deletion remains an explicit operator opt-in.

When deletion is enabled, `AUTO_DELETE_MIN_FAILURES` must not exceed `AUTO_MARK_MIN_FAILURES`; otherwise the first active marker would prevent the later failure count from ever being reached. The monitor rejects that enabled-but-unreachable configuration with a clear startup error. Existing operators who intentionally retain three-failure deletion must set both thresholds to `3`; the Plex deployment sets both to `1`.

### Plex deployment

- marker creation enabled;
- marker threshold `1`;
- deletion enabled;
- deletion threshold `1`.

The marker is written first. The existing exact-file quarantine/delete path then runs. If deletion is blocked or interrupted, Subgen still avoids reopening that generation. If deletion succeeds and Sonarr/Radarr installs a replacement, the changed identity makes the old marker inapplicable.

## Marker Contract

The monitor writes a dedicated, atomically replaced JSON registry in `SUBGEN_STATE_DIR`. The proposed default filename is `subgen_failure_markers.json`. It is separate from private monitor recovery state so Subgen consumes only a narrow, versioned contract.

Each entry contains:

- schema version;
- exact case-preserving container path beneath `/media`;
- immutable file identity: device, inode, size, modification time, and change time;
- failure kind and matching-failure count;
- creation/update timestamp.

Host paths, tokens, subtitle content, and credentials are not written to this shared registry.

The registry is mounted read-only into the Subgen container. Reads are bounded by a maximum accepted file size, require a regular non-symlink file, and use a cached snapshot invalidated by file metadata changes.

## Processing Flow

1. The monitor attributes a qualifying processing failure or unfinished crash candidate to an exact container path and captures the current file identity.
2. Once `AUTO_MARK_MIN_FAILURES` is reached, it atomically writes or refreshes that generation's marker and appends a structured marker event.
3. The marker write is attempted before deletion. A marker-write failure is audited and does not broaden the deletion target.
4. Every file-based enqueue path checks the registry before `has_audio` or any media parser.
5. Exact path plus exact identity match means skip with a clear log event.
6. Missing path, missing marker, or identity mismatch means continue normally. A replacement therefore self-unblocks.
7. Malformed, oversized, unsafe, or unreadable registry input fails open for transcription with a rate-limited warning; it never authorizes deletion.

## Ownership and Boundaries

- Add a small shared marker-contract module because neither scanner-only logic nor deletion-only safety owns this cross-process serialization contract.
- `monitor_subgen_failures.py` remains the sole marker producer and audit owner.
- The canonical enqueue boundary remains the sole enforcement point, preventing duplicated webhook/scanner checks.
- `subgen_ops_safety.py` remains the sole deletion owner and is not replaced.
- Existing directory-level `.subgen_skip` markers remain unchanged.

## Events and Operator Visibility

Add structured events for marker creation, refresh, skip, stale-generation mismatch, and write/read failure. The human-readable monitor summary shows marker status and identity-bound scope without exposing credentials.

## Compatibility and Deployment

- The status endpoint and all existing webhook/API response shapes remain unchanged in this change.
- Source, CPU GHCR, and GPU Compose profiles mount the monitor state directory read-only and expose the marker settings.
- Packaged images include the shared marker module.
- Operators who do not run the monitor see an absent registry and unchanged processing behaviour.
- Disabling `SKIP_MARKED_FAILED_FILES` ignores markers without deleting them.
- Disabling `AUTO_MARK_FAILED_FILES` stops new marker creation but preserves existing registry evidence.

## Release and Rollout

This feature is released as `0.4.0` because it adds an operator-visible capability without intentionally breaking the existing API or deletion contract.

The implementation pull request includes:

- `VERSION` set to `0.4.0`;
- a `CHANGELOG.md` entry and `docs/RELEASE_NOTES_0.4.0.md`;
- packaged Compose defaults updated from `v0.3.0` to `v0.4.0` through the existing version contract;
- Docker packaging and workflow path coverage for every new runtime module;
- configuration and migration documentation for marker defaults and Plex's threshold-one override.

Release acceptance requires:

1. focused marker tests, the full pytest suite, compile checks, and all three Compose validations pass locally;
2. pull-request CI is green with no unresolved review findings and GitHub reports the branch mergeable;
3. the pull request is merged before tagging, so `v0.4.0` identifies the reviewed main-branch commit;
4. the GitHub Release is published from `docs/RELEASE_NOTES_0.4.0.md`;
5. the GHCR workflow publishes `ghcr.io/herbertmt978/subgen-english-plex:v0.4.0`, and its immutable digest is recorded;
6. the packaged image boots with scanning disabled and returns HTTP 200 from `/status`;
7. an isolated temporary-media smoke proves matching-generation skip, replacement-generation acceptance, and threshold-one marker-before-delete sequencing without touching library media;
8. Plex configuration and monitor state are backed up before migration, and the previous deployment is retained until the new image, monitor, production scan, and failure-marker reads are healthy;
9. Plex runs the immutable release image with the existing 8 GiB hard/no-swap cap, marker threshold `1`, and delete threshold `1`;
10. post-deploy checks cover container limits, HTTP health, monitor activity, marker registry permissions, VM memory pressure, OOM/restart state, and production-scan behaviour.

Rollback restores the preserved Compose/override and monitor service, then recreates the previous container. Marker registry files are retained as evidence but ignored by the previous runtime; rollback never deletes media.

## Verification

Focused regression coverage must prove:

1. public defaults are marker-on after one qualifying failure and deletion-off;
2. no marker is written below threshold;
3. threshold marker persistence happens before delete invocation;
4. a matching generation is skipped before `has_audio`;
5. a replacement at the same path is not skipped;
6. duplicate basenames in different directories remain independent;
7. malformed, oversized, symlinked, and outside-root state fails safely;
8. directory `.subgen_skip` behaviour is unchanged;
9. source and packaged Compose/image profiles include the contract and validate;
10. an enabled deletion threshold greater than the marker threshold is rejected as unreachable;
11. the full repository test, compile, and Compose validation commands pass.

## Non-goals

- Automatically asking Sonarr/Radarr to search.
- Path-only or basename-only blacklists.
- A remote mutation endpoint for markers.
- Ollama lifecycle coordination.
- Changing subtitle naming, model choice, or queue concurrency.

## Design Checks

- Requirement ready: approved marker-plus-deletion behaviour and the public/Plex first-failure marker policy are explicit.
- Existing-surface check: directory markers are too broad and monitor recovery state is private; a small shared marker-contract module is justified.
- Complexity: keep serialization/identity matching isolated; do not add it independently to scanner, facade, and monitor.
- Architecture alignment: aligned with the existing monitor identity owner, canonical enqueue boundary, and exact-file deletion owner.
- ADR signal: the marker registry is a durable cross-process contract; implementation closeout should decide whether an ADR is warranted after the final schema is verified.
