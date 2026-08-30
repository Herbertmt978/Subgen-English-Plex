# Generation-Bound Failure Markers v0.4.0 Implementation Plan

Status: `approved for execution`
Date: `2026-08-30`

## Goal

Release Subgen English for Plex `v0.4.0` with an exact file-generation marker that is persisted after the first qualifying failure, checked before media probing, and automatically ignored when Sonarr/Radarr replaces the file at the same path. Keep public deletion disabled, configure the Plex deployment to mark and delete after one failure, and publish and deploy a verified immutable GHCR image with a tested rollback.

## Architecture

A new root module, `subgen_failure_markers.py`, owns the versioned JSON contract, strict container-path and five-field identity validation, bounded secure reads, cache invalidation, and exact match decisions. `monitor_subgen_failures.py` remains the sole producer and audit owner. `subgen_core.media.gen_subtitles_queue` remains the canonical enforcement point and checks the shared reader before `has_audio`; scanner-side pre-probes are removed so no file-based path opens marked media first. `subgen_ops_safety.py` remains the only deletion owner.

## Tech Stack

- Python 3.10+ standard library and pytest
- FastAPI Subgen compatibility facade plus `subgen_core`
- Docker Compose and GHCR
- systemd host monitor on Linux
- Git, GitHub CLI, GitHub Actions

## Baseline / Authority Refs

- `docs/aegis/specs/2026-08-30-generation-bound-failure-markers-design.md`
- `docs/aegis/baseline/2026-08-30-initial-baseline.md`
- `docs/aegis/BASELINE-GOVERNANCE.md`
- `CONTRIBUTING.md`
- Existing executable contracts under `tests/`
- User approval on 2026-08-30: public marker/skip after the first qualifying failure; Plex marker and deletion after the first qualifying failure; package as a new release

## Compatibility Boundary

- Preserve every existing HTTP route and response shape.
- Preserve subtitle naming, translation behavior, queue identity, concurrency, and existing directory `.subgen_skip` behavior.
- Preserve `subgen_ops_safety.py` as the sole exact-file deletion implementation.
- Preserve public `AUTO_DELETE_FAILED_FILES=false` and `AUTO_DELETE_MIN_FAILURES=3` defaults.
- Default `AUTO_MARK_FAILED_FILES=true`, `AUTO_MARK_MIN_FAILURES=1`, and `SKIP_MARKED_FAILED_FILES=true`.
- If automatic deletion is enabled, reject `AUTO_DELETE_MIN_FAILURES > AUTO_MARK_MIN_FAILURES` because active markers make that delete count unreachable. Operators retaining three-failure deletion set both thresholds to `3`; Plex sets both to `1`.
- An absent, unreadable, malformed, oversized, unsafe, or unsupported registry never authorizes deletion and fails open for transcription.
- A prior runtime ignores the new registry during rollback; the registry is retained as evidence.

## TDD Route

- Mode: `off`
- Decision: `skipped`
- Strict authority: `not applicable`
- Test posture: `post-change regression`
- Reason: neither the user nor repository requested strict RED/GREEN TDD; the change will use focused post-change contract tests followed by the full suite and deployment smoke tests.
- Verification: focused marker, monitor, scanner, packaging, full pytest, compile, Compose, image boot, isolated marker/delete, and production health checks.

## Aegis Visibility

Planning is useful because this change creates a durable cross-process schema, touches destructive-operation sequencing, changes a deployment default, adds a distribution surface, and culminates in a production rollout.

## Plan Basis

The existing monitor already owns exact paths, generation fingerprints, atomic state, audit output, and thresholded deletion. The runtime already funnels file work through `subgen_core.media.gen_subtitles_queue`, although Watchdog and direct-file scanner branches currently perform redundant `has_audio` probes first. The package has three Compose profiles, a GHCR publication workflow, and a versioned release convention.

## BaselineUsageDraft

- Required baseline refs: approved design spec, initial product/runtime baseline, repository contribution rules, current tests.
- Delivered context refs: live Plex resource measurements and existing monitor/deployment behavior from this task.
- Acknowledged before plan refs: all required refs above.
- Cited in plan refs: all required refs above.
- Missing refs: none.
- Decision: `continue`.

## Requirement Ready Check

- Requirement source refs: approved design spec and the user's first-failure correction.
- Goals and scope refs: design Intent, Approved Behaviour, Release and Rollout.
- User / scenario refs: a corrupt media generation repeatedly crashes Subgen; a replacement at the same path must process normally.
- Requirement item refs: marker-first sequencing, first-failure public skip, public deletion off, Plex first-failure deletion, v0.4.0 release.
- Acceptance / verification criteria refs: design Verification and Release acceptance lists.
- Open blocker questions: none.
- Decision: `ready`.

## Ripple Signal Triage

The cross-process contract affects the monitor producer, runtime consumer, source and packaged images, all Compose profiles, state-directory ownership guidance, packaging tests, version docs, GHCR workflow, and Plex deployment. HTTP APIs, subtitle outputs, queue keys, repair behavior, and Ollama lifecycle are outside the change.

## Change Necessity

