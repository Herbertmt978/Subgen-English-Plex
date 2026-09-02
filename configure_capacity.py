"""Generate Subgen's finite Docker memory limit from verified engine capacity."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from subgen_core.resource_management import (
    GIB,
    MIB,
    automatic_host_reserve_bytes,
    automatic_subgen_memory_limit_bytes,
)

MINIMUM_SUPPORTED_ENGINE_BYTES = 4 * GIB
MAX_DOCKER_INFO_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DockerEngineCapacity:
    """Capacity facts reported by the selected Linux Docker engine."""

    total_bytes: int
    rootless: bool
    cgroup_driver: str
    cgroup_version: str


class CapacityConfigurationError(RuntimeError):
    """A fail-closed capacity configuration error."""


def inspect_docker_engine_capacity(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DockerEngineCapacity:
    """Return validated capacity facts for the selected Linux Docker engine."""

    try:
        result = runner(
            ["docker", "info", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapacityConfigurationError(
            "Docker capacity could not be inspected"
        ) from exc
    if result.returncode != 0:
        raise CapacityConfigurationError("Docker capacity could not be inspected")
    raw = result.stdout
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_DOCKER_INFO_BYTES:
        raise CapacityConfigurationError("Docker returned invalid capacity metadata")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CapacityConfigurationError(
            "Docker returned invalid capacity metadata"
        ) from exc
    if not isinstance(payload, dict):
        raise CapacityConfigurationError("Docker returned invalid capacity metadata")
    if payload.get("OSType") != "linux":
        raise CapacityConfigurationError("Subgen requires a Linux Docker engine")
    if payload.get("MemoryLimit") is not True:
        raise CapacityConfigurationError("Docker memory limits are unavailable")
    if payload.get("SwapLimit") is not True:
        raise CapacityConfigurationError("Docker no-extra-swap limits are unavailable")
    total = payload.get("MemTotal")
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        raise CapacityConfigurationError("Docker returned invalid total memory")
    if total < MINIMUM_SUPPORTED_ENGINE_BYTES:
        raise CapacityConfigurationError(
            "Subgen requires at least 4 GiB of verified Docker-engine memory"
        )
    options = payload.get("SecurityOptions", [])
    if options is None:
        options = []
    if not isinstance(options, list) or any(
        not isinstance(option, str) for option in options
    ):
        raise CapacityConfigurationError("Docker returned invalid security metadata")
    rootless = any("rootless" in option.casefold() for option in options)
    cgroup_driver = payload.get("CgroupDriver")
    cgroup_version = payload.get("CgroupVersion")
    if not isinstance(cgroup_driver, str) or not cgroup_driver.strip():
        raise CapacityConfigurationError("Docker returned invalid cgroup metadata")
    if isinstance(cgroup_version, int) and not isinstance(cgroup_version, bool):
        cgroup_version = str(cgroup_version)
    if not isinstance(cgroup_version, str) or not cgroup_version.strip():
        raise CapacityConfigurationError("Docker returned invalid cgroup metadata")
    cgroup_driver = cgroup_driver.strip().casefold()
    cgroup_version = cgroup_version.strip()
    if cgroup_driver == "none":
        raise CapacityConfigurationError(
            "Docker cgroup resource limits are unavailable"
        )
    if rootless and (cgroup_driver != "systemd" or cgroup_version != "2"):
        raise CapacityConfigurationError(
            "Rootless Docker resource limits require cgroup v2 with systemd"
        )
    return DockerEngineCapacity(
        total_bytes=total,
        rootless=rootless,
        cgroup_driver=cgroup_driver,
        cgroup_version=cgroup_version,
    )


def inspect_docker_engine_memory(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Compatibility wrapper returning the selected engine's reported memory."""

    return inspect_docker_engine_capacity(runner).total_bytes


def parse_guaranteed_memory_bytes(raw: str | None) -> int | None:
    """Parse an optional stable VM/engine floor without using binary floats."""

    if raw is None:
        return None
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError) as exc:
        raise CapacityConfigurationError(
            "Guaranteed memory must be a positive GiB value"
        ) from exc
    if not value.is_finite() or value <= 0:
        raise CapacityConfigurationError(
            "Guaranteed memory must be a positive GiB value"
        )
    capacity = int((value * GIB).to_integral_value(rounding=ROUND_FLOOR))
    if capacity < MINIMUM_SUPPORTED_ENGINE_BYTES:
        raise CapacityConfigurationError("Guaranteed memory must be at least 4 GiB")
    return capacity


