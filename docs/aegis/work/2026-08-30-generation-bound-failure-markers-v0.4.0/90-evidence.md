# Generation-Bound Failure Markers v0.4.0 — Evidence

Status: `draft`

## Initial Evidence

- Baseline regression before design: `315 passed, 56 skipped` using isolated pytest plugin loading.
- Approved design commits: `9b78174`, `658ebdc`.
- Executable plan commit: `3c98071`.
- TaskStartSnapshot: clean branch, zero behind/three ahead of `origin/main`, no active Git operation, one worktree.
- User constraint added before issue/code publication: no GitHub-hosted runners for tests or builds; use local/simulator execution and conditionally shut down only a simulator woken by this task after a clear activity check.
- No-runner plan amendment: design/plan/work records updated; `git diff --check` and incomplete-token/stale-PR-gate searches passed before commit.

Later sections record concise command outcomes, artifact identifiers, and deployment evidence; raw secrets, private media names, and complete logs are excluded.