- User-visible need: stop a known-bad file generation from reopening and retriggering an entire folder scan while still allowing a replacement to process.
- No-change / non-code option: directory `.subgen_skip` blocks healthy siblings and replacements; deletion alone leaves churn when deletion is blocked; a path-only list blocks replacements.
- Why code change is necessary: only a persisted generation fingerprint checked before media probing can distinguish the bad object from its replacement.
- Minimum change boundary: one shared contract module, monitor producer wiring, canonical enqueue enforcement, scanner pre-probe removal, package/config/docs/version surfaces.
- Decision: `code-change`.

## Existence Check

- Proposed new surface: `subgen_failure_markers.py` and `subgen_failure_markers.json`.
- Existing owner / reuse candidate: private monitor recovery state, `.subgen_skip`, and `subgen_ops_safety.py`.
- Why existing surface is insufficient: private state includes host-only details; directory markers are too broad; deletion safety should not become a runtime skip-policy owner.
- Creation proof: producer and consumer need one narrow, versioned, non-destructive schema and identical identity/path rules.
- Entropy / retirement impact: one module and one bounded registry; no parallel deletion path; old runtimes safely ignore the file; registry retirement requires first disabling consumers and preserving audit evidence.
- Decision: `add-with-proof`.

## Architecture Integrity Lens

- Invariant: only exact container path plus exact device/inode/size/mtime/ctime may skip a file generation.
- Canonical owner / contract: shared module owns serialization and match decisions; monitor owns writes; media enqueue owns enforcement; ops safety owns deletion.
- Responsibility overlap: remove scanner `has_audio` calls that bypass the enqueue boundary; do not duplicate matching in webhooks or scanners.
- Higher-level simplification: every scanner branch delegates probing to `gen_subtitles_queue`.
- Retirement / falsifier: if the shared module is not the only schema parser or any enqueue route probes first, return to design before release.
- Verdict: `aligned`.

## Complexity Budget

- Artifact class: source, tests, decision/release artifacts.
- Target files / artifacts: `monitor_subgen_failures.py` (1,489 lines), `subgen_override.py` (1,302), `subgen_core/media.py` (585), `subgen_core/scanner.py` (126), `tests/test_monitor_subgen_failures.py` (724), new focused files.
- Current pressure: monitor and facade exceed the 1,200-line strong signal; monitor tests are near the 800-line soft signal.
- Projected post-change pressure: at risk if schema logic or most tests are added to existing large files.
- Budget result: `at-risk`.
- Planned governance: put all schema/reader logic and most new tests in new cohesive files; restrict monitor/facade edits to configuration and orchestration wiring; do not perform unrelated refactors.

## Plan-Time Complexity Check

- Target files: monitor, facade, media, scanner, focused tests, packaging/docs.
- Existing size / shape signals: monitor and facade are oversized mixed orchestration files.
- Owner fit: monitor is correct producer/audit owner but not schema owner; facade is correct composition root but not match owner.
- Add-in-place risk: encoding, parsing, caching, and validation in either oversized file would create duplicate or mixed responsibility.
- Better file boundary: new `subgen_failure_markers.py`; new `tests/test_failure_markers.py`.
- Recommendation: `add owner file` with wiring-only edits in oversized owners.

## Files

### Create

- `subgen_failure_markers.py` — versioned marker schema, validation, bounded reader/cache, exact match results.
- `tests/test_failure_markers.py` — contract, producer sequencing, consumer enforcement, replacement, safety, and scanner-boundary regressions.
- `docs/RELEASE_NOTES_0.4.0.md` — operator-facing release notes and upgrade sequence.
- `docs/aegis/adr/0001-generation-bound-failure-marker-registry.md` — accepted durable contract decision.

### Modify

- `monitor_subgen_failures.py` — marker configuration, persistence, audit/summary, and marker-before-delete orchestration.
- `subgen_override.py` — reader construction and runtime settings only.
- `subgen_core/media.py` — canonical pre-probe marker decision and structured logs.
- `subgen_core/scanner.py` — remove redundant scanner-side media probes.
- `subgen_ops_safety.py` — no algorithm change; only expose/reuse identity normalization if the shared module requires it without duplication.
- `tests/test_monitor_subgen_failures.py` — extend the common argument helper without growing this file with feature coverage.
- `tests/test_packaging.py` — package, workflow, Compose mount/default, compile command, and version contracts.
- `tests/test_module_boundaries.py` — shared-contract dependency and single-enforcement ownership checks if AST coverage is required.
- `Dockerfile` — copy the shared module and its non-destructive identity dependency.
- `docker-compose.yml`, `docker-compose.ghcr.yml`, `docker-compose.gpu.yml` — read-only registry mount and runtime settings; release image update.
- `.github/workflows/publish-ghcr.yml` — rebuild when shared runtime modules change.
- `.env.example`, `monitor.env.example` — public state path, marker defaults, and threshold explanation.
- `README.md`, `docs/CONFIGURATION.md`, `docs/INSTALL.md`, `docs/MIGRATION.md`, `CONTRIBUTING.md` — behavior, ownership, upgrade, packaging, and verification guidance.
- `CHANGELOG.md`, `VERSION` — v0.4.0 release metadata.
- `docs/aegis/INDEX.md` and approved design/plan status — durable decision and completion links.