def resolve_stable_capacity_bytes(
    engine_total_bytes: int,
    guaranteed_memory_bytes: int | None,
) -> int:
    """Bind policy to the engine total or a lower guaranteed VM floor."""

    if (
        isinstance(engine_total_bytes, bool)
        or not isinstance(engine_total_bytes, int)
        or engine_total_bytes < MINIMUM_SUPPORTED_ENGINE_BYTES
    ):
        raise CapacityConfigurationError("Docker engine memory is invalid")
    if guaranteed_memory_bytes is None:
        return engine_total_bytes
    if (
        isinstance(guaranteed_memory_bytes, bool)
        or not isinstance(guaranteed_memory_bytes, int)
        or guaranteed_memory_bytes < MINIMUM_SUPPORTED_ENGINE_BYTES
    ):
        raise CapacityConfigurationError("Guaranteed memory is invalid")
    if guaranteed_memory_bytes > engine_total_bytes:
        raise CapacityConfigurationError(
            "Guaranteed memory cannot exceed Docker-engine memory"
        )
    return guaranteed_memory_bytes


def render_memory_limit(stable_capacity_bytes: int) -> str:
    """Return Compose's generated limit as an integer MiB value."""

    limit = automatic_subgen_memory_limit_bytes(stable_capacity_bytes)
    if limit % MIB:
        raise CapacityConfigurationError("Generated memory limit is not whole MiB")
    return f"{limit // MIB}m"


def _atomic_write(path: Path, rendered: str, *, mode: int) -> None:
    """Atomically replace a generated UTF-8 file with a fixed POSIX mode."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CapacityConfigurationError(
            "Generated file directory is unavailable"
        ) from exc
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(rendered)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise CapacityConfigurationError(
            "Generated capacity file could not be updated"
        ) from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def render_capacity_compose(stable_capacity_bytes: int) -> str:
    """Render the generated service fragment used by every Compose profile."""

    limit = render_memory_limit(stable_capacity_bytes)
    return (
        "# Generated by configure_capacity.py; rerun it when hardware changes.\n"
        "services:\n"
        "  subgen-capacity:\n"
        f"    mem_limit: {limit}\n"
        f"    memswap_limit: {limit}\n"
        "    oom_score_adj: 1000\n"
    )


def write_capacity_compose(path: Path, stable_capacity_bytes: int) -> None:
    """Atomically write the literal capacity fragment used through `extends`."""

    _atomic_write(
        Path(path), render_capacity_compose(stable_capacity_bytes), mode=0o644
    )


def environment_permissions_are_private(
    mode: int, platform_name: str = os.name
) -> bool:
    """Return whether a secret file mode is private on the current platform."""

    return platform_name != "posix" or stat.S_IMODE(mode) & 0o077 == 0


def verify_private_environment_file(path: Path) -> None:
    """Require the secret-bearing environment file and private POSIX access."""

    path = Path(path)
    if path.is_symlink():
        raise CapacityConfigurationError("Environment file must not be a symlink")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CapacityConfigurationError(
            "Environment file is unavailable; create it with owner-only access"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CapacityConfigurationError("Environment file must be a regular file")
    if not environment_permissions_are_private(metadata.st_mode):
        raise CapacityConfigurationError(
            "Environment file must be owner-only; run chmod 600 on it"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a finite no-extra-swap Subgen memory limit from the "
            "selected Docker engine."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="secret-bearing environment file to validate (default: .env)",
    )
    parser.add_argument(
        "--capacity-file",
        type=Path,
        default=Path(".subgen-capacity.yml"),
        help="generated Compose capacity fragment (default: .subgen-capacity.yml)",
    )
    parser.add_argument(
        "--guaranteed-memory-gib",
        help=(
            "stable VM balloon, rootless user-slice, or nested-daemon floor in "
            "GiB; must not exceed the verified Docker-engine total"
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        engine = inspect_docker_engine_capacity(runner)
        guaranteed = parse_guaranteed_memory_bytes(args.guaranteed_memory_gib)
        if engine.rootless and guaranteed is None:
            raise CapacityConfigurationError(
                "Rootless Docker requires --guaranteed-memory-gib because an "
                "ancestor user slice can be lower than Docker's reported total"
            )
        stable_capacity = resolve_stable_capacity_bytes(engine.total_bytes, guaranteed)
        rendered_limit = render_memory_limit(stable_capacity)
        verify_private_environment_file(args.env_file)
        write_capacity_compose(args.capacity_file, stable_capacity)
    except CapacityConfigurationError as exc:
        print(f"Capacity configuration failed: {exc}", file=sys.stderr)
        return 2

    reserve = automatic_host_reserve_bytes(stable_capacity)
    print(
        "Configured "
        f"{args.capacity_file} with a {rendered_limit} Subgen limit from "
        f"{stable_capacity // MIB} MiB stable capacity; "
        f"host reserve={reserve // MIB} MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
