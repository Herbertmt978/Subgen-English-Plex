"""Deliberately RED contracts for the approved ``subgen_core`` ownership split."""

import ast
import importlib
import inspect
import runpy
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import subgen
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "subgen_core"
MODULE_SLICES = [
    pytest.param(
        "queueing",
        {"__init__.py", "queueing.py"},
        id="queueing",
    ),
    pytest.param(
        "integrations",
        {"integrations/__init__.py", "integrations/plex.py", "integrations/jellyfin.py"},
        id="integrations",
    ),
    pytest.param(
        "media and scanner",
        {"media.py", "scanner.py"},
        id="media-scanner",
    ),
    pytest.param(
        "model runtime",
        {"model_runtime.py"},
        id="model-runtime",
    ),
    pytest.param(
        "transcription",
        {"transcription.py"},
        id="transcription",
    ),
]

MEDIA_FUNCTIONS = {
    "is_audio_file_extension",
    "define_subtitle_language_naming",
    "name_subtitle",
    "get_audio_track_by_language",
    "choose_transcribe_language",
    "get_audio_tracks",
    "validate_media",
    "is_media_validation_current",
    "find_language_audio_track",
    "find_default_audio_track_language",
    "select_audio_track",
    "gen_subtitles_queue",
    "should_skip_file",
    "get_subtitle_languages",
    "get_audio_languages",
    "subtitle_exists_in_language",
    "has_internal_subtitle_in_language",
    "has_external_subtitle_in_language",
    "is_valid_subtitle_language",
    "has_audio",
    "is_valid_path",
    "has_video_extension",
    "has_audio_extension",
    "path_mapping",
}
RUNTIME_MEDIA_FUNCTIONS = MEDIA_FUNCTIONS - {
    "get_audio_track_by_language",
    "find_language_audio_track",
    "find_default_audio_track_language",
    "select_audio_track",
    "is_valid_subtitle_language",
}
SCANNER_FUNCTIONS = {
    "is_file_stable",
    "_is_in_skipped_dir",
    "queue_existing",
    "transcribe_existing",
}
RUNTIME_SCANNER_FUNCTIONS = SCANNER_FUNCTIONS
MODEL_RUNTIME_FUNCTIONS = {
    "initialize_model_runtime",
    "observe_idle_once",
    "release_after_inference_failure",
    "release_model",
    "run_model_idle_observer",
    "transcribe_with_model",
    "start_model",
    "schedule_model_cleanup",
    "perform_model_cleanup",
    "delete_model",
    "wait_for_model_recovery",
}
TRANSCRIPTION_FUNCTIONS = {
    "get_audio_start_time",
    "apply_timestamp_offset",
    "asr_task_worker",
    "get_audio_chunk",
    "detect_language_from_upload",
    "extract_audio_segment_from_content",
    "detect_language_task",
    "extract_audio_segment_to_memory",
    "probe_media_duration",
    "write_lrc",
    "send_completion_webhook",
    "gen_subtitles",
    "handle_multiple_audio_tracks",
    "extract_audio_track_to_memory",
}
CANONICAL_CONSTANTS = {
    "VIDEO_EXTENSIONS": ("_media", "VIDEO_EXTENSIONS"),
    "AUDIO_EXTENSIONS": ("_media", "AUDIO_EXTENSIONS"),
    "SKIP_MARKER": ("_scanner", "SKIP_MARKER"),
}


def _model_runtime_module():
    source_file = CORE / "model_runtime.py"
    assert source_file.is_file(), "missing canonical model runtime owner"
    return importlib.import_module("subgen_core.model_runtime")


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


def test_bootstrap_keeps_app_and_legacy_exports():
    assert subgen.app is not None
    for name in (
        "DeduplicatedQueue",
        "TaskResult",
        "gen_subtitles",
        "transcribe_existing",
        "get_plex_file_name",
        "get_jellyfin_file_name",
    ):
        assert hasattr(subgen, name), name