## Marker Contract to Implement

The shared module exposes these stable names:

```text
MARKER_SCHEMA_VERSION: integer constant equal to 1
DEFAULT_MARKER_REGISTRY_PATH: /opt/subgen/monitor/subgen_failure_markers.json
MAX_MARKER_REGISTRY_BYTES: integer constant equal to 8 * 1024 * 1024
MAX_MARKER_ENTRIES: integer constant equal to 10,000
MarkerRegistryError: ValueError subclass
MarkerCheck: immutable tuple with status, detail, and report fields
canonical_container_path(value: str) -> str
normalize_file_identity(value: mapping or five-integer sequence) -> FileIdentity
build_marker_entry(container_path, identity, failure_kind, failure_count,
                   timestamp, created_utc=None) -> dict
load_marker_document(path, max_bytes=MAX_MARKER_REGISTRY_BYTES) -> dict
encode_marker_document(entries, updated_utc) -> str
FailureMarkerReader(registry_path=DEFAULT_MARKER_REGISTRY_PATH,
                    media_root="/media",
                    max_bytes=MAX_MARKER_REGISTRY_BYTES)
FailureMarkerReader.check(file_path) -> MarkerCheck
```

The on-disk JSON is exactly one schema wrapper plus a sorted entry list:

```json
{
  "schema_version": 1,
  "updated_utc": "2026-08-30T12:00:00Z",
  "markers": [
    {
      "container_path": "/media/TV/Show/episode.mkv",
      "file_identity": [2049, 123456, 987654321, 1788091200000000000, 1788091200000000000],
      "failure_kind": "processing_error",
      "failure_count": 1,
      "created_utc": "2026-08-30T12:00:00Z",
      "updated_utc": "2026-08-30T12:00:00Z"
    }
  ]
}
```

`canonical_container_path` accepts only absolute, case-preserving paths strictly beneath `/media`, rejects NUL and `..`, and never uses a basename as identity. The loader accepts only schema `1`, a regular non-symlink single-link registry no larger than 8 MiB, at most 10,000 entries, unique exact paths, five non-negative integer identity fields, positive failure counts, supported failure kinds, and timestamp strings. It never returns partially valid data.

`FailureMarkerReader` maps a candidate beneath its configured media root to the corresponding case-preserving `/media` path, captures its regular non-symlink identity, and returns `matched` only for exact path and identity equality. A missing registry is `unmarked`; invalid registry state is `unavailable`; path match plus identity mismatch is `stale`. It caches by registry device/inode/size/mtime/ctime and sets `report=True` only once per unchanged registry/outcome/path/identity tuple to bound logs.

## Tasks

### Task 1: Open the required public issue before production code

**Files:** none.

**Why:** `CONTRIBUTING.md` requires an issue before deletion behavior or deployment defaults change.

**Change Necessity:** external traceability is required; no source edit occurs in this task.

**Impact / Compatibility:** issue text must describe marker-first sequencing, first-failure public skip, deletion-off public default, Plex threshold-one override, and replacement behavior without private host paths or media names.

**Steps:**

1. Confirm no existing issue covers the feature:

   ```powershell
   gh issue list --repo Herbertmt978/Subgen-English-Plex --state all --search 'generation marker OR failed media skip'
   ```

   Expected: no matching open issue.

2. Create the issue:

   ```powershell
   $issueBody = @'
   ## Problem
   One bad media-file generation can fail Subgen and be rediscovered by the next full-folder scan, causing repeated churn or crashes.

   ## Proposed behavior
   - Persist a versioned exact-path plus five-field file-generation marker after the first qualifying failure.
   - Check the marker before media probing or queue insertion.
   - Skip only an exact identity match; process a replacement at the same path normally.
   - Write and audit the marker before optional exact-file deletion.
   - Keep public deletion disabled; allow explicit threshold-one marker/delete operation for Plex.

   ## Acceptance
   - Marker/skip defaults on after one qualifying failure.
   - Public deletion remains off.
   - Replacement identity self-unblocks.
   - Malformed or unavailable marker state fails open for transcription and never authorizes deletion.
   - v0.4.0 tests, package, GHCR image, release notes, rollback, and isolated deployment smoke all pass.
   '@
   $issueUrl = gh issue create --repo Herbertmt978/Subgen-English-Plex --title "Skip exact failed media generations before rescanning" --body $issueBody
   $issueNumber = [int]($issueUrl -replace '.*/', '')
   $issueUrl
   ```

   Expected: one `https://github.com/Herbertmt978/Subgen-English-Plex/issues/` URL.

3. Record the issue URL in the later pull-request body without editing source solely for the issue number.

**Verification:** `gh issue view --repo Herbertmt978/Subgen-English-Plex $issueNumber --json title,state,url,body` shows the exact scope and no private data.

### Task 2: Add the shared marker contract and bounded reader

