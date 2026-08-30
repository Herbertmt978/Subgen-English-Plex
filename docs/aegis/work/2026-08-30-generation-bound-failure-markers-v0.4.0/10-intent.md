# Generation-Bound Failure Markers v0.4.0 — Task Intent

Status: `active`
Date: `2026-08-30`
Parent plan: `docs/aegis/plans/2026-08-30-generation-bound-failure-markers-v0.4.0.md`

## Requested Outcome

Ship `v0.4.0` with an exact file-generation marker after the first qualifying failure, public deletion disabled, Plex marker/delete thresholds set to one, an immutable verified GHCR artifact, and a rollback-safe Plex rollout.

## Scope

- Shared versioned marker schema and reader.
- Monitor marker-before-delete persistence and audit.
- Canonical pre-probe runtime enforcement.
- Source/image/Compose packaging, docs, version, release, and Plex deployment.
- Local/simulator-only test and image-build execution with automatic GitHub runner triggers disabled.

## Non-goals

- Sonarr/Radarr API search triggers.
- Ollama lifecycle coordination.
- GitHub-hosted runner use for tests or image builds.
- Subtitle naming, model choice, concurrency, or HTTP API changes.
- Path-only or directory-wide replacement of the existing `.subgen_skip` behavior.

## Stop Conditions

- `done`: source, release, artifact, and Plex deployment evidence meet the parent plan.
- `blocked`: required credentials, host reachability, local/simulator verification, or release infrastructure prevents progress.
- `needs-verification`: code or deployment exists but required evidence is incomplete.
- `scope-exceeded`: implementation requires a new owner/API/destructive target beyond the approved plan.

## Risk Hints

- Marker must precede an enabled delete attempt.
- Host and container fingerprints must agree on Plex.
- Registry permissions must remain private while readable by the container UID.
- Public deletion stays off; Plex deletion is explicitly threshold one.
- The live 8 GiB hard/no-swap limit must survive container recreation.
- The simulator must be checked for other workloads before use and before shutdown; only a host woken by this task may be shut down by this task.

## BaselineReadSetHint

- `docs/aegis/specs/2026-08-30-generation-bound-failure-markers-design.md`
- `docs/aegis/baseline/2026-08-30-initial-baseline.md`
- `docs/aegis/BASELINE-GOVERNANCE.md`
- `CONTRIBUTING.md`
- Existing tests and packaging/release workflows.

## BaselineUsageDraft

- Required baseline refs: all BaselineReadSetHint entries.
- Delivered context refs: live Plex measurements and deployment findings captured in the parent task.
- Acknowledged before plan refs: all required refs.
- Cited in plan refs: all required refs.
- Missing refs: none.
- Decision: `continue`.

## ImpactStatementDraft

The change affects monitor state, runtime queue policy, scanner ownership, three Compose profiles, image contents, workflow triggers, simulator lifecycle, manual GHCR publication, public defaults, documentation, release metadata, and the Plex deployment. It preserves HTTP, subtitle, queue, repair, and exact deletion contracts.

## Execution Readiness View

- Intent Lock: first-failure exact-generation skip; replacement allowed; Plex also deletes; release v0.4.0.
- Scope Fence: no Arr API trigger, Ollama coordinator, model/concurrency/API change, or arbitrary mutation endpoint.
- Baseline Lock: approved design, initial baseline, contribution rules, tests.
- Owner / Contract Constraints: shared contract; monitor producer; media enqueue consumer; ops safety deletion.
- Compatibility Boundary: fail-open transcription, fail-closed deletion, stable routes/outputs/directory markers.
- Retirement Boundary: remove scanner pre-probes; retain existing state, repair, and `.subgen_skip`.
- Task Batches: issue, contract, monitor, runtime, package/docs, local/simulator verification, no-runner release, Plex rollout.
- Test Obligations: parent plan Task 2–8 verification.
- Review Gates: focused checks and commit per slice; complete local/simulator pre-push suite; no-runner proof; live release/artifact/deployment checks.
- Drift / Rewind Rules: owner/schema drift returns to design; failed evidence returns to owning slice; rollout failure uses backup.
- Evidence Required Before Completion: local/simulator tests, diffs, commits, issue/release, no-runner proof, digest, image smoke, backup, live health/limits/OOM/pressure.
- Advisory Boundary: method-pack execution guidance only; not completion authority.

## TaskStartSnapshot

- Root: `C:/Users/Ashby/Dropbox/PC (3)/Documents/Code/Subgen-English-Plex`
- HEAD: `3c980719c8849491be4a425a087642ce13e90f0b`
- Branch: `Herb/generation-bound-failure-markers`
- Divergence from `origin/main`: zero behind, three ahead.
- Staged/unstaged/untracked: none.
- Active Git operations: none.
- Worktrees: one, at the repository root above.
