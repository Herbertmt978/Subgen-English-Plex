# Generation-Bound Failure Markers v0.4.0 — Evidence

Status: `draft`

## Initial Evidence

- Baseline regression before design: `315 passed, 56 skipped` using isolated pytest plugin loading.
- Approved design commits: `9b78174`, `658ebdc`.
- Executable plan commit: `3c98071`.
- TaskStartSnapshot: clean branch, zero behind/three ahead of `origin/main`, no active Git operation, one worktree.
- User constraint added before issue/code publication: no GitHub-hosted runners for tests or builds; use local/simulator execution and conditionally shut down only a simulator woken by this task after a clear activity check.
- No-runner plan amendment: design/plan/work records updated; `git diff --check` and incomplete-token/stale-PR-gate searches passed before commit.
- Public traceability: issue [#6](https://github.com/Herbertmt978/Subgen-English-Plex/issues/6) is open; `gh issue view` confirmed marker-first, replacement, deletion-off, v0.4.0, and local/simulator-only verification scope.

## Shared Marker Contract

- Added the sole schema/reader owner in `subgen_failure_markers.py` plus focused contract tests.
- The contract enforces exact case-preserving `/media` paths, five-field generation identity, deterministic schema version 1, bounded secure reads, exact replacement detection, cached invalidation, and fail-open consumer decisions.
- Added reload-race coverage beyond the planned cases: a registry atomically replaced during a read is reloaded rather than caching old contents under new metadata.
- Focused verification ran five consecutive times: each run reported `10 passed, 1 skipped`; the skip is the platform-gated Windows symlink-capability case. `git diff --check` and Python compilation passed in the same verification command.
- A Windows-only lazy NTFS `ctime` normalization mismatch was reproduced and isolated; Linux retains device/inode/size/mtime/ctime cache identity while Windows uses the stable device/inode/size/mtime/mode/link tuple. Exact media-generation identity remains the existing five-field contract on every platform.

## Monitor Producer

- Commit `186770e` adds public marker-on/threshold-one and delete-off defaults, rejects only the approved unreachable enabled threshold, and leaves `subgen_ops_safety.py` as the sole deletion owner.
- The monitor atomically persists and audits an exact marker before optional processing-error or exact-SIGSEGV deletion; a valid-file registry failure records `marker_blocked` and never calls deletion. Legacy basename-only crash attribution is report-only.
- Focused verification after corrupt-registry and lower-delete-threshold regressions: `32 passed, 19 skipped`.
- Fresh complete local regression after the producer slice: `336 passed, 58 skipped in 1.89s`; skips are platform-gated Linux filesystem/deletion coverage reserved for the simulator.
- `git diff --check`, Python compilation, scoped staging, and commit readback passed. No GitHub workflow was invoked.

## Runtime Enforcement

- Commit `f2585a4` wires one shared reader through the facade and enforces matching-generation skips in `subgen_core.media.gen_subtitles_queue` after the active-queue guard and before `has_audio`.
- Matching generations skip without probing; replacement identities and malformed registries proceed normally with rate-limited structured events. `SKIP_MARKED_FAILED_FILES=false` bypasses registry reads.
- Watchdog and direct-file scanner pre-probes were removed; an AST ownership regression requires media to remain the sole marker enforcement owner and requires its marker check to precede `has_audio`.
- Focused verification: `83 passed, 1 skipped`; complete local verification: `342 passed, 58 skipped in 2.29s`.
- A transient synced-workspace Git index rewrite collision occurred after valid staging. Disk space (449.61 GiB free), ACL, lock state, `git write-tree`, and staged diff were healthy; one unchanged local commit retry succeeded. Nothing was pushed and no workflow ran.

## v0.4.0 Distribution and Documentation

- Dockerfile and source/package Compose profiles carry the shared marker module, identity dependency, common marker settings, and the same read-only state mount. Packaged CPU/GPU defaults derive `v0.4.0` from `VERSION`.
- Public examples retain marker-on/one and deletion-off/three. Upgrade docs require existing delete users to set marker/delete to three/three, and document the explicit Plex one/one Sonarr/Radarr cycle.
- Both GitHub workflows now expose only `workflow_dispatch`; no automatic push, pull-request, or release trigger remains. `CONTRIBUTING.md` records local/idle-simulator verification and conditional wake/shutdown rules.
- Added complete v0.4.0 release notes and accepted ADR 0001. Release metadata tests bind VERSION, changelog link, notes sections, and Aegis index.
- Moved producer feature coverage out of the large legacy monitor test file into the planned dedicated marker test owner; behavior remained green (`85 passed, 19 skipped` for the combined marker/monitor/package/boundary set).
- Fresh Task 5 verification: `52 passed` package/boundary tests; `compileall`; all three `docker compose ... config --quiet` commands; `git diff --check`; and the no-automatic-trigger search all passed locally.

## Full Local and Simulator Verification

- Release packaging commit `dfa2408` was followed by safety fix `c24818a`. The simulator's first complete Linux run exposed that the new marker gate prevented the pre-existing unfingerprinted-generation reset from running: deletion stayed blocked, but the unmatched failure count became `1` instead of remaining `0`.
- The fix extracted the secure generation-capture/reset operation, runs it before marker persistence can gate deletion, reuses it in the deletion owner, and covers processing-error and exact crash paths. A new platform-independent regression test keeps this Linux-only safety failure visible on Windows.
- Fresh Windows verification for the exact fixed tree: `356 passed, 58 skipped in 1.73s`; marker/monitor focus `37 passed, 19 skipped`; Python compilation, `git diff --check`, all three Compose configurations, version `0.4.0`, and the no-automatic-workflow-trigger check passed. The worktree was clean after commit.
- At the user's request, Ubuntu's distro-supported `python3-venv` package was installed and verified on both this workstation and the simulator: package `3.12.3-0ubuntu2.1`, Python `3.12.3`. No GitHub runner or test service was used.
- The simulator was already powered on, with no logged-in user, relevant test/build process, container, image, build cache, or task lock. It had 718.3 GiB free on the fixed `D:` Docker/work drive. This task did not send Wake-on-LAN and therefore did not shut the machine down.
- Exact commit `c24818a` was transferred directly over the LAN as a Git archive; SHA-256 `3189A643D2D9E8A754EEAB5D69B1552EEA4FB3078BBEB06C204C433BDC67FEA0` matched before extraction. GitHub was not used for transfer or verification.
- Fresh Linux verification after the fix: `414 passed, 1 warning in 4.94s`; the warning is the upstream Starlette/httpx deprecation emitted by FastAPI's test client. Python compilation and all three simulator Compose configurations passed.
- The simulator built `subgen-english-plex:v0.4.0-candidate` locally from the pinned upstream digest. Explicit post-build inspection recorded image ID `sha256:5898ac603ba7a7bcee0935d45f67d49b79976e327dd1490e7b16b096fbba331d`, size 12.7 GB, expected labels, and zero containers. A task-local anonymous Docker Hub configuration bypassed an unavailable headless Windows credential helper without reading or modifying saved credentials.
- Packaged-candidate `/status` returned HTTP 200 on `127.0.0.1:19000`; the source-mounted pinned base returned HTTP 200 on `127.0.0.1:19001`. Both named smoke containers were removed.
- Disposable in-container evidence returned `original=matched`, `replacement=stale`, `marker_only_file_retained=true`, `delete_threshold=1`, and `marker_before_delete=true`. The host-only monitor was mounted read-only, while the reader and runtime modules came from the candidate image, matching the production packaging boundary.
- Final simulator checks found zero containers, no logged-in user, and no relevant test/build processes. Task-created old/smoke/config directories were removed, the exact candidate source/venv/image were retained for publication, the task-started Ubuntu WSL instance was terminated, and the already-on simulator was left powered on.

Later sections record concise command outcomes, artifact identifiers, and deployment evidence; raw secrets, private media names, and complete logs are excluded.