**Files:** create `subgen_failure_markers.py`; create `tests/test_failure_markers.py`; optionally modify `subgen_ops_safety.py` only to expose a non-duplicated identity normalizer.

**Why:** producer and consumer need one exact schema and match implementation.

**Change Necessity:** a directory marker, private monitor state, or path-only list cannot distinguish a replacement; the minimum new owner is the shared module defined above.

**Impact / Compatibility:** no API or deletion change yet; malformed/absent registries remain non-blocking.

**Steps:**

1. Implement the constants, `MarkerRegistryError`, `MarkerCheck`, canonical path validation, identity normalization, entry builder, bounded document loader/encoder, and `FailureMarkerReader` exactly as specified under “Marker Contract to Implement.” Use `os.open` with `O_NOFOLLOW` where available, compare `lstat`/`fstat`, require regular single-link files, read at most `max_bytes + 1`, and reject registry changes during a read.

2. Keep the document deterministic: sort entries by exact `container_path`, serialize with `indent=2`, append one newline, and reject duplicates rather than choosing one.

3. Add focused tests named:

   - `test_marker_document_round_trips_exact_case_sensitive_paths`
   - `test_marker_document_rejects_duplicate_paths_and_invalid_identity`
   - `test_reader_matches_exact_generation`
   - `test_reader_marks_replacement_generation_stale`
   - `test_reader_keeps_duplicate_basenames_independent`
   - `test_reader_fails_open_for_missing_malformed_and_oversized_registry`
   - `test_reader_refuses_symlinked_registry_and_media_leaf`
   - `test_reader_reloads_only_after_registry_metadata_changes`
   - `test_reader_rate_limits_unchanged_failure_and_stale_reports`
   - `test_reader_rejects_candidates_outside_media_root`

4. Run the focused file:

   ```powershell
   $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
   $env:PYTEST_PLUGINS='requests_mock.contrib._pytest_plugin'
   python -m pytest -q tests/test_failure_markers.py
   ```

   Expected: all marker-contract tests pass with no skips except platform-specific symlink capability.

5. Inspect and commit only this coherent contract slice:

   ```powershell
   git diff --check
   git diff -- subgen_failure_markers.py subgen_ops_safety.py tests/test_failure_markers.py
   git add -- subgen_failure_markers.py subgen_ops_safety.py tests/test_failure_markers.py
   git diff --cached --check
   git commit -m "Add generation-bound failure marker contract"
   ```

**Verification:** focused tests plus a clean staged diff; `git show --stat --oneline HEAD` names only the contract slice.

### Task 3: Persist and audit markers before monitor deletion

**Files:** modify `monitor_subgen_failures.py`, `tests/test_monitor_subgen_failures.py`, and `tests/test_failure_markers.py`.

**Why:** a failed or blocked deletion must still leave enough durable evidence for Subgen to avoid reopening the same generation.

**Change Necessity:** the monitor is the only process with exact failure attribution and threshold state, so producer orchestration must be wired there.

**Impact / Compatibility:** deletion remains owned by `try_delete_path` and `subgen_ops_safety`; public deletion remains off. Marker write failure blocks an enabled deletion attempt for that event and emits an audit error, because deleting without the required preceding marker would violate the approved sequence.

**Steps:**

1. Add monitor arguments and environment defaults:

   ```text
   --auto-mark-failed-files / AUTO_MARK_FAILED_FILES=true
   --auto-mark-min-failures / AUTO_MARK_MIN_FAILURES=1
   ```

   Store `self.auto_mark`, `self.auto_mark_min_failures`, and `self.marker_registry_path = self.state_dir / "subgen_failure_markers.json"`.

2. During initialization, raise a clear `ValueError` only when marker creation and deletion are both enabled and `AUTO_DELETE_MIN_FAILURES > AUTO_MARK_MIN_FAILURES`. This prevents a marker from making the configured delete threshold unreachable. Marker-disabled configurations retain existing deletion semantics.

3. Add a single monitor producer method that:

   - returns success without writing when marking is disabled or the count is below threshold;
   - requires an exact path strictly beneath `/media` plus a complete `failure_identity`;
   - safely loads the existing registry without treating invalid content as empty;
   - preserves the first creation timestamp on refresh;
   - refuses to exceed 10,000 entries or 8 MiB;
   - atomically replaces and directory-syncs the registry using existing private-state primitives;
   - records `FAILURE_MARKER_CREATED`, `FAILURE_MARKER_REFRESHED`, or `FAILURE_MARKER_WRITE_FAILED` without serializing registry content into logs;
   - updates marker status fields used by the human-readable summary.

4. In both `record_processing_error` and exact-path `record_crash_candidate`, call the producer after count/identity update and before `try_delete_path`. When the marker threshold is reached and an enabled marker write fails, do not call deletion for that event. Legacy basename-only crash candidates remain report-only because they cannot form an exact container path.

5. Extend the summary with marker enabled/threshold/path plus per-candidate marker status, timestamp, and generation scope. Do not write tokens, subtitle content, or whole registry entries.

6. Extend `make_args` so existing three-failure deletion tests explicitly use a three-failure marker threshold, while new public-default tests exercise marker threshold one with deletion disabled.

