# Contributing

Small, focused fixes are welcome. Please open an issue first for changes that alter subtitle naming, deletion behaviour, API compatibility, or deployment defaults.

## Local checks

Use Python 3.10 or newer.

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m compileall -q subgen_override.py language_code.py subgen_ops_safety.py subgen_failure_markers.py monitor_subgen_failures.py monitor_frigate_priority.py repair_subgen_failures.py subgen_core profile_model_envelopes.py apply_stable_ts_fix.py release_tools/check_stable_ts_timing.py
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.gpu.yml config --quiet
docker compose -f docker-compose.ghcr.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.model-envelopes.yml config --quiet
docker compose -f docker-compose.gpu.yml -f docker-compose.model-envelopes.yml config --quiet
docker compose -f docker-compose.ghcr.yml -f docker-compose.model-envelopes.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.priority-pressure.yml config --quiet
docker compose -f docker-compose.gpu.yml -f docker-compose.priority-pressure.yml config --quiet
docker compose -f docker-compose.ghcr.yml -f docker-compose.priority-pressure.yml config --quiet
docker compose -f docker-compose.gpu.yml -f docker-compose.model-envelopes.yml -f docker-compose.priority-pressure.yml config --quiet
```

The Compose checks deliberately fail until `.subgen-capacity.yml` has been
generated from the Docker engine that is running them. Create a private test
`.env`, run `python configure_capacity.py` on that engine, and never commit
either generated file. If the current workstation cannot prove Linux cgroup
memory enforcement, run these checks in the simulator's Linux Docker
environment instead of weakening or fabricating the capacity boundary.

Tests mock the large machine-learning dependencies, so a GPU is not required to run the suite.

Create intended-private state directories in tests with an explicit `0o700`
mode. Do not rely on the shell's umask: Ubuntu may otherwise create a
group-writable directory that the service correctly rejects. Tests of unsafe
permissions must still set those permissions deliberately and verify rejection.

GitHub-hosted runners are disabled for this project. Maintainers run the full
suite and image build locally or on the dedicated simulator PC. Before using
that simulator, confirm no other user, test process, Docker build/container, or
task marker is active. Wake it only when needed, and shut it down afterward
only if your task woke it and a final activity check is clear. The manual
workflow definitions are retained as an emergency fallback, not the normal
test or release path. They are not dispatched for v0.5.0: its full suite,
package build and smoke checks, and image publication must run locally or on
the idle simulator.

## Updating the upstream runtime pin

The image build also applies a small, hash-checked correction to stable-ts
2.19.1: it removes the same wordless segments before, rather than after, the
existing timestamp ordering and validation. Without this ordering, an empty
segment can hide reversed word timestamps inside an otherwise valid segment.
`apply_stable_ts_fix.py` refuses unknown source bytes and is safe to rerun on
the exact corrected file. When changing the runtime pin, review whether the
upstream package still needs this correction; never just accept a new hash.
Subgen's own strict timing validation is not relaxed.

Run `release_tools/check_stable_ts_timing.py` with the actual candidate image's
Python and installed backend, separately from pytest's mocked dependencies.
It uses synthetic timestamps, loads no model, and checks mixed/wordless/empty
results, preserved words, regrouping, and strict rejection. A dependency patch
changes the built image identity, so existing model-envelope and acceptance
evidence cannot be reused as proof for the new image.

The Docker build and source Compose profile deliberately use the same immutable upstream Subgen digest instead of following `latest`. To update it:

1. Pull the candidate tag and inspect its immutable `RepoDigests`: `docker pull mccloud/subgen:<candidate>` followed by `docker image inspect mccloud/subgen:<candidate>`. Before applying this repository's override, record the upstream application version with `docker run --rm --entrypoint grep mccloud/subgen:<candidate> -m1 '^subgen_version' /subgen/subgen.py`.
2. In one branch, set that exact `mccloud/subgen@sha256:...` reference in `Dockerfile`, `docker-compose.yml`, and `UPSTREAM_RUNTIME_IMAGE` in `tests/test_packaging.py`.
3. Treat any runtime-pin change as invalidating prior OCI-identity and
   ModelEnvelope evidence. Recreate and review the exact config-digest plus
   ordered-layer identity and re-profile staged catalogs for the candidate;
   never carry evidence across image identities.
4. Run the full local checks above, build the packaged image, and boot both the packaged image and source profile with scanning and monitoring disabled. Require a successful `GET /status` from each; this reports the stable overlaid runtime version `2026.07.1`, not the untouched upstream base version from step 1 or the project/release version in `VERSION`.
5. Run one controlled short transcription and one non-English-to-English translation smoke with the intended device/compute profile. Commit the three matching pin updates only after both boot and inference smokes pass. Record the upstream base version, stable overlaid runtime version, project/release version, digest change, and regenerated evidence identity in the changelog; never update just one reference or replace the digest with a mutable tag.

## Architecture and tests

`subgen_override.py` is the executable FastAPI composition root and compatibility facade. Canonical queueing, integration, media/scanner, model-runtime, and transcription implementations live under `subgen_core`. Package modules must not import `subgen_override` or `subgen`, because the deployed facade executes as `__main__`.

Patch the canonical `subgen_core` owner when a test exercises an extracted algorithm. Patch the facade when a route, worker dispatch path, or legacy compatibility export is the behavior under test. Changes to a shared contract should cover both its owner and the affected facade or consumer.

## Pull requests

- Keep unrelated formatting and dependency upgrades out of the change.
- Add regression coverage for behavioural fixes.
- Do not commit `.env`, `monitor.env`, tokens, media names, subtitle text, or private paths.
- Update the README or configuration guide when a default or operator-visible behaviour changes.
