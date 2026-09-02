"""Contracts that keep modular code in packaged and source-bind deployments."""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_RUNTIME_IMAGE = (
    "mccloud/subgen@sha256:"
    "128a16bae4f6296fbddd95be3ff47a1c10815fdac6489a66e0b022f2b98c9076"
)
PREVIOUS_RELEASE = "0.4.1"
STABLE_RUNTIME_STATUS_VERSION = "2026.07.1"
RELEASE_H2_HEADINGS = (
    "Highlights",
    "Compared with earlier releases",
    "Public defaults",
    "Operator-specific Frigate deployment",
    "Back up before upgrading",
    "Upgrade",
    "Disposable smoke test",
    "Compatibility",
    "Deletion safety",
    "Rollback",
    "How this release is verified",
    "Known boundaries",
)


def _nested_yaml_block(text, *keys):
    lines = text.splitlines()
    start = 0
    end = len(lines)
    for depth, key in enumerate(keys):
        indent = depth * 2
        marker = f"{' ' * indent}{key}:"
        index = next(i for i in range(start, end) if lines[i] == marker)
        start = index + 1
        end = next(
            (
                i
                for i in range(start, end)
                if lines[i].strip()
                and not lines[i].lstrip().startswith("#")
                and len(lines[i]) - len(lines[i].lstrip()) <= indent
            ),
            end,
        )
    return lines[start:end]


def _markdown_section(text, heading_term):
    lines = text.splitlines()
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")
    for index, line in enumerate(lines):
        match = heading_pattern.match(line)
        if match and heading_term.casefold() in match.group(2).casefold():
            level = len(match.group(1))
            end = next(
                (
                    candidate
                    for candidate in range(index + 1, len(lines))
                    if (next_heading := heading_pattern.match(lines[candidate]))
                    and len(next_heading.group(1)) <= level
                ),
                len(lines),
            )
            return "\n".join(lines[index + 1 : end])
    raise AssertionError(f"No Markdown heading contains {heading_term!r}")


def _markdown_exact_section(text, heading):
    lines = text.splitlines()
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+)$")
    for index, line in enumerate(lines):
        match = heading_pattern.match(line)
        if match and match.group(2).casefold() == heading.casefold():
            level = len(match.group(1))
            end = next(
                (
                    candidate
                    for candidate in range(index + 1, len(lines))
                    if (next_heading := heading_pattern.match(lines[candidate]))
                    and len(next_heading.group(1)) <= level
                ),
                len(lines),
            )
            return "\n".join(lines[index + 1 : end])
    raise AssertionError(f"No Markdown heading equals {heading!r}")


def _markdown_h2_headings(text):
    return tuple(line.removeprefix("## ") for line in text.splitlines() if line.startswith("## "))


def _long_volume_entries(text):
    entries = []
    current = None
    in_bind = False
    for line in _nested_yaml_block(text, "services", "subgen", "volumes"):
        stripped = line.strip()
        if stripped.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            in_bind = False
            key, value = stripped.removeprefix("- ").split(":", 1)
            current[key] = value.strip()
            continue
        if current is None or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        if key == "bind":
            in_bind = True
        elif in_bind and key == "create_host_path":
            current["bind.create_host_path"] = value
        else:
            current[key] = value
    if current is not None:
        entries.append(current)
    return entries


def test_image_copies_subgen_core_package():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = [line.strip() for line in dockerfile.splitlines() if line.strip()]
    assert "COPY subgen_core /subgen/subgen_core" in instructions


def test_image_copies_failure_marker_contract_and_identity_dependency():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = {line.strip() for line in dockerfile.splitlines() if line.strip()}
    assert "COPY subgen_failure_markers.py /subgen/subgen_failure_markers.py" in instructions
    assert "COPY subgen_ops_safety.py /subgen/subgen_ops_safety.py" in instructions