7. Add focused tests named:

   - `test_public_marker_default_is_first_failure_and_delete_remains_off`
   - `test_monitor_writes_first_failure_marker_without_deleting`
   - `test_monitor_persists_marker_before_delete_invocation`
   - `test_monitor_marker_write_failure_blocks_delete_and_is_audited`
   - `test_monitor_refreshes_same_generation_without_duplicate_entry`
   - `test_monitor_replaces_marker_entry_for_new_generation`
   - `test_monitor_marks_exact_sigsegv_candidate_before_delete`
   - `test_monitor_keeps_legacy_basename_only_crash_candidate_report_only`
   - `test_enabled_unreachable_delete_threshold_is_rejected`
   - `test_marker_disabled_preserves_existing_delete_threshold_behavior`

8. Run focused monitor and contract tests:

   ```powershell
   python -m pytest -q tests/test_failure_markers.py tests/test_monitor_subgen_failures.py
   ```

9. Review and commit:

   ```powershell
   git diff --check
   git add -- monitor_subgen_failures.py tests/test_monitor_subgen_failures.py tests/test_failure_markers.py
   git diff --cached --check
   git commit -m "Persist failure markers before deletion"
   ```

**Verification:** tests prove real registry content, marker-before-delete call order, first-failure defaults, replacement refresh, and failure blocking.

**Repair Track:** root cause is retry state living only in the monitor/deletion path; stable repair is a durable exact-generation marker written by the existing failure owner.

**Retirement Track:** do not retire private monitor recovery state or `.subgen_skip`; the new registry is an intentionally narrower consumer contract. Basename-only crash attribution remains non-destructive and cannot create markers.

### Task 4: Enforce marker skips at the canonical pre-probe boundary

**Files:** modify `subgen_override.py`, `subgen_core/media.py`, `subgen_core/scanner.py`, `tests/test_failure_markers.py`, and `tests/test_module_boundaries.py` if needed.

**Why:** the marker prevents churn only if every file path checks it before AV/FFmpeg opens the file.

**Change Necessity:** runtime enforcement is necessary; monitor-only state cannot stop a scanner or webhook from probing the offender.

**Impact / Compatibility:** unmarked and stale paths follow the existing queue logic; queue/API shapes do not change.

**Steps:**

1. In the facade, read:

   ```python
   skip_marked_failed_files = convert_to_bool(os.getenv("SKIP_MARKED_FAILED_FILES", True))
   failure_marker_registry_path = os.getenv(
       "SUBGEN_FAILURE_MARKER_PATH",
       DEFAULT_MARKER_REGISTRY_PATH,
   )
   failure_marker_reader = FailureMarkerReader(failure_marker_registry_path)
   ```

   Import only the shared reader and constants; keep the facade edit wiring-only.

2. In `subgen_core.media.gen_subtitles_queue`, after the cheap active-queue guard and before `runtime.has_audio`, check the reader when skipping is enabled:

   - `matched`: emit one human log plus structured `failure_marker_skip`, then return;
   - `stale`: on `report=True`, emit `failure_marker_stale`, then continue;
   - `unavailable`: on `report=True`, emit `failure_marker_read_failed`, then continue;
   - `unmarked`: continue silently.

   Structured events use the existing `emit_subgen_event` helper and only include event, path, task type, task id, and a bounded error/detail.

3. Remove `runtime.has_audio` from `NewFileHandler.create_subtitle` and the direct-file branch in `transcribe_existing`; both branches pass paths directly to `gen_subtitles_queue`, which retains extension, validity, and audio-stream filtering after the marker decision.

4. Add focused tests named:

   - `test_matching_marker_skips_before_has_audio`
   - `test_replacement_identity_reaches_has_audio_and_queue`
   - `test_skip_marked_failed_files_false_ignores_matching_marker`
   - `test_malformed_registry_reaches_has_audio_with_one_warning`
   - `test_new_file_handler_never_preprobes_before_queue_boundary`
   - `test_direct_file_scan_never_preprobes_before_queue_boundary`
   - `test_directory_subgen_skip_contract_is_unchanged`
   - `test_media_is_the_only_failure_marker_enforcement_owner`

5. Run focused ownership, skip, standalone, and marker tests:

   ```powershell
   python -m pytest -q tests/test_failure_markers.py tests/test_skip_logic.py tests/test_standalone_mode.py tests/test_module_boundaries.py
   ```

6. Review and commit:

   ```powershell
   git diff --check
   git add -- subgen_override.py subgen_core/media.py subgen_core/scanner.py tests/test_failure_markers.py tests/test_module_boundaries.py
   git diff --cached --check
   git commit -m "Skip marked media generations before probing"
   ```

**Verification:** a `has_audio` spy must remain uncalled for a matching marker and must be called for a changed replacement; AST/behavior checks show no scanner-side pre-probe.

**Repair Track:** root cause is the scanner opening media before a durable failure exclusion exists. Canonical repair moves all probing behind the enqueue policy boundary.

**Retirement Track:** scanner pre-probe branches are removed rather than retained as fallbacks; `gen_subtitles_queue` remains the only enforcement owner.