def test_renamed_script_bootstraps_through_main_app(monkeypatch):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    instructions = [line.strip() for line in dockerfile.splitlines() if line.strip()]
    volumes = _nested_yaml_block(compose, "services", "subgen", "volumes")
    assert "COPY subgen_override.py /subgen/subgen.py" in instructions
    assert "      - ./subgen_override.py:/subgen/subgen.py:ro" in volumes

    run = MagicMock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    namespace = runpy.run_path(str(ROOT / "subgen_override.py"), run_name="__main__")

    assert namespace["app"] is not None
    run.assert_called_once()
    assert run.call_args.args == ("__main__:app",)
    assert run.call_args.kwargs["host"] == "0.0.0.0"
    assert run.call_args.kwargs["port"] == int(namespace["webhookport"])


@pytest.mark.parametrize("slice_name,expected_modules", MODULE_SLICES)
def test_approved_core_modules_exist_and_import(slice_name, expected_modules):
    missing = sorted(path for path in expected_modules if not (CORE / path).is_file())
    assert missing == [], f"missing {slice_name} module owners: {missing}"

    for path in sorted(expected_modules):
        if path.endswith("/__init__.py"):
            module_name = "subgen_core." + path.removesuffix("/__init__.py").replace("/", ".")
        elif path == "__init__.py":
            module_name = "subgen_core"
        else:
            module_name = "subgen_core." + path.removesuffix(".py").replace("/", ".")
        importlib.import_module(module_name)


def test_core_never_imports_executable_facade():
    if not CORE.is_dir():
        pytest.skip("subgen_core is created by the next extraction slice")

    forbidden = {"subgen", "subgen_override"}
    violations = []
    for source_file in CORE.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".", 1)[0]}
            else:
                continue
            if names & forbidden:
                violations.append(f"{source_file.relative_to(ROOT)}:{node.lineno}")

    assert violations == [], f"core imports executable facade: {violations}"


def test_owner_operated_profiler_uses_core_owners_without_reverse_dependency():
    profiler_path = ROOT / "profile_model_envelopes.py"
    profiler_tree = ast.parse(
        profiler_path.read_text(encoding="utf-8"),
        filename=str(profiler_path),
    )
    imported_core_owners = {
        alias.name
        for node in ast.walk(profiler_tree)
        if isinstance(node, ast.ImportFrom) and node.module == "subgen_core"
        for alias in node.names
    }
    assert {
        "backend_release",
        "model_envelope_catalog",
        "priority_pressure",
        "resource_management",
    } <= imported_core_owners

    profiler_source = profiler_path.read_text(encoding="utf-8")
    runtime_source = (CORE / "model_runtime.py").read_text(encoding="utf-8")
    shared_release_call = "_backend_release.unload_verified_backend"
    assert "backend_release_owner.unload_verified_backend" in profiler_source
    assert shared_release_call in runtime_source

    reverse_dependencies = []
    for source_file in CORE.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module}
            else:
                continue
            if any(name.split(".", 1)[0] == "profile_model_envelopes" for name in names):
                reverse_dependencies.append(f"{source_file.relative_to(ROOT)}:{node.lineno}")

    assert reverse_dependencies == []


def test_model_runtime_has_canonical_algorithm_owners():
    source_file = CORE / "model_runtime.py"
    assert source_file.is_file(), "missing canonical model runtime owner"

    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }

    assert MODEL_RUNTIME_FUNCTIONS <= functions.keys()
    for function_name in MODEL_RUNTIME_FUNCTIONS:
        assert functions[function_name].args.args[0].arg == "runtime", function_name

    model_runtime = _model_runtime_module()
    root_owned_state = {
        "model",
        "model_cleanup_timer",
        "model_cleanup_lock",
        "model_load_lock",
        "model_selection_lock",
        "model_inference_semaphore",
        "model_inference_permit_count",
        "model_runtime_condition",
        "model_admission_closed",
        "model_release_generation",
        "model_release_transition",
        "model_active_inferences",
        "model_load_generation",
        "model_unload_generation",
        "cuda_oom_generation",
        "media_failure_generation",
        "model_runtime_initialized",
        "model_decision",
        "model_requirement",
        "model_pressure_controller",
        "model_capacity_profile",
        "model_chunk_baseline_seconds",
        "model_stabilized_gpu",
        "model_runtime_cancel_event",
        "model_permit_wait_seconds",
        "model_load_allocation_failures",
        "model_profile_unhealthy",
        "model_profile_unhealthy_reason",
        "model_runtime_status",
        "model_idle_observer_stop",
        "priority_pressure_probe",
        "priority_pressure_reader",
        "active_direct_tasks",
        "active_direct_tasks_lock",
    }
    assert root_owned_state.isdisjoint(vars(model_runtime))