def test_image_copies_owner_operated_profiler_at_exact_runtime_path():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = {line.strip() for line in dockerfile.splitlines() if line.strip()}
    assert (
        "COPY profile_model_envelopes.py /subgen/profile_model_envelopes.py"
        in instructions
    )


def test_build_and_source_compose_share_verified_upstream_runtime():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    build_base = next(
        line.split(maxsplit=1)[1]
        for line in dockerfile.splitlines()
        if line.strip().upper().startswith("FROM ")
    )
    service = _nested_yaml_block(compose, "services", "subgen")
    source_image = next(
        line.strip().split(maxsplit=1)[1]
        for line in service
        if line.strip().startswith("image: ")
    )

    assert build_base == source_image == UPSTREAM_RUNTIME_IMAGE


def test_upstream_runtime_references_are_immutable_digest_pins():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    references = [
        next(
            line.split(maxsplit=1)[1]
            for line in dockerfile.splitlines()
            if line.strip().upper().startswith("FROM ")
        ),
        next(
            line.strip().split(maxsplit=1)[1]
            for line in _nested_yaml_block(compose, "services", "subgen")
            if line.strip().startswith("image: ")
        ),
    ]

    digest_reference = re.compile(r"mccloud/subgen@sha256:[0-9a-f]{64}")
    assert all(digest_reference.fullmatch(reference) for reference in references)
    assert all(not reference.endswith(":latest") for reference in references)


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_packaged_compose_defaults_to_the_versioned_release_image(compose_path):
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    service = _nested_yaml_block(compose, "services", "subgen")
    image = next(
        line.strip().split(maxsplit=1)[1]
        for line in service
        if line.strip().startswith("image: ")
    )

    assert image == (
        "${SUBGEN_IMAGE:-ghcr.io/herbertmt978/"
        f"subgen-english-plex:v{version}}}"
    )