### Task 5: Package, document, and version the feature as v0.4.0

**Files:** modify packaging, Compose, examples, docs, tests, workflow, `VERSION`, `CHANGELOG.md`, and Aegis index; create release notes and ADR.

**Why:** source-only behavior would fail in GHCR and on Plex; the operator-visible default and durable contract require upgrade documentation.

**Change Necessity:** distribution and deployment files must carry the reader module, read-only state mount, and settings.

**Impact / Compatibility:** packaged profiles move from `v0.3.0` to `v0.4.0`; source and image profiles remain equivalent.

**Steps:**

1. Add to all Subgen services:

   ```yaml
   environment:
     - SKIP_MARKED_FAILED_FILES=${SKIP_MARKED_FAILED_FILES:-true}
     - SUBGEN_FAILURE_MARKER_PATH=/opt/subgen/monitor/subgen_failure_markers.json
   volumes:
     - ${SUBGEN_STATE_DIR:-./monitor}:/opt/subgen/monitor:ro
   ```

   Source Compose also bind-mounts `subgen_failure_markers.py` and any required identity helper read-only under `/subgen`.

2. Copy the shared module and its required non-destructive dependency in `Dockerfile`. Add both paths to the GHCR workflow trigger. Update packaging tests to prove image copy, source mounts, all three read-only state mounts, runtime defaults, workflow paths, compile coverage, and version derivation.

3. Add `SUBGEN_STATE_DIR=./monitor` and `SKIP_MARKED_FAILED_FILES=true` to `.env.example`. Add marker enablement and threshold one to `monitor.env.example`; retain deletion false/three. Explain that enabling deletion requires delete threshold no greater than marker threshold.

4. Update README, configuration, install, migration, source map, quick-start directory creation/permissions, and operational safety text. State explicitly:

   - first qualifying failure creates an active exact-generation marker;
   - replacement identity self-unblocks;
   - registry read failure processes normally and never deletes;
   - monitor and container must share the same numeric UID read permission and state directory;
   - public deletion remains off;
   - existing three-failure delete users set both thresholds to three before upgrade;
   - Plex uses threshold one for both and relies on Sonarr/Radarr to replace deletion gaps.

5. Set `VERSION` to `0.4.0`; rename the version assertion accordingly; add dated changelog entries and comparison link; create complete release notes with backup, upgrade, smoke, rollback, and known-boundary sections.

6. Add ADR 0001 recording the shared registry decision, alternatives rejected (directory marker, path-only marker, exposing private monitor state), compatibility, security boundary, and retirement condition. Update `docs/aegis/INDEX.md` with the plan and ADR.

7. Run packaging/docs tests and compilation:

   ```powershell
   python -m pytest -q tests/test_packaging.py tests/test_module_boundaries.py
   python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
   docker compose -f docker-compose.yml config --quiet
   docker compose -f docker-compose.gpu.yml config --quiet
   docker compose -f docker-compose.ghcr.yml config --quiet
   ```

8. Search for stale release strings only where historical notes legitimately retain them:

   ```powershell
   rg -n "v0\.3\.0|0\.3\.0|AUTO_MARK_MIN_FAILURES|SKIP_MARKED_FAILED_FILES|subgen_failure_markers" README.md docs .env.example monitor.env.example Dockerfile docker-compose.yml docker-compose.gpu.yml docker-compose.ghcr.yml .github tests VERSION CHANGELOG.md
   ```

9. Review and commit:

   ```powershell
   git diff --check
   git add -- .github Dockerfile docker-compose.yml docker-compose.gpu.yml docker-compose.ghcr.yml .env.example monitor.env.example README.md docs CONTRIBUTING.md CHANGELOG.md VERSION tests/test_packaging.py tests/test_module_boundaries.py
   git diff --cached --check
   git commit -m "Prepare Subgen English Plex 0.4.0"
   ```

**Verification:** package contracts, compile, all Compose files, release metadata, docs search, and scoped staged diff pass.

### Task 6: Run full local verification and an isolated image smoke

**Files:** no intended source changes; fixes found by verification return to the owning task and receive a focused recheck.

**Why:** the release changes failure handling, packaging, and production behavior.

**Change Necessity:** verification is required; no new code path is endorsed here.

**Impact / Compatibility:** tests must prove stable routes and unrelated runtime behavior remain intact.

**Steps:**

1. Run the full repository-required suite in the isolated plugin environment:

   ```powershell
   $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
   $env:PYTEST_PLUGINS='requests_mock.contrib._pytest_plugin'
   python -m pytest -q
   python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py repair_subgen_failures.py subgen_core
   docker compose -f docker-compose.yml config --quiet
   docker compose -f docker-compose.gpu.yml config --quiet
   docker compose -f docker-compose.ghcr.yml config --quiet
   ```

   Expected: all tests pass, compile exits zero, and all Compose validations are silent/zero.

2. Review branch ownership and complete diff:

   ```powershell
   git status --short --branch
   git diff --check origin/main...HEAD
   git diff --stat origin/main...HEAD
   git diff origin/main...HEAD
   git log --oneline --decorate origin/main..HEAD
   ```