def test_model_runtime_facade_signatures_match_canonical_owners():
    model_runtime = _model_runtime_module()

    for function_name in MODEL_RUNTIME_FUNCTIONS:
        owner_signature = inspect.signature(getattr(model_runtime, function_name))
        owner_signature = owner_signature.replace(
            parameters=tuple(owner_signature.parameters.values())[1:]
        )
        facade_signature = inspect.signature(getattr(subgen, function_name))
        if function_name == "wait_for_model_recovery":
            assert tuple(facade_signature.parameters) == ()
        else:
            assert facade_signature == owner_signature


def test_model_runtime_facade_is_algorithm_free():
    source_file = ROOT / "subgen_override.py"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }

    for function_name in MODEL_RUNTIME_FUNCTIONS:
        function = functions[function_name]
        statements = [
            statement
            for statement in function.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(statements) == 1, f"{function_name} still contains an algorithm"
        assert isinstance(statements[0], ast.Return), f"{function_name} is not a delegate"
        call = statements[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert (call.func.value.id, call.func.attr) == (
            "_model_runtime",
            function_name,
        )
        runtime_argument = call.args[0]
        assert isinstance(runtime_argument, ast.Call)
        assert isinstance(runtime_argument.func, ast.Name)
        assert runtime_argument.func.id == "_runtime"

        if function_name == "transcribe_with_model":
            forwarded_args = call.args[1:]
            assert len(forwarded_args) == 1
            assert isinstance(forwarded_args[0], ast.Starred)
            assert isinstance(forwarded_args[0].value, ast.Name)
            assert forwarded_args[0].value.id == function.args.vararg.arg
            assert len(call.keywords) == 1
            assert call.keywords[0].arg is None
            assert isinstance(call.keywords[0].value, ast.Name)
            assert call.keywords[0].value.id == function.args.kwarg.arg
        elif function_name in {
            "release_after_inference_failure",
            "release_model",
        }:
            assert len(call.args[1:]) == 1
            assert isinstance(call.args[1], ast.Name)
            assert call.args[1].id == function.args.args[0].arg
            assert call.keywords == []
        elif function_name == "wait_for_model_recovery":
            assert len(call.args[1:]) == 1
            assert isinstance(call.args[1], ast.Name)
            assert call.args[1].id == "model_runtime_cancel_event"
            assert call.keywords == []
        else:
            assert call.args[1:] == []
            assert call.keywords == []


def test_model_runtime_resolves_dependencies_through_runtime():
    source_file = CORE / "model_runtime.py"
    assert source_file.is_file(), "missing canonical model runtime owner"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))

    forbidden_imports = {
        "ctypes",
        "gc",
        "logging",
        "os",
        "queue",
        "stable_whisper",
        "threading",
        "torch",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported.isdisjoint(forbidden_imports), imported & forbidden_imports

    forbidden_core_dependencies = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = {
                node.module,
                *(f"{node.module}.{alias.name}" for alias in node.names),
            }
        else:
            continue
        for name in names:
            if any(
                part in name
                for part in (
                    "integrations",
                    "media",
                    "queueing",
                    "scanner",
                    "transcription",
                )
            ):
                forbidden_core_dependencies.append(
                    f"{source_file.relative_to(ROOT)}:{node.lineno}:{name}"
                )

    assert forbidden_core_dependencies == []


def test_transcription_has_canonical_runtime_first_algorithm_owners():
    source_file = CORE / "transcription.py"
    assert source_file.is_file(), "missing canonical transcription owner"

    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert TRANSCRIPTION_FUNCTIONS <= functions.keys()
    for function_name in TRANSCRIPTION_FUNCTIONS:
        assert functions[function_name].args.args[0].arg == "runtime", function_name
    assert isinstance(functions["get_audio_chunk"], ast.AsyncFunctionDef)


def test_transcription_facade_preserves_signatures_and_defaults():
    expected_parameters = {
        "get_audio_start_time": ("video_path",),
        "apply_timestamp_offset": ("result", "offset"),
        "asr_task_worker": ("task_data",),
        "get_audio_chunk": (
            "audio_file", "offset", "length", "sample_rate", "audio_format",
        ),
        "detect_language_from_upload": ("task_data",),
        "extract_audio_segment_from_content": (
            "audio_content", "start_time", "duration",
        ),
        "detect_language_task": ("path", "original_task_data"),
        "extract_audio_segment_to_memory": (
            "input_file", "start_time", "duration", "track_index",
        ),
        "probe_media_duration": ("file_path",),
        "write_lrc": ("result", "file_path"),
        "send_completion_webhook": (
            "source_file_path", "subtitle_file_path", "language", "task_type",
        ),
        "gen_subtitles": (
            "file_path", "transcription_type", "force_language", "audio_tracks",
            "audio_track_index", "media_validation",
        ),
        "handle_multiple_audio_tracks": (
            "file_path", "language", "audio_tracks", "audio_track_index",
        ),
        "extract_audio_track_to_memory": ("input_video_path", "track_index"),
    }
    expected_defaults = {
        "get_audio_chunk": {
            "offset": subgen.detect_language_offset,
            "length": subgen.detect_language_length,
            "sample_rate": 16000,
            "audio_format": subgen.np.int16,
        },
        "detect_language_task": {"original_task_data": None},
        "extract_audio_segment_to_memory": {"track_index": None},
        "gen_subtitles": {
            "force_language": subgen.LanguageCode.NONE,
            "audio_tracks": None,
            "audio_track_index": None,
            "media_validation": None,
        },
        "handle_multiple_audio_tracks": {
            "language": None,
            "audio_tracks": None,
            "audio_track_index": None,
        },
    }

    for function_name, parameter_names in expected_parameters.items():
        signature = inspect.signature(getattr(subgen, function_name))
        assert tuple(signature.parameters) == parameter_names
        defaults = expected_defaults.get(function_name, {})
        for parameter_name, expected_default in defaults.items():
            actual_default = signature.parameters[parameter_name].default
            if parameter_name == "audio_format":
                assert actual_default is expected_default
            else:
                assert actual_default == expected_default
        for parameter_name in set(parameter_names) - defaults.keys():
            assert (
                signature.parameters[parameter_name].default
                is inspect.Parameter.empty
            )


def test_transcription_facade_is_algorithm_free_and_runtime_aware():
    source_file = ROOT / "subgen_override.py"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for function_name in TRANSCRIPTION_FUNCTIONS:
        function = functions[function_name]
        statements = [
            statement
            for statement in function.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(statements) == 1, f"{function_name} still contains an algorithm"
        assert isinstance(statements[0], ast.Return), f"{function_name} is not a delegate"
        value = statements[0].value
        if function_name == "get_audio_chunk":
            assert isinstance(value, ast.Await)
            value = value.value
        assert isinstance(value, ast.Call)
        assert isinstance(value.func, ast.Attribute)
        assert isinstance(value.func.value, ast.Name)
        assert (value.func.value.id, value.func.attr) == (
            "_transcription",
            function_name,
        )
        runtime_argument = value.args[0]
        assert isinstance(runtime_argument, ast.Call)
        assert isinstance(runtime_argument.func, ast.Name)
        assert runtime_argument.func.id == "_runtime"
        assert [
            argument.id if isinstance(argument, ast.Name) else None
            for argument in value.args[1:]
        ] == [argument.arg for argument in function.args.args]
        assert value.keywords == []


def test_transcription_resolves_heavy_and_patchable_dependencies_through_runtime():
    source_file = CORE / "transcription.py"
    assert source_file.is_file(), "missing canonical transcription owner"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    forbidden_names = {
        "LanguageCode",
        "ffmpeg",
        "json",
        "logging",
        "np",
        "os",
        "requests",
        "subprocess",
        "time",
    }
    violations = sorted(
        {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        & forbidden_names
    )
    assert violations == []

    forbidden_core_dependencies = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = {node.module}
        else:
            continue
        for name in names:
            if any(
                dependency in name
                for dependency in (
                    "integrations", "media", "model_runtime", "queueing", "scanner",
                )
            ):
                forbidden_core_dependencies.append(
                    f"{source_file.relative_to(ROOT)}:{node.lineno}:{name}"
                )
    assert forbidden_core_dependencies == []


def test_integration_clients_never_enqueue_work():
    integration_files = [
        CORE / "integrations" / "plex.py",
        CORE / "integrations" / "jellyfin.py",
    ]
    missing = [source_file.relative_to(ROOT) for source_file in integration_files if not source_file.is_file()]
    assert missing == [], f"missing integration module owners: {missing}"

    violations = []
    for source_file in integration_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in {"task_queue", "gen_subtitles_queue"}:
                violations.append(f"{source_file.relative_to(ROOT)}:{node.lineno}:{node.id}")
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.endswith("queueing")
            ):
                violations.append(f"{source_file.relative_to(ROOT)}:{node.lineno}:{node.module}")

    assert violations == [], f"integration clients enqueue work: {violations}"


def test_integration_facade_wrappers_are_algorithm_free():
    source_file = ROOT / "subgen_override.py"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expected = {
        "get_next_plex_episode": ("_plex_client", ("current_episode_rating_key", "stay_in_season")),
        "get_plex_file_name": ("_plex_client", ("itemid", "server_ip", "plex_token")),
        "refresh_plex_metadata": ("_plex_client", ("itemid", "server_ip", "plex_token")),
        "refresh_jellyfin_metadata": ("_jellyfin_client", ("itemid", "server_ip", "jellyfin_token")),
        "get_jellyfin_file_name": ("_jellyfin_client", ("item_id", "jellyfin_url", "jellyfin_token")),
        "get_jellyfin_admin": ("_jellyfin_client", ("users",)),
    }

    for function_name, (owner_name, parameter_names) in expected.items():
        function = functions[function_name]
        assert tuple(argument.arg for argument in function.args.args) == parameter_names
        statements = [
            statement
            for statement in function.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(statements) == 1, f"{function_name} still contains an algorithm"
        assert isinstance(statements[0], ast.Return), f"{function_name} is not a delegating wrapper"
        call = statements[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert call.func.value.id == owner_name
        assert call.func.attr == function_name


def test_media_and_scanner_have_canonical_algorithm_owners():
    media_file = CORE / "media.py"
    scanner_file = CORE / "scanner.py"
    assert media_file.is_file(), "missing canonical media owner"
    assert scanner_file.is_file(), "missing canonical scanner owner"

    media_tree = ast.parse(media_file.read_text(encoding="utf-8"), filename=str(media_file))
    scanner_tree = ast.parse(scanner_file.read_text(encoding="utf-8"), filename=str(scanner_file))
    media_functions = {
        node.name: node for node in media_tree.body if isinstance(node, ast.FunctionDef)
    }
    scanner_functions = {
        node.name: node for node in scanner_tree.body if isinstance(node, ast.FunctionDef)
    }
    scanner_classes = {
        node.name: node for node in scanner_tree.body if isinstance(node, ast.ClassDef)
    }

    assert MEDIA_FUNCTIONS <= media_functions.keys()
    assert SCANNER_FUNCTIONS <= scanner_functions.keys()
    assert "NewFileHandler" in scanner_classes
    for name in RUNTIME_MEDIA_FUNCTIONS:
        assert media_functions[name].args.args[0].arg == "runtime", name
    for name in RUNTIME_SCANNER_FUNCTIONS:
        assert scanner_functions[name].args.args[0].arg == "runtime", name
    handler_init = next(
        node
        for node in scanner_classes["NewFileHandler"].body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert tuple(argument.arg for argument in handler_init.args.args[:2]) == ("self", "runtime")


def test_media_and_scanner_constants_have_canonical_owners():
    media = importlib.import_module("subgen_core.media")
    scanner = importlib.import_module("subgen_core.scanner")
    assert subgen.VIDEO_EXTENSIONS is media.VIDEO_EXTENSIONS
    assert subgen.AUDIO_EXTENSIONS is media.AUDIO_EXTENSIONS
    assert subgen.SKIP_MARKER is scanner.SKIP_MARKER

    source_file = ROOT / "subgen_override.py"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    assignments = {
        node.targets[0].id: node.value
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        )
    }
    for constant_name, (owner_name, owner_constant) in CANONICAL_CONSTANTS.items():
        value = assignments[constant_name]
        assert isinstance(value, ast.Attribute)
        assert isinstance(value.value, ast.Name)
        assert (value.value.id, value.attr) == (owner_name, owner_constant)


def test_media_and_scanner_facade_signatures_match_canonical_owners():
    media = importlib.import_module("subgen_core.media")
    scanner = importlib.import_module("subgen_core.scanner")
    delegates = {
        **{name: (media, name in RUNTIME_MEDIA_FUNCTIONS) for name in MEDIA_FUNCTIONS},
        **{name: (scanner, name in RUNTIME_SCANNER_FUNCTIONS) for name in SCANNER_FUNCTIONS},
    }

    for function_name, (owner, takes_runtime) in delegates.items():
        owner_signature = inspect.signature(getattr(owner, function_name))
        if takes_runtime:
            owner_signature = owner_signature.replace(
                parameters=tuple(owner_signature.parameters.values())[1:]
            )
        assert inspect.signature(getattr(subgen, function_name)) == owner_signature


def test_media_and_scanner_facade_is_algorithm_free():
    source_file = ROOT / "subgen_override.py"
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    delegates = {
        **{name: ("_media", name in RUNTIME_MEDIA_FUNCTIONS) for name in MEDIA_FUNCTIONS},
        **{name: ("_scanner", name in RUNTIME_SCANNER_FUNCTIONS) for name in SCANNER_FUNCTIONS},
    }

    for function_name, (owner_name, takes_runtime) in delegates.items():
        function = functions[function_name]
        statements = [
            statement
            for statement in function.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        assert len(statements) == 1, f"{function_name} still contains an algorithm"
        assert isinstance(statements[0], ast.Return), f"{function_name} is not a delegate"
        call = statements[0].value
        assert isinstance(call, ast.Call)
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert (call.func.value.id, call.func.attr) == (owner_name, function_name)
        if takes_runtime:
            runtime_argument = call.args[0]
            assert isinstance(runtime_argument, ast.Call)
            assert isinstance(runtime_argument.func, ast.Name)
            assert runtime_argument.func.id == "_runtime"

        forwarded_arguments = call.args[1:] if takes_runtime else call.args
        parameter_names = [argument.arg for argument in function.args.args]
        assert [
            argument.id if isinstance(argument, ast.Name) else None
            for argument in forwarded_arguments
        ] == parameter_names
        if function.args.kwarg is None:
            assert call.keywords == []
        else:
            assert len(call.keywords) == 1
            assert call.keywords[0].arg is None
            assert isinstance(call.keywords[0].value, ast.Name)
            assert call.keywords[0].value.id == function.args.kwarg.arg

    handler = classes["NewFileHandler"]
    assert len(handler.bases) == 1
    assert isinstance(handler.bases[0], ast.Attribute)
    assert isinstance(handler.bases[0].value, ast.Name)
    assert (handler.bases[0].value.id, handler.bases[0].attr) == (
        "_scanner",
        "NewFileHandler",
    )
    facade_handler_methods = {
        node.name for node in handler.body if isinstance(node, ast.FunctionDef)
    }
    assert facade_handler_methods == {"__init__"}
    assert subgen.NewFileHandler().runtime is subgen

    runtime_function = functions["_runtime"]
    assert subgen._runtime() is subgen
    assert len(runtime_function.body) == 1
    assert isinstance(runtime_function.body[0], ast.Return)


def test_media_and_scanner_have_no_integration_or_queue_dependency():
    forbidden = []
    for source_file in (CORE / "media.py", CORE / "scanner.py"):
        assert source_file.is_file(), f"missing canonical owner: {source_file.name}"
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module}
            else:
                continue
            for name in names:
                if "integrations" in name or name.endswith("queueing"):
                    forbidden.append(
                        f"{source_file.relative_to(ROOT)}:{node.lineno}:{name}"
                    )

    assert forbidden == [], f"media/scanner bypass runtime callbacks: {forbidden}"


def test_media_is_the_only_failure_marker_enforcement_owner():
    media_file = CORE / "media.py"
    scanner_file = CORE / "scanner.py"
    media_tree = ast.parse(media_file.read_text(encoding="utf-8"), filename=str(media_file))
    scanner_tree = ast.parse(
        scanner_file.read_text(encoding="utf-8"),
        filename=str(scanner_file),
    )
    media_queue = next(
        node
        for node in media_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "gen_subtitles_queue"
    )
    marker_checks = [
        node.lineno
        for node in ast.walk(media_queue)
        if isinstance(node, ast.Attribute) and node.attr == "failure_marker_reader"
    ]
    audio_probes = [
        node.lineno
        for node in ast.walk(media_queue)
        if isinstance(node, ast.Attribute) and node.attr == "validate_media"
    ]
    scanner_probe_or_marker_refs = [
        f"{getattr(node, 'attr', getattr(node, 'id', 'unknown'))}:{node.lineno}"
        for node in ast.walk(scanner_tree)
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {
                "failure_marker_reader",
                "validate_media",
                "skip_marked_failed_files",
            }
        )
        or (
            isinstance(node, ast.Name)
            and node.id in {
                "failure_marker_reader",
                "skip_marked_failed_files",
            }
        )
    ]

    assert len(marker_checks) == 1
    assert len(audio_probes) == 1
    assert marker_checks[0] < audio_probes[0]
    assert scanner_probe_or_marker_refs == []


def test_runtime_media_functions_resolve_filesystem_through_runtime():
    media_file = CORE / "media.py"
    tree = ast.parse(media_file.read_text(encoding="utf-8"), filename=str(media_file))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    violations = [
        f"{function_name}:{node.lineno}"
        for function_name in sorted(RUNTIME_MEDIA_FUNCTIONS)
        for node in ast.walk(functions[function_name])
        if isinstance(node, ast.Name) and node.id == "os"
    ]

    assert violations == [], f"runtime media functions bypass runtime.os: {violations}"


def test_legacy_queue_exports_have_a_canonical_owner():
    package = importlib.import_module("subgen_core")
    queueing = importlib.import_module("subgen_core.queueing")
    assert subgen.TaskResult is queueing.TaskResult
    assert subgen.DeduplicatedQueue is queueing.DeduplicatedQueue
    assert subgen.task_event_id is queueing.task_event_id
    assert subgen.generate_audio_hash is queueing.generate_audio_hash
    assert package.TaskResult is queueing.TaskResult
    assert package.DeduplicatedQueue is queueing.DeduplicatedQueue
    assert package.task_event_id is queueing.task_event_id
    assert package.generate_audio_hash is queueing.generate_audio_hash