def test_status_route_keeps_stable_overlaid_runtime_version():
    source = (ROOT / "subgen_override.py").read_text(encoding="utf-8")
    version_assignment = re.search(
        r"^subgen_version\s*=\s*['\"](?P<version>[^'\"]+)['\"]",
        source,
        flags=re.MULTILINE,
    )
    status_route = re.search(
        r"@app\.get\(['\"]/status['\"]\)\s*"
        r"def status\(\):(?P<body>.*?)(?=^@app\.)",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert version_assignment is not None
    assert version_assignment.group("version") == STABLE_RUNTIME_STATUS_VERSION
    assert status_route is not None
    assert '"version": f"Subgen {subgen_version},' in status_route.group("body")


def test_source_compose_mounts_subgen_core_read_only():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    assert "      - ./subgen_core:/subgen/subgen_core:ro" in volumes


def test_source_compose_mounts_failure_marker_modules_read_only():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    assert (
        "      - ./subgen_failure_markers.py:/subgen/subgen_failure_markers.py:ro"
        in volumes
    )
    assert "      - ./subgen_ops_safety.py:/subgen/subgen_ops_safety.py:ro" in volumes


def test_source_compose_mounts_profiler_at_packaged_path_read_only():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    assert (
        "      - ./profile_model_envelopes.py:"
        "/subgen/profile_model_envelopes.py:ro" in volumes
    )


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_all_compose_profiles_mount_marker_state_read_only(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    assert (
        "      - ${SUBGEN_STATE_DIR:-./monitor}:/opt/subgen/monitor:ro" in volumes
    )


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_all_compose_profiles_enable_marker_skip_with_shared_path(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    environment = _nested_yaml_block(compose, "services", "subgen", "environment")
    assert "      - SKIP_MARKED_FAILED_FILES=${SKIP_MARKED_FAILED_FILES:-true}" in environment
    assert (
        "      - SUBGEN_FAILURE_MARKER_PATH=/opt/subgen/monitor/"
        "subgen_failure_markers.json" in environment
    )


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_all_compose_profiles_expose_startup_scan_with_catch_up_default(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    environment = _nested_yaml_block(compose, "services", "subgen", "environment")
    assert "      - SKIP_STARTUP_SCAN=${SKIP_STARTUP_SCAN:-False}" in environment


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_all_compose_profiles_expose_v0_5_resource_defaults(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    environment = _nested_yaml_block(compose, "services", "subgen", "environment")
    expected = {
        "      - WHISPER_MODEL=${WHISPER_MODEL:-auto}",
        "      - SEGMENTATION_ENABLED=${SEGMENTATION_ENABLED:-True}",
        "      - SEGMENTATION_CHUNK_MINUTES=${SEGMENTATION_CHUNK_MINUTES:-auto}",
        "      - MEMORY_PRESSURE_YIELD=${MEMORY_PRESSURE_YIELD:-True}",
        "      - MEMORY_PRESSURE_RESERVE_GIB=${MEMORY_PRESSURE_RESERVE_GIB:-auto}",
        "      - PRIORITY_PRESSURE_FILE=${PRIORITY_PRESSURE_FILE:-}",
        "      - GPU_MEMORY_RESERVE_GIB=${GPU_MEMORY_RESERVE_GIB:-auto}",
        "      - CANONICAL_SHARED_CUDA=${CANONICAL_SHARED_CUDA:-False}",
        "      - MODEL_ENVELOPE_CATALOG=${MODEL_ENVELOPE_CATALOG:-/opt/subgen/model-envelopes/catalog.json}",
        "      - MODEL_ENVELOPE_IDENTITY=${MODEL_ENVELOPE_IDENTITY:-/opt/subgen/model-envelopes/image-identity.json}",
    }
    assert expected.issubset(environment)


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_all_compose_profiles_keep_public_resource_boundaries(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    service = _nested_yaml_block(compose, "services", "subgen")
    environment = _nested_yaml_block(compose, "services", "subgen", "environment")

    assert "    extends:" in service
    assert "      file: .subgen-capacity.yml" in service
    assert "      service: subgen-capacity" in service
    assert "    mem_limit:" not in service
    assert "    memswap_limit:" not in service
    assert "    oom_score_adj:" not in service
    assert "    mem_reservation:" not in service
    assert "    oom_kill_disable:" not in service
    assert "      - CONCURRENT_TRANSCRIPTIONS=1" in environment


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_base_compose_profiles_do_not_bind_host_model_envelope_evidence(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    joined = "\n".join(volumes)

    assert "/var/lib/subgen/model-envelopes" not in joined
    assert "/opt/subgen/model-envelopes" not in joined


@pytest.mark.parametrize(
    "compose_path",
    ["docker-compose.yml", "docker-compose.ghcr.yml", "docker-compose.gpu.yml"],
)
def test_base_compose_profiles_do_not_bind_optional_priority_signal(compose_path):
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    volumes = "\n".join(_nested_yaml_block(compose, "services", "subgen", "volumes"))

    assert "/run/subgen-priority" not in volumes


def test_priority_overlay_binds_only_parent_read_only_without_host_path_creation():
    overlay = (ROOT / "docker-compose.priority-pressure.yml").read_text(
        encoding="utf-8"
    )
    environment = _nested_yaml_block(overlay, "services", "subgen", "environment")
    entries = _long_volume_entries(overlay)

    assert environment == [
        "      - PRIORITY_PRESSURE_FILE=${PRIORITY_PRESSURE_FILE:-/run/subgen-priority/pressure.json}"
    ]
    assert entries == [
        {
            "type": "bind",
            "source": "/run/subgen-priority",
            "target": "/run/subgen-priority",
            "read_only": "true",
            "bind.create_host_path": "false",
        }
    ]
    assert "source: /run/subgen-priority/pressure.json" not in overlay


def test_model_envelope_overlay_binds_exact_artifacts_without_host_path_creation():
    overlay = (ROOT / "docker-compose.model-envelopes.yml").read_text(
        encoding="utf-8"
    )
    entries = _long_volume_entries(overlay)
    expected = (
        (
            "/var/lib/subgen/model-envelopes/v1",
            "/opt/subgen/model-envelopes",
        ),
        (
            "/var/lib/subgen/model-envelopes/v1/catalog.json",
            "/opt/subgen/model-envelopes/catalog.json",
        ),
        (
            "/var/lib/subgen/model-envelopes/v1/image-identity.json",
            "/opt/subgen/model-envelopes/image-identity.json",
        ),
    )

    assert tuple(
        (entry.get("source"), entry.get("target")) for entry in entries
    ) == expected
    for entry in entries:
        assert entry.get("type") == "bind"
        assert entry.get("read_only") == "true"
        assert entry.get("bind.create_host_path") == "false"


@pytest.mark.parametrize(
    ("compose_path", "expected_delay"),
    [
        ("docker-compose.yml", "60"),
        ("docker-compose.ghcr.yml", "60"),
        ("docker-compose.gpu.yml", "300"),
    ],
)
def test_model_cleanup_delay_uses_profile_default_when_example_is_blank(
    compose_path,
    expected_delay,
):
    example_lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    compose = (ROOT / compose_path).read_text(encoding="utf-8")
    environment = _nested_yaml_block(compose, "services", "subgen", "environment")

    assert "MODEL_CLEANUP_DELAY=" in example_lines
    assert (
        f"      - MODEL_CLEANUP_DELAY=${{MODEL_CLEANUP_DELAY:-{expected_delay}}}"
        in environment
    )


@pytest.mark.parametrize(
    "workflow_path",
    [".github/workflows/test.yml", ".github/workflows/publish-ghcr.yml"],
)
def test_github_workflows_are_manual_only(workflow_path):
    workflow = (ROOT / workflow_path).read_text(encoding="utf-8")
    triggers = [line.strip() for line in _nested_yaml_block(workflow, "on") if line.strip()]
    assert triggers == ["workflow_dispatch:"]


def test_release_version_is_0_5_0():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.5.0"


@pytest.mark.parametrize("path", ["README.md", "docs/CONFIGURATION.md"])
def test_public_docs_cover_direct_file_standalone_mode(path):
    text = (ROOT / path).read_text(encoding="utf-8")
    section = _markdown_section(text, "standalone")
    assert "TRANSCRIBE_FOLDERS" in section
    assert "MONITOR" in section
    assert "Plex" in section
    assert any(term in section.casefold() for term in ("optional", "without", "not require"))

    assignments = re.findall(r"^TRANSCRIBE_FOLDERS=(.+)$", section, flags=re.MULTILINE)
    assert any(
        "|" not in value
        and any(extension in value.casefold() for extension in (".mkv", ".mp4", ".mov"))
        for value in assignments
    )
    assert any("|" in value for value in assignments)


def test_readme_source_map_names_subgen_core():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "`subgen_core/`" in readme


def test_readme_quick_start_uses_runnable_missing_evidence_base_profile():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    quick_start = _markdown_section(readme, "Quick start")

    assert "/var/lib/subgen/model-envelopes" not in quick_start
    assert "docker-compose.model-envelopes.yml" not in quick_start
    assert "docker compose -f docker-compose.ghcr.yml config --quiet" in quick_start
    assert "docker compose -f docker-compose.ghcr.yml up -d" in quick_start


def test_readme_separates_public_fallbacks_from_frigate_shared_gpu_policy():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    hardware = _markdown_section(readme, "Model and hardware")
    frigate = _markdown_section(readme, "Frigate deployment boundary")

    for capacity in ("4 GiB", "6 GiB", "9 GiB"):
        assert capacity in hardware
    assert "fallback" in hardware.casefold()
    assert "ModelEnvelope" in hardware
    assert "large-v3" in hardware
    assert "never silently downgraded" in hardware
    assert "RTX 3090" in frigate
    assert "GPU_MEMORY_RESERVE_GIB" in frigate
    assert "auto" in frigate
    assert "20 GiB guaranteed balloon floor" in frigate
    assert "17 GiB hard/no-extra-swap" in frigate
    assert "earlier 10/12 GiB" in frigate
    assert "v0.3.0" in frigate


def test_contributor_compile_check_covers_subgen_core():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    commands = [
        line.strip()
        for line in contributing.splitlines()
        if line.strip().startswith("python -m compileall")
    ]
    assert any("subgen_core" in command.split() for command in commands)
    assert any("subgen_failure_markers.py" in command.split() for command in commands)
    assert any("profile_model_envelopes.py" in command.split() for command in commands)
    assert any("monitor_frigate_priority.py" in command.split() for command in commands)


def test_contributing_validates_every_base_with_and_without_evidence_overlay():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    overlay = "docker-compose.model-envelopes.yml"

    for base in (
        "docker-compose.yml",
        "docker-compose.gpu.yml",
        "docker-compose.ghcr.yml",
    ):
        assert f"docker compose -f {base} config --quiet" in contributing
        assert (
            f"docker compose -f {base} -f {overlay} config --quiet"
            in contributing
        )
        assert (
            f"docker compose -f {base} -f docker-compose.priority-pressure.yml config --quiet"
            in contributing
        )
    assert (
        "docker compose -f docker-compose.gpu.yml "
        "-f docker-compose.model-envelopes.yml "
        "-f docker-compose.priority-pressure.yml config --quiet"
    ) in contributing


def test_public_environment_defaults_share_marker_state():
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    lines = environment.splitlines()
    assert "SUBGEN_STATE_DIR=./monitor" in lines
    assert "SKIP_STARTUP_SCAN=False" in lines
    assert "SKIP_MARKED_FAILED_FILES=true" in lines
    assert "WHISPER_MODEL=auto" in lines
    assert "SEGMENTATION_ENABLED=True" in lines
    assert "SEGMENTATION_CHUNK_MINUTES=auto" in lines
    assert "MEMORY_PRESSURE_YIELD=True" in lines
    assert "MEMORY_PRESSURE_RESERVE_GIB=auto" in lines
    assert "PRIORITY_PRESSURE_FILE=" in lines
    assert "GPU_MEMORY_RESERVE_GIB=auto" in lines
    assert "MODEL_ENVELOPE_CATALOG=/opt/subgen/model-envelopes/catalog.json" in lines
    assert (
        "MODEL_ENVELOPE_IDENTITY=/opt/subgen/model-envelopes/image-identity.json"
        in lines
    )

    monitor_environment = (ROOT / "monitor.env.example").read_text(encoding="utf-8")
    assert "AUTO_MARK_FAILED_FILES=true" in monitor_environment.splitlines()
    assert "AUTO_MARK_MIN_FAILURES=1" in monitor_environment.splitlines()
    assert "AUTO_DELETE_INVALID_MEDIA=false" in monitor_environment.splitlines()
    assert "AUTO_DELETE_FAILED_FILES=false" in monitor_environment.splitlines()
    assert "AUTO_DELETE_MIN_FAILURES=1" in monitor_environment.splitlines()
    assert "SUBGEN_REPAIR_ACTION=report" in monitor_environment.splitlines()

    priority_environment = (ROOT / "priority-monitor.env.example").read_text(
        encoding="utf-8"
    )
    priority_lines = priority_environment.splitlines()
    assert "FRIGATE_PRIORITY_SIGNAL_FILE=/run/subgen-priority/pressure.json" in priority_lines
    assert "FRIGATE_PRIORITY_ORIGIN=http://127.0.0.1:5000" in priority_lines
    assert "OLLAMA_PRIORITY_ORIGIN=http://127.0.0.1:11434" in priority_lines
    assert (
        "FRIGATE_PRIORITY_POLICY_FILE=/var/lib/subgen-priority/private/"
        "frigate-priority-policy.json"
    ) in priority_lines
    assert "FRIGATE_PRIORITY_POLICY_SHA256=" in priority_lines


def test_priority_monitor_private_environment_is_ignored_and_not_built():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "priority-monitor.env" in gitignore
    assert "priority-monitor.env" in dockerignore
    for ignored in (
        "*frigate-priority-policy*.json",
        "*priority-policy-draft*.json",
        "*private-policy-draft*.json",
    ):
        assert ignored in gitignore
        assert ignored in dockerignore
    assert "monitor_frigate_priority.py" not in dockerfile


def test_priority_monitor_unit_is_owner_only_low_priority_and_docker_independent():
    unit = (ROOT / "systemd" / "subgen-priority-monitor.service").read_text(
        encoding="utf-8"
    )

    for required in (
        "EnvironmentFile=/opt/subgen/priority-monitor.env",
        "UMask=0077",
        "RuntimeDirectory=subgen-priority",
        "RuntimeDirectoryMode=0700",
        "RuntimeDirectoryPreserve=yes",
        "Before=docker.service",
        "ExecStart=/usr/bin/python3 /opt/subgen/monitor_frigate_priority.py",
        "Restart=always",
        "Nice=19",
        "CPUWeight=1",
        "IOWeight=1",
    ):
        assert required in unit.splitlines()
    assert "SupplementaryGroups=docker" not in unit
    assert "Requires=docker.service" not in unit


def test_priority_documentation_preserves_optional_fail_closed_boundary():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    migration = (ROOT / "docs" / "MIGRATION.md").read_text(encoding="utf-8")
    combined = "\n".join((readme, configuration, install, migration))

    for document in (readme, configuration, install, migration):
        assert "PRIORITY_PRESSURE_FILE" in document
    assert "docker-compose.priority-pressure.yml" in combined
    assert "FRIGATE_PRIORITY_SIGNAL_FILE" in combined
    assert "fail-closed" in combined.casefold()
    assert re.search(r"\bmode(?:-| )`0700`", combined)
    assert re.search(r"\bmode(?:-| )`0600`", combined)
    assert "Before=docker.service" in combined
    assert "custom or rootless" in combined
    assert "/var/lib/subgen-priority/private" in combined
    assert "/opt/subgen/private" not in combined


def test_priority_policy_documentation_marks_fixed_schema_constants():
    configuration = (ROOT / "docs" / "CONFIGURATION.md").read_text(
        encoding="utf-8"
    )

    assert "fixed v0.5 schema constants" in configuration
    assert "`detection_fps_limit=80.0`" in configuration
    assert "`source_max_age_seconds=30`" in configuration


def test_upgrade_preserves_selected_base_and_active_overlays():
    install = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    upgrade = _markdown_exact_section(install, "Upgrade")

    assert "git fetch --tags --prune origin" in upgrade
    assert "git switch --detach v0.5.0" in upgrade
    assert "compose_args=(-f docker-compose.ghcr.yml)" in upgrade
    assert "compose_args=(-f docker-compose.gpu.yml)" in upgrade
    assert "compose_args=(-f docker-compose.yml)" in upgrade
    assert "compose_args+=(-f docker-compose.model-envelopes.yml)" in upgrade
    assert "compose_args+=(-f docker-compose.priority-pressure.yml)" in upgrade
    assert 'docker compose "${compose_args[@]}" config --quiet' in upgrade
    assert 'docker compose "${compose_args[@]}" pull' in upgrade
    assert 'docker compose "${compose_args[@]}" up -d' in upgrade
    assert "git pull" not in upgrade


def test_release_metadata_matches_version():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release_notes = (ROOT / "docs" / f"RELEASE_NOTES_{version}.md").read_text(
        encoding="utf-8"
    )

    assert f"## [{version}]" in changelog
    assert f"compare/v{PREVIOUS_RELEASE}...v{version}" in changelog
    assert release_notes.startswith(f"# Subgen English for Plex {version}\n")
    for heading in ("Back up", "Upgrade", "smoke test", "Rollback", "Known boundaries"):
        assert heading.casefold() in release_notes.casefold()


def test_release_notes_are_human_facing_and_compare_supported_releases():
    notes = (ROOT / "docs" / "RELEASE_NOTES_0.5.0.md").read_text(
        encoding="utf-8"
    )
    comparison = _markdown_section(notes, "Compared with earlier releases")
    for version in ("v0.4.0", "v0.4.1", "v0.5.0"):
        assert version in comparison
    assert "5 to 30 minutes" in notes
    assert "model weights" in notes
    assert "public defaults" in notes.casefold()
    assert "Frigate" in notes
    assert "fixed for the process" in notes
    assert "three consecutive healthy" in notes
    assert "optional failure monitor" in notes
    assert "does not dispatch a\nGitHub Actions workflow" in notes
    assert "commit" not in _markdown_section(notes, "Highlights").casefold()


def test_release_notes_keep_exact_human_facing_structure():
    notes = (ROOT / "docs" / "RELEASE_NOTES_0.5.0.md").read_text(
        encoding="utf-8"
    )
    intro = notes.split("\n## ", 1)[0].split("\n", 1)[1]
    sentences = re.split(
        r"(?<=[.!?])\s+(?=[A-Z])",
        " ".join(intro.split()),
    )

    assert sentences[0] == (
        "A longer film should take longer to transcribe, not require "
        "ever-growing RAM."
    )
    assert "hardware-sized time windows" in intro
    assert "only the current window in memory" in intro
    assert "model weights" in intro
    assert "highest-quality safe multilingual Whisper model" in intro
    assert _markdown_h2_headings(notes) == RELEASE_H2_HEADINGS


@pytest.mark.parametrize(
    ("unit_path", "expected_description"),
    [
        (
            "systemd/subgen-repair.service",
            "Description=Report Subgen repeated-crash candidates without deleting media",
        ),
        (
            "systemd/subgen-repair.timer",
            "Description=Run Subgen repeated-crash reporting periodically",
        ),
    ],
)
def test_repair_units_use_approved_report_only_descriptions(
    unit_path,
    expected_description,
):
    lines = (ROOT / unit_path).read_text(encoding="utf-8").splitlines()
    assert lines[1] == expected_description


def test_adr_and_index_record_marker_registry_decision():
    adr_path = ROOT / "docs" / "aegis" / "adr" / "0001-generation-bound-failure-marker-registry.md"
    index = (ROOT / "docs" / "aegis" / "INDEX.md").read_text(encoding="utf-8")

    assert adr_path.is_file()
    assert "Status: Accepted" in adr_path.read_text(encoding="utf-8")
    assert "adr/0001-generation-bound-failure-marker-registry.md" in index


def test_proposed_v0_5_adr_is_indexed_without_premature_acceptance():
    adr_path = (
        ROOT
        / "docs"
        / "aegis"
        / "adr"
        / "0002-memory-aware-segmented-transcription.md"
    )
    index = (ROOT / "docs" / "aegis" / "INDEX.md").read_text(encoding="utf-8")
    adr = adr_path.read_text(encoding="utf-8")

    assert "Status: Proposed" in adr
    assert "Status: Accepted" not in adr
    assert "adr/0002-memory-aware-segmented-transcription.md" in index
    assert "acceptance pending" in index.casefold()


def test_contributing_requires_local_or_idle_simulator_verification():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "GitHub-hosted runners are disabled" in contributing
    assert "simulator PC" in contributing
    assert "no other user" in contributing
    assert "only if your task woke it" in contributing
    assert "not dispatched for v0.5.0" in contributing
    assert "image publication" in contributing