3. Because the local Docker daemon is unavailable, use the Plex Docker host only for an isolated pre-release build and boot; do not alter the live container. Transfer or clone the branch into a temporary directory, build `subgen-english-plex:v0.4.0-candidate`, and start it on `127.0.0.1:19000` with `MONITOR=False`, `SKIP_STARTUP_SCAN=True`, `PROCESS_ADDED_MEDIA=False`, a temporary empty media directory, temporary model/state directories, and no Plex token. Require HTTP 200 from `/status`, then stop/remove the temporary container and retain build logs.

4. In a second temporary directory on Plex, exercise only disposable fake media:

   - marker-only threshold one produces a registry and leaves the file;
   - the candidate image reader reports `matched` for the original identity;
   - replacing the fake file at the same path reports `stale` and allows processing;
   - threshold-one marker plus delete records marker creation before exact-file deletion;
   - cleanup removes only the named temporary directories and candidate container/image after resolved paths are verified beneath the temporary root.

**Verification:** fresh full-suite evidence, clean diff, HTTP 200, and isolated original/replacement/delete evidence. Never use real library files for this smoke.

### Task 7: Publish the reviewed v0.4.0 GitHub release and immutable GHCR artifact

**Files:** GitHub branch, pull request, tag, release, Actions, and GHCR state.

**Why:** the user requested a packaged release, not a private VM-only patch.

**Change Necessity:** external publication is the authorized distribution step.

**Impact / Compatibility:** publish only after all local checks pass; never diagnose known failures with speculative pushes.

**Steps:**

1. Push the verified branch once:

   ```powershell
   git push --set-upstream origin Herb/generation-bound-failure-markers
   ```

2. Create a pull request whose body includes the issue-closing line, behavior/default table, tests, image smoke, deployment plan, and rollback. There is no repository PR template. Use `gh pr create --base main --head Herb/generation-bound-failure-markers` and the verified body.

3. Wait for all PR checks, inspect review threads and mergeability, and fix any actionable finding locally in one cohesive verified follow-up push:

   ```powershell
   gh pr checks --watch
   gh pr view --json state,mergeable,reviewDecision,statusCheckRollup,reviews,comments,url
   ```

   Expected before merge: no failing/pending checks, no unresolved actionable feedback, and `mergeable` reports `MERGEABLE`.

4. Squash-merge only after readiness is proven, fetch main, and verify the merge commit contains `VERSION=0.4.0`:

   ```powershell
   gh pr merge --squash --delete-branch
   git fetch origin main --tags
   git show origin/main:VERSION
   ```

5. Create and push an annotated `v0.4.0` tag at the verified main commit, then publish the GitHub Release from `docs/RELEASE_NOTES_0.4.0.md`:

   ```powershell
   git tag -a v0.4.0 origin/main -m "Subgen English Plex 0.4.0"
   git push origin v0.4.0
   gh release create v0.4.0 --repo Herbertmt978/Subgen-English-Plex --verify-tag --title "Subgen English Plex v0.4.0" --notes-file docs/RELEASE_NOTES_0.4.0.md
   ```

6. Wait for the release-triggered `Publish GHCR Image` workflow, verify success, pull `ghcr.io/herbertmt978/subgen-english-plex:v0.4.0` on Plex, and record the repository digest from `docker image inspect`. Verify the image labels and `/status`; use the digest, not the mutable tag, in production.

**Verification:** live GitHub release exists, tag resolves to reviewed main, Actions are green, GHCR tag pulls, immutable digest is recorded, and candidate image returns HTTP 200.

### Task 8: Back up, deploy, and observe v0.4.0 on Plex

**Files / systems:** Plex VM deployment directory, private backup directory, monitor env/state, systemd unit, Docker Compose project.

**Why:** complete the approved production rollout while preserving recovery and the 8 GiB safety limit.

**Change Necessity:** the feature is only useful to the user after the released artifact and matching monitor are deployed.

**Impact / Compatibility:** this recreates Subgen and updates the host monitor; it must not touch library media except through the already authorized exact first-failure deletion policy after deployment.

**Steps:**

1. Re-inspect the live host, resolved deployment path, Compose files, image/digest, container limits, monitor unit/env/state paths, UID/GID, mounts, VM memory pressure, OOM/restart counters, and production health. Do not print secrets.

2. Stop only `subgen-monitor.service` during file/config replacement. Create a timestamped owner-only backup outside the media tree containing Compose/override/env, monitor script/modules, unit, and marker/private state. Verify backup paths resolve beneath the selected backup root and retain the previous image digest and recreation command.

3. Install the exact `v0.4.0` monitor/shared modules and configure:

   ```dotenv
   AUTO_MARK_FAILED_FILES=true
   AUTO_MARK_MIN_FAILURES=1
   AUTO_DELETE_FAILED_FILES=true
   AUTO_DELETE_MIN_FAILURES=1
   ```

   Configure Compose with `SKIP_MARKED_FAILED_FILES=true`, the same state directory mounted read-only, and `SUBGEN_IMAGE` set to the exact repository digest returned by `docker image inspect ghcr.io/herbertmt978/subgen-english-plex:v0.4.0 --format '{{index .RepoDigests 0}}'`; do not expose credentials in output.

