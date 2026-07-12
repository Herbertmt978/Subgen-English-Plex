"""Contracts that keep modular code in packaged and source-bind deployments."""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_RUNTIME_IMAGE = (
    "mccloud/subgen@sha256:"
    "128a16bae4f6296fbddd95be3ff47a1c10815fdac6489a66e0b022f2b98c9076"
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


def test_image_copies_subgen_core_package():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = [line.strip() for line in dockerfile.splitlines() if line.strip()]
    assert "COPY subgen_core /subgen/subgen_core" in instructions


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


def test_source_compose_mounts_subgen_core_read_only():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    assert "      - ./subgen_core:/subgen/subgen_core:ro" in volumes


def test_image_workflow_watches_subgen_core_changes():
    workflow = (ROOT / ".github/workflows/publish-ghcr.yml").read_text(encoding="utf-8")
    paths = _nested_yaml_block(workflow, "on", "push", "paths")
    assert "      - subgen_core/**" in paths


def test_release_version_is_0_3_0():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.3.0"


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


def test_readme_recommends_rtx_3090_accuracy_profile():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    profile = _markdown_section(readme, "RTX 3090")
    expected_settings = {
        "WHISPER_MODEL=large-v3",
        "TRANSCRIBE_DEVICE=cuda",
        "COMPUTE_TYPE=float16",
        "CONCURRENT_TRANSCRIPTIONS=1",
        "SUBGEN_MEMORY_LIMIT=20g",
    }
    assert expected_settings.issubset(profile.split())
    assert "20 GB" in profile
    assert "host RAM" in profile


def test_contributor_compile_check_covers_subgen_core():
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    commands = [
        line.strip()
        for line in contributing.splitlines()
        if line.strip().startswith("python -m compileall")
    ]
    assert any("subgen_core" in command.split() for command in commands)