4. Validate effective Compose and systemd configuration before recreation. Preserve the existing hard/no-swap `8g` memory and memory-plus-swap limits, four CPUs, low CPU shares, PID limit, and OOM score adjustment.

5. Recreate Subgen from the immutable digest, require running/healthy status and HTTP 200, then start the monitor and require active/enabled status, heartbeat, marker registry permissions/readability, and no configuration-loop restarts.

6. Trigger only a normal production scan or allow the configured startup scan. Observe logs, container restart/OOM state, memory.current/peak/events, VM available memory and PSI, marker read events, and monitor errors long enough to cover initial discovery. Do not deliberately corrupt or delete real media.

7. Confirm the live configuration shows marker threshold one, deletion threshold one, marker audit enabled, public registry mounted read-only, and the 8 GiB no-swap ceiling. Confirm Ollama remains outside this deployment and no stop/start coordinator was introduced.

8. If health, permissions, marker reads, or pressure checks fail, stop the new monitor, restore the preserved Compose/override/service files and prior image digest, recreate the prior container, restart the prior monitor, and recheck HTTP/OOM/scan health. Retain the v0.4.0 marker registry as evidence; rollback never deletes media.

**Verification:** immutable digest, HTTP 200, monitor active, marker registry readable, one-failure settings effective, 8 GiB/no-swap effective, no OOM/restart regression, production scan progresses, and rollback materials are intact.

## Plan Pressure Test

- Owner / contract / retirement: one schema owner; existing producer, enforcement, and deletion owners retained; scanner pre-probes retired.
- Architecture integrity / higher-level path: canonical enqueue is the only pre-probe gate.
- Verification scope: contract, safety, monitor order, consumer behavior, packaging, full tests, image, GitHub, and live Plex.
- Task executability: each task names exact files, behaviors, commands, evidence, and commit boundary.
- Pressure result: `proceed`.

## Execution Readiness View

- Intent Lock: first qualifying failure creates an exact-generation skip; replacement processes; Plex also deletes immediately; ship v0.4.0.
- Scope Fence: no Sonarr/Radarr API trigger, Ollama coordination, subtitle/model changes, queue concurrency changes, or arbitrary marker mutation API.
- Baseline Lock: approved design, initial baseline, contribution rules, and current tests.
- Approved Behavior: public marker/skip on at one, public delete off; Plex mark/delete at one.
- Owner / Contract Constraints: shared marker module, monitor producer, media enqueue consumer, ops safety deletion.
- Compatibility Boundary: stable APIs/outputs/directory markers; fail-open transcription and fail-closed deletion.
- Retirement Boundary: remove scanner pre-probes; retain private state and old directory markers; prior runtime ignores registry.
- Task Batches: issue/contract; monitor; runtime; packaging/release docs; full verification; GitHub release; Plex rollout.
- Test Obligations: exact identity, replacement, duplicate basename, malformed/oversized/symlink/outside root, order, defaults, ownership, package, full suite, image/live smoke.
- Review Gates: focused tests per task, staged diff per commit, complete pre-push verification, PR CI/review/mergeability, release artifact/digest, production health.
- Drift / Rewind Rules: schema or owner drift returns to design; test failure returns to owning task; deployment failure invokes preserved rollback.
- Evidence Required Before Completion: passing commands, commit/PR/tag/release URLs, green Actions, GHCR digest, HTTP/monitor/limit/OOM/pressure checks, backup/rollback confirmation.
- Advisory Boundary: method-pack execution guidance only; not GateDecision, PolicySnapshot, or completion authority.

## Risks

- Host/container identity mismatch on an unusual filesystem: isolated Plex smoke must prove the five fields agree before production.
- State UID mismatch: shared mount may be unreadable; align numeric service/container UID and verify without broadening permissions.
- Corrupt/oversized registry: consumer processes normally with one bounded warning; producer refuses overwrite and blocks marker-dependent deletion.
- Configuration deadlock: reject enabled deletion threshold above marker threshold.
- Registry growth: hard cap at 10,000 entries/8 MiB; do not silently evict active evidence in v0.4.0.
- Release artifact drift: deploy by verified digest after tag/release workflow success.
- Production memory pressure: retain the measured 8 GiB hard/no-swap cap and verify events/PSI/OOM after recreation.

## Retirement

- Retire scanner-side `has_audio` pre-probes in the same release.
- Do not retire `.subgen_skip`, private monitor recovery state, repair workflow, or exact deletion safety.
- Keep the previous production config/image until the v0.4.0 observation window succeeds.
- The registry can be retired only after marker creation is disabled, all consumers are removed, and audit/rollback retention is explicitly resolved.

## Execution Route

- Decision: `inline`
- Evidence: multi-agent execution is disabled for this workspace; tasks are serially coupled through one schema and release branch.
- Fallback: execute each task in this workspace with scoped commits and checkpointed verification.
- User confirmation required: `no`; the user approved the design, first-failure default, release, and Plex deployment.
