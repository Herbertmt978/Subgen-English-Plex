subgen_version = '2026.07.1'

"""
ENVIRONMENT VARIABLES DOCUMENTATION

This application supports both new standardized environment variable names and legacy names for backwards compatibility. The new names follow a consistent naming convention:

STANDARDIZED NAMING CONVENTION:
- Use UPPERCASE with underscores for separation
- Group related variables with consistent prefixes:
  * PLEX_* for Plex server integration
  * JELLYFIN_* for Jellyfin server integration
  * PROCESS_* for media processing triggers
  * SKIP_* for all skip conditions
  * SUBTITLE_* for subtitle-related settings
  * WHISPER_* for Whisper model settings
  * TRANSCRIBE_* for transcription settings

BACKWARDS COMPATIBILITY:
Legacy environment variable names are still supported. If both new and old names are set,
the new standardized name takes precedence.

NEW NAME → OLD NAME (for backwards compatibility):
- PLEX_TOKEN → PLEXTOKEN
- PLEX_SERVER → PLEXSERVER
- JELLYFIN_TOKEN → JELLYFINTOKEN
- JELLYFIN_SERVER → JELLYFINSERVER
- PROCESS_ADDED_MEDIA → PROCADDEDMEDIA
- PROCESS_MEDIA_ON_PLAY → PROCMEDIAONPLAY
- SUBTITLE_LANGUAGE_NAME → NAMESUBLANG
- WEBHOOK_PORT → WEBHOOKPORT
- SKIP_IF_EXTERNAL_SUBTITLES_EXIST → SKIPIFEXTERNALSUB
- SKIP_IF_TARGET_SUBTITLES_EXIST → SKIP_IF_TO_TRANSCRIBE_SUB_ALREADY_EXIST
- SKIP_IF_INTERNAL_SUBTITLES_LANGUAGE → SKIPIFINTERNALSUBLANG
- SKIP_SUBTITLE_LANGUAGES → SKIP_LANG_CODES
- SKIP_IF_AUDIO_LANGUAGES → SKIP_IF_AUDIO_TRACK_IS
- SKIP_ONLY_SUBGEN_SUBTITLES → ONLY_SKIP_IF_SUBGEN_SUBTITLE
- SKIP_IF_NO_LANGUAGE_BUT_SUBTITLES_EXIST → SKIP_IF_LANGUAGE_IS_NOT_SET_BUT_SUBTITLES_EXIST

MIGRATION GUIDE:
Users can gradually migrate to the new names. Both will work simultaneously during the
transition period. The old names may be deprecated in future versions.
"""

import ast
import asyncio
import csv
import ctypes
import ctypes.util
import gc
import hashlib
import importlib
import json
import logging
import math
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from threading import Lock, Timer
from typing import List, Union

import av
import faster_whisper
import ffmpeg
import numpy as np
import requests
import stable_whisper
import torch
from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response, StreamingResponse
from stable_whisper import Segment
from watchdog.observers.polling import PollingObserver as Observer

from language_code import LanguageCode
from subgen_core import media as _media
from subgen_core import model_envelope_catalog as _model_envelope_catalog
from subgen_core import model_runtime as _model_runtime
from subgen_core import priority_pressure as _priority_pressure
from subgen_core import resource_management as _resource_management
from subgen_core import resource_probes as _resource_probes
from subgen_core import scanner as _scanner
from subgen_core import transcription as _transcription
from subgen_core.integrations import jellyfin as _jellyfin_client
from subgen_core.integrations import plex as _plex_client
from subgen_core.queueing import (
    DeduplicatedQueue,
    TaskResult,
    generate_audio_hash,
    task_event_id,
)
from subgen_failure_markers import (
    DEFAULT_MARKER_REGISTRY_PATH,
    FailureMarkerReader,
    normalize_file_identity,
)


def _runtime():
    return sys.modules[__name__]


def convert_to_bool(in_bool):
    # Convert the input to string and lower case, then check against true values
    return str(in_bool).lower() in ('true', 'on', '1', 'y', 'yes')


def _strict_environment_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _auto_or_positive_gib(name: str) -> float | None:
    raw = os.getenv(name, "auto").strip()
    if raw.casefold() == "auto":
        return None
    if not raw:
        raise ValueError(f"{name} must be 'auto' or a positive number")
    try:
        value = float(raw)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be 'auto' or a positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be 'auto' or a positive number")
    if int(value * _resource_management.GIB) <= 0:
        raise ValueError(f"{name} must represent at least one byte")
    return value


def _chunk_minutes_setting(raw: str) -> int | None:
    normalized = raw.strip().casefold()
    if normalized == "auto":
        return None
    if not normalized:
        raise ValueError(
            "SEGMENTATION_CHUNK_MINUTES must be 'auto' or an integer from 5 to 60"
        )
    try:
        value = int(normalized)
    except ValueError as exc:
        raise ValueError(
            "SEGMENTATION_CHUNK_MINUTES must be 'auto' or an integer from 5 to 60"
        ) from exc
    if not 5 <= value <= 60:
        raise ValueError(
            "SEGMENTATION_CHUNK_MINUTES must be 'auto' or an integer from 5 to 60"
        )
    return value


def _normalized_model_revision(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return _model_envelope_catalog.normalize_model_revision(value)
    except _model_envelope_catalog.ArtifactValidationError as exc:
        raise ValueError(
            "WHISPER_MODEL_REVISION must be an immutable 40-character lowercase "
            "Hugging Face commit"
        ) from exc


def _cuda_index(device: str) -> int:
    normalized = device.strip().casefold()
    if normalized == "gpu":
        normalized = "cuda"
    if normalized == "cuda":
        return 0
    if normalized.startswith("cuda:"):
        suffix = normalized.split(":", 1)[1]
        if suffix.isascii() and suffix.isdecimal():
            return int(suffix)
    raise ValueError("TRANSCRIBE_DEVICE must use 'cuda' or 'cuda:<index>' for CUDA")


def _decoder_options_digest(options: dict) -> str | None:
    try:
        return _model_envelope_catalog.decoder_options_sha256(options)
    except _model_envelope_catalog.ArtifactValidationError:
        return None

def get_env_with_fallback(new_name: str, old_name: str, default_value=None, convert_func=None):
    """
    Get environment variable with backwards compatibility fallback.

    Args:
        new_name: The new standardized environment variable name
        old_name: The legacy environment variable name for backwards compatibility
        default_value: Default value if neither variable is set
        convert_func: Optional function to convert the value (e.g., convert_to_bool, int)

    Returns:
        The environment variable value, converted if convert_func is provided
    """
    blank_numeric_is_unset = convert_func is int
    if new_name in os.environ and not (
        blank_numeric_is_unset and os.environ[new_name] == ''
    ):
        value = os.environ[new_name]
    elif old_name in os.environ and not (
        blank_numeric_is_unset and os.environ[old_name] == ''
    ):
        value = os.environ[old_name]
    else:
        value = default_value

    # Apply conversion function if provided
    if convert_func and value is not None:
        return convert_func(value)

    return value

# Server Integration - with backwards compatibility
plextoken = get_env_with_fallback('PLEX_TOKEN', 'PLEXTOKEN', '')
plexserver = get_env_with_fallback('PLEX_SERVER', 'PLEXSERVER', '')
jellyfintoken = get_env_with_fallback('JELLYFIN_TOKEN', 'JELLYFINTOKEN', '')
jellyfinserver = get_env_with_fallback('JELLYFIN_SERVER', 'JELLYFINSERVER', '')

# Whisper Configuration
requested_whisper_model = os.getenv('WHISPER_MODEL', 'auto').strip() or 'auto'
if requested_whisper_model.casefold() == 'auto':
    requested_whisper_model = 'auto'
whisper_model = requested_whisper_model
whisper_model_revision = _normalized_model_revision(
    os.getenv('WHISPER_MODEL_REVISION', '')
)
if requested_whisper_model == 'auto' and whisper_model_revision is not None:
    raise ValueError(
        "WHISPER_MODEL_REVISION is only valid with an explicit WHISPER_MODEL"
    )
whisper_model_revision_commit = (
    whisper_model_revision.removeprefix('hf:') if whisper_model_revision else None
)
whisper_threads = int(os.getenv('WHISPER_THREADS', 4))
concurrent_transcriptions = int(os.getenv('CONCURRENT_TRANSCRIPTIONS', 2))
transcribe_device = os.getenv('TRANSCRIBE_DEVICE', 'cpu')
model_envelope_catalog_path = os.getenv(
    'MODEL_ENVELOPE_CATALOG',
    '/opt/subgen/model-envelopes/catalog.json',
).strip() or '/opt/subgen/model-envelopes/catalog.json'
model_envelope_identity_path = os.getenv(
    'MODEL_ENVELOPE_IDENTITY',
    '/opt/subgen/model-envelopes/image-identity.json',
).strip() or '/opt/subgen/model-envelopes/image-identity.json'
segmentation_enabled = _strict_environment_bool('SEGMENTATION_ENABLED', True)
segmentation_chunk_minutes = _chunk_minutes_setting(
    os.getenv('SEGMENTATION_CHUNK_MINUTES', 'auto')
)
memory_pressure_yield = _strict_environment_bool('MEMORY_PRESSURE_YIELD', True)
priority_pressure_file = os.getenv('PRIORITY_PRESSURE_FILE', '').strip()
if priority_pressure_file and not memory_pressure_yield:
    raise ValueError(
        "PRIORITY_PRESSURE_FILE requires MEMORY_PRESSURE_YIELD=True"
    )
priority_pressure_probe = _priority_pressure.PriorityPressureReader(
    priority_pressure_file or None
)
priority_pressure_reader = priority_pressure_probe.read
memory_pressure_reserve_gib = _auto_or_positive_gib(
    'MEMORY_PRESSURE_RESERVE_GIB'
)
gpu_memory_reserve_gib = _auto_or_positive_gib('GPU_MEMORY_RESERVE_GIB')
canonical_shared_cuda = _strict_environment_bool('CANONICAL_SHARED_CUDA', False)

# Processing Control - with backwards compatibility
procaddedmedia = get_env_with_fallback('PROCESS_ADDED_MEDIA', 'PROCADDEDMEDIA', True, convert_to_bool)
procmediaonplay = get_env_with_fallback('PROCESS_MEDIA_ON_PLAY', 'PROCMEDIAONPLAY', True, convert_to_bool)

# Subtitle Configuration - with backwards compatibility
subtitle_language_name = get_env_with_fallback('SUBTITLE_LANGUAGE_NAME', 'NAMESUBLANG', '')

# System Configuration - with backwards compatibility
webhookport = get_env_with_fallback('WEBHOOK_PORT', 'WEBHOOKPORT', 9000, int)
word_level_highlight = convert_to_bool(os.getenv('WORD_LEVEL_HIGHLIGHT', False))
debug = convert_to_bool(os.getenv('DEBUG', True))
use_path_mapping = convert_to_bool(os.getenv('USE_PATH_MAPPING', False))
path_mapping_from = os.getenv('PATH_MAPPING_FROM', r'/tv')
path_mapping_to = os.getenv('PATH_MAPPING_TO', r'/Volumes/TV')
model_location = os.getenv('MODEL_PATH', './models')
monitor = convert_to_bool(os.getenv('MONITOR', False))
skip_startup_scan = convert_to_bool(os.getenv('SKIP_STARTUP_SCAN', False))
skip_marked_failed_files = convert_to_bool(os.getenv('SKIP_MARKED_FAILED_FILES', True))
failure_marker_registry_path = (
    os.getenv('SUBGEN_FAILURE_MARKER_PATH', DEFAULT_MARKER_REGISTRY_PATH).strip()
    or DEFAULT_MARKER_REGISTRY_PATH
)
failure_marker_reader = FailureMarkerReader(failure_marker_registry_path)
transcribe_folders = os.getenv('TRANSCRIBE_FOLDERS', '')
transcribe_or_translate = os.getenv('TRANSCRIBE_OR_TRANSLATE', 'transcribe').lower()
clear_vram_on_complete = convert_to_bool(os.getenv('CLEAR_VRAM_ON_COMPLETE', True))
compute_type = os.getenv('COMPUTE_TYPE', 'auto')
append = convert_to_bool(os.getenv('APPEND', False))
reload_script_on_change = convert_to_bool(os.getenv('RELOAD_SCRIPT_ON_CHANGE', False))
lrc_for_audio_files = convert_to_bool(os.getenv('LRC_FOR_AUDIO_FILES', True))
custom_regroup = os.getenv('CUSTOM_REGROUP', 'cm_sl=84_sl=42++++++1')
detect_language_length = int(os.getenv('DETECT_LANGUAGE_LENGTH', 30))
detect_language_offset = int(os.getenv('DETECT_LANGUAGE_OFFSET', 0))
model_cleanup_delay = int(os.getenv('MODEL_CLEANUP_DELAY', 30))
asr_timeout = int(os.getenv('ASR_TIMEOUT', 18000))
webhook_url_completed = os.getenv('WEBHOOK_URL_COMPLETED', '')
http_timeout = float(os.getenv('HTTP_TIMEOUT_SECONDS', 30))
subgen_api_key = os.getenv('SUBGEN_API_KEY', '').strip()
notify_on_english_audio_mismatch = convert_to_bool(os.getenv('NOTIFY_ON_ENGLISH_AUDIO_MISMATCH', True))
skip_video_extensions = {
    ext if ext.startswith('.') else f".{ext}"
    for ext in (
        item.strip().lower()
        for item in os.getenv('SKIP_VIDEO_EXTENSIONS', '').split('|')
        if item.strip()
    )
}

# Skip Configuration - with backwards compatibility
skip_if_external_sub_exists = get_env_with_fallback('SKIP_IF_EXTERNAL_SUBTITLES_EXIST', 'SKIPIFEXTERNALSUB', False, convert_to_bool)
skip_if_target_subtitle_exists = get_env_with_fallback('SKIP_IF_TARGET_SUBTITLES_EXIST', 'SKIP_IF_TO_TRANSCRIBE_SUB_ALREADY_EXIST', True, convert_to_bool)
skip_if_internal_sub_language = LanguageCode.from_string(get_env_with_fallback('SKIP_IF_INTERNAL_SUBTITLES_LANGUAGE', 'SKIPIFINTERNALSUBLANG', ''))
ignore_forced_subtitles = convert_to_bool(os.getenv('IGNORE_FORCED_SUBTITLES', True))
plex_queue_next_episode = convert_to_bool(os.getenv('PLEX_QUEUE_NEXT_EPISODE', False))
plex_queue_season = convert_to_bool(os.getenv('PLEX_QUEUE_SEASON', False))
plex_queue_series = convert_to_bool(os.getenv('PLEX_QUEUE_SERIES', False))
# Language and Skip Configuration - with backwards compatibility
skip_subtitle_languages = ([LanguageCode.from_string(code) for code in get_env_with_fallback('SKIP_SUBTITLE_LANGUAGES', 'SKIP_LANG_CODES', '').split("|")]
        if get_env_with_fallback('SKIP_SUBTITLE_LANGUAGES', 'SKIP_LANG_CODES')
    else[]
)
force_detected_language_to = LanguageCode.from_string(os.getenv('FORCE_DETECTED_LANGUAGE_TO', ''))
preferred_audio_languages =[
    LanguageCode.from_string(code)
    for code in os.getenv('PREFERRED_AUDIO_LANGUAGES', 'eng').split("|")
] # in order of preference
limit_to_preferred_audio_languages = convert_to_bool(os.getenv('LIMIT_TO_PREFERRED_AUDIO_LANGUAGE', False))
skip_audio_languages = ([LanguageCode.from_string(code) for code in get_env_with_fallback('SKIP_IF_AUDIO_LANGUAGES', 'SKIP_IF_AUDIO_TRACK_IS', '').split("|")]
    if get_env_with_fallback('SKIP_IF_AUDIO_LANGUAGES', 'SKIP_IF_AUDIO_TRACK_IS')
    else[]
)

# Additional Subtitle Configuration - with backwards compatibility
subtitle_language_naming_type = os.getenv('SUBTITLE_LANGUAGE_NAMING_TYPE', 'ISO_639_2_B')
only_match_subgen_subtitles = get_env_with_fallback('SKIP_ONLY_SUBGEN_SUBTITLES', 'ONLY_SKIP_IF_SUBGEN_SUBTITLE', False, convert_to_bool)
skip_unknown_language = convert_to_bool(os.getenv('SKIP_UNKNOWN_LANGUAGE', False))
skip_if_no_audio_language_but_subtitles_exist = get_env_with_fallback('SKIP_IF_NO_LANGUAGE_BUT_SUBTITLES_EXIST', 'SKIP_IF_LANGUAGE_IS_NOT_SET_BUT_SUBTITLES_EXIST', False, convert_to_bool)
ignore_forced_subtitles = convert_to_bool(os.getenv('IGNORE_FORCED_SUBTITLES', True))
should_whisper_detect_audio_language = convert_to_bool(os.getenv('SHOULD_WHISPER_DETECT_AUDIO_LANGUAGE', False))
show_in_subname_subgen = convert_to_bool(os.getenv('SHOW_IN_SUBNAME_SUBGEN', True))
show_in_subname_model = convert_to_bool(os.getenv('SHOW_IN_SUBNAME_MODEL', True))

# Advanced Configuration
try:
    kwargs = ast.literal_eval(os.getenv('SUBGEN_KWARGS', '{}') or '{}')
    if not isinstance(kwargs, dict):
        raise ValueError("SUBGEN_KWARGS must evaluate to a dictionary")
except (SyntaxError, ValueError):
    kwargs = {}
    logging.info("kwargs (SUBGEN_KWARGS) is an invalid dictionary, defaulting to empty '{}'")

transcribe_device = transcribe_device.strip().casefold()
if transcribe_device == "gpu":
    transcribe_device = "cuda"
if whisper_threads <= 0:
    raise ValueError("WHISPER_THREADS must be a positive integer")
if concurrent_transcriptions <= 0:
    raise ValueError("CONCURRENT_TRANSCRIPTIONS must be a positive integer")

cuda_device_index = None
if transcribe_device.startswith("cuda"):
    cuda_device_index = _cuda_index(transcribe_device)
if canonical_shared_cuda:
    if cuda_device_index is None:
        raise ValueError("CANONICAL_SHARED_CUDA requires TRANSCRIBE_DEVICE=cuda")
    if gpu_memory_reserve_gib is None:
        raise ValueError(
            "CANONICAL_SHARED_CUDA requires a positive GPU_MEMORY_RESERVE_GIB"
        )
    if not priority_pressure_probe.configured:
        raise ValueError(
            "CANONICAL_SHARED_CUDA requires PRIORITY_PRESSURE_FILE"
        )

memory_pressure_reserve_bytes = (
    int(memory_pressure_reserve_gib * _resource_management.GIB)
    if memory_pressure_reserve_gib is not None
    else None
)
decoder_options_sha256 = _decoder_options_digest(kwargs)


def _nvidia_snapshot() -> dict[str, object]:
    if cuda_device_index is None:
        raise RuntimeError("CUDA telemetry requested for a non-CUDA runtime")
    completed = subprocess.run(
        [
            "nvidia-smi",
            f"--id={cuda_device_index}",
            "--query-gpu=uuid,driver_version",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if len(completed.stdout) > 4096:
        raise RuntimeError("exact-device NVIDIA telemetry is oversized")
    rows = [row for row in csv.reader(completed.stdout.splitlines()) if row]
    if completed.returncode != 0 or len(rows) != 1 or len(rows[0]) != 2:
        raise RuntimeError("exact-device NVIDIA telemetry is unavailable")
    device_id, driver_version = (value.strip() for value in rows[0])
    if not device_id or not driver_version or not device_id.isascii():
        raise RuntimeError("exact-device NVIDIA identity is unavailable")
    return {
        "device_id": device_id,
        "driver_version": driver_version,
    }


def _read_exact_gpu_memory() -> tuple[str, int, int]:
    snapshot = _nvidia_snapshot()
    free_bytes, total_bytes = torch.cuda.mem_get_info(cuda_device_index)
    properties = torch.cuda.get_device_properties(cuda_device_index)
    total_bytes = int(total_bytes)
    free_bytes = int(free_bytes)
    if (
        total_bytes <= 0
        or int(properties.total_memory) != total_bytes
        or not 0 <= free_bytes <= total_bytes
    ):
        raise RuntimeError("exact-device CUDA memory telemetry is inconsistent")
    return snapshot["device_id"], total_bytes, free_bytes


def read_resource_pressure_sample():
    """Read generic host/cgroup/GPU pressure without consuming priority input."""

    gpu_reader = _read_exact_gpu_memory if cuda_device_index is not None else None
    return _resource_probes.read_pressure_sample(gpu_memory_reader=gpu_reader)


def read_pressure_sample():
    gpu_reader = _read_exact_gpu_memory if cuda_device_index is not None else None
    return _resource_probes.read_pressure_sample(
        gpu_memory_reader=gpu_reader,
        priority_reader=priority_pressure_reader,
    )


def build_runtime_identity(total_vram_bytes: int, expected_device_id: str):
    if cuda_device_index is None:
        raise RuntimeError("exact model envelopes require CUDA runtime identity")
    device_id, exact_total_bytes, _free_bytes = _read_exact_gpu_memory()
    snapshot = _nvidia_snapshot()
    if (
        not expected_device_id
        or device_id != expected_device_id
        or snapshot["device_id"] != expected_device_id
    ):
        raise RuntimeError("CUDA device identity changed during runtime matching")
    if exact_total_bytes != total_vram_bytes:
        raise RuntimeError("NVIDIA total VRAM changed during runtime matching")
    properties = torch.cuda.get_device_properties(cuda_device_index)
    capability = torch.cuda.get_device_capability(cuda_device_index)
    cuda_version = getattr(torch.version, "cuda", None)
    ctranslate2 = importlib.import_module("ctranslate2")
    versions = (
        stable_whisper.__version__,
        faster_whisper.__version__,
        ctranslate2.__version__,
        cuda_version,
        snapshot["driver_version"],
        properties.name,
    )
    if any(
        type(value) is not str or not value or not value.isascii()
        for value in versions
    ):
        raise RuntimeError("exact CUDA runtime identity is unavailable")
    return _model_envelope_catalog.RuntimeIdentity(
        stable_ts_version=stable_whisper.__version__,
        faster_whisper_version=faster_whisper.__version__,
        ctranslate2_version=ctranslate2.__version__,
        cuda_runtime_version=cuda_version,
        driver_version=snapshot["driver_version"],
        device_name=properties.name,
        compute_capability=f"{capability[0]}.{capability[1]}",
        total_vram_bytes=total_vram_bytes,
    )

VIDEO_EXTENSIONS = _media.VIDEO_EXTENSIONS
AUDIO_EXTENSIONS = _media.AUDIO_EXTENSIONS

@asynccontextmanager
async def lifespan(app: FastAPI):
    global model_idle_observer_thread

    model_runtime_cancel_event.clear()
    if memory_pressure_yield:
        model_idle_observer_stop.clear()
        model_idle_observer_thread = threading.Thread(
            target=run_model_idle_observer,
            daemon=True,
            name="subgen-model-idle-observer",
        )
        model_idle_observer_thread.start()
    if transcribe_folders:
        threading.Thread(
            target=transcribe_existing,
            args=(transcribe_folders,),
            daemon=True,
        ).start()
    try:
        yield
    finally:
        model_runtime_cancel_event.set()
        model_idle_observer_stop.set()
        if model_idle_observer_thread is not None:
            model_idle_observer_thread.join(timeout=6)
            model_idle_observer_thread = None

app = FastAPI(lifespan=lifespan)


def require_api_key(x_subgen_api_key: str | None = Header(default=None)) -> None:
    """Protect expensive manual/API endpoints when SUBGEN_API_KEY is configured."""
    if subgen_api_key and (
        x_subgen_api_key is None
        or not secrets.compare_digest(x_subgen_api_key, subgen_api_key)
    ):
        raise HTTPException(status_code=401, detail="A valid X-Subgen-Api-Key header is required")

model = None
model_cleanup_timer = None
model_cleanup_lock = Lock()

# Locks to ensure thread-safety during concurrent AI operations
model_load_lock = Lock()
model_selection_lock = Lock()
active_direct_tasks = 0
active_direct_tasks_lock = Lock()
model_inference_permit_count = max(1, concurrent_transcriptions)
model_inference_semaphore = threading.BoundedSemaphore(
    model_inference_permit_count
)
model_runtime_condition = threading.Condition(Lock())
model_admission_closed = True
model_release_generation = 0
model_release_transition = None
model_active_inferences = 0
model_load_generation = 0
model_unload_generation = 0
cuda_oom_generation = 0
media_failure_generation = 0
model_runtime_initialized = False
model_decision = None
model_requirement = None
model_pressure_controller = None
model_capacity_profile = None
model_chunk_baseline_seconds = None
model_stabilized_gpu = None
model_envelope_expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
model_runtime_clock = time.monotonic
model_runtime_sleep = time.sleep
model_runtime_cancel_event = threading.Event()
model_permit_wait_seconds = 1.0
model_load_allocation_failures = 0
model_profile_unhealthy = False
model_profile_unhealthy_reason = None
model_idle_observer_stop = threading.Event()
model_idle_observer_thread = None
model_runtime_status = {
    "controller_state": "uninitialized",
    "recovery_reason": None,
    "admission_open": False,
    "capacity_source": None,
    "requested_model": requested_whisper_model,
    "envelope_key": None,
    "envelope_disposition": None,
    "envelope_reason": None,
    "selected_model": None,
    "model_explicit": requested_whisper_model != "auto",
    "automatic_ceiling": None,
    "decision_reason": None,
    "decision_provenance": None,
    "gpu_total_bytes": None,
    "gpu_stabilized_free_bytes": None,
    "gpu_reserve_bytes": None,
    "gpu_allocatable_bytes": None,
}

in_docker = os.path.exists('/.dockerenv')
docker_status = "Docker" if in_docker else "Standalone"

# Dictionary to store task results keyed by task_id
# Entries are cleaned up in /asr endpoint finally block to prevent unbounded growth
task_results = {}
task_results_lock = Lock()


def emit_subgen_event(
    event: str,
    task: dict,
    error: Exception | str | None = None,
    *,
    failure_class: str | None = None,
    source_identity=None,
    validator_outcomes: dict | None = None,
    validation_detail: str | None = None,
) -> None:
    """Emit a machine-readable lifecycle event without replacing human logs."""
    event_path = task.get("video_file") or task.get("path", "unknown")
    payload = {
        "event": event,
        "task_id": task_event_id(task),
        "task_type": task.get("type", "transcribe"),
        "path": str(event_path),
    }
    if error is not None:
        payload["error"] = str(error)[:500]
    if failure_class is not None:
        allowed_failure_classes = {
            "invalid_media",
            "probe_indeterminate",
            "inference_error",
            "resource_exhaustion",
            "sigsegv",
            "resource_pressure_yield",
        }
        if (
            not isinstance(failure_class, str)
            or failure_class not in allowed_failure_classes
        ):
            raise ValueError("Unsupported Subgen failure class")
        payload["failure_class"] = failure_class
    if source_identity is not None:
        payload["source_identity"] = list(normalize_file_identity(source_identity))
    if validator_outcomes is not None:
        allowed_validator_outcomes = {
            "audio_present",
            "no_audio",
            "invalid_format",
            "indeterminate",
        }
        if (
            event != "media_validation_failed"
            or not isinstance(validator_outcomes, dict)
            or set(validator_outcomes) != {"ffprobe", "pyav"}
            or any(
                not isinstance(outcome, str)
                or outcome not in allowed_validator_outcomes
                for outcome in validator_outcomes.values()
            )
        ):
            raise ValueError("Invalid validator-outcome evidence")
        payload["validator_outcomes"] = dict(validator_outcomes)
    if validation_detail is not None:
        if (
            event != "media_validation_failed"
            or not isinstance(validation_detail, str)
            or not validation_detail
            or len(validation_detail) > 64
            or any(
                not (character.isascii() and (character.isalnum() or character == "_"))
                for character in validation_detail
            )
        ):
            raise ValueError("Invalid media-validation detail code")
        payload["validation_detail"] = validation_detail
    if event == "media_validation_failed" and (
        failure_class not in {"invalid_media", "probe_indeterminate"}
        or validator_outcomes is None
        or validation_detail is None
    ):
        raise ValueError("Media validation events require complete typed evidence")
    if event == "media_validation_failed":
        _model_runtime.record_media_failure(_runtime())
    logging.info("SUBGEN_EVENT %s", json.dumps(payload, separators=(",", ":")))


def emit_model_runtime_error(task: dict, error: Exception) -> None:
    """Clear worker state without attributing a profile failure to media."""
    if isinstance(error, _model_runtime.ModelLoadProfileUnhealthy):
        error_code = "model_load_profile_unhealthy"
    elif isinstance(error, _model_runtime.ModelReleaseError):
        error_code = "model_release_failed"
    elif isinstance(error, _resource_management.MemoryPressureYield):
        error_code = "memory_pressure_yield"
    else:
        error_code = "model_runtime_cancelled"
    payload = {
        "event": "runtime_error",
        "task_id": task_event_id(task),
        "task_type": task.get("type", "transcribe"),
        "scope": "model_runtime",
        "error_code": error_code,
    }
    logging.info("SUBGEN_EVENT %s", json.dumps(payload, separators=(",", ":")))


def transcribe_with_model(*args, **transcribe_kwargs):
    return _model_runtime.transcribe_with_model(_runtime(), *args, **transcribe_kwargs)


def cleanup_task_result(task_id: str, *, require_inactive: bool = False) -> None:
    if not task_id:
        return
    if require_inactive and task_queue.is_active(task_id):
        return
    with task_results_lock:
        result = task_results.get(task_id)
        if result is not None and result.done.is_set():
            task_results.pop(task_id, None)

# Start queue
task_queue = DeduplicatedQueue()

# ============================================================================
# TRANSCRIPTION WORKER
# ============================================================================

def transcription_worker():
    """Main worker thread with centralized logging and status tracking."""
    while True:
        task = None
        next_task = None
        try:
            task = task_queue.get(block=True, timeout=1)
            task_type = task.get("type", "transcribe")
            path = task.get("path", "unknown")
            display_name = os.path.basename(path) if ("/" in str(path) or "\\" in str(path)) else path

            # Status for START log
            proc_count = len(task_queue.get_processing_tasks())
            queue_count = len(task_queue.get_queued_tasks())
            task_validation = task.get("media_validation")
            task_source_identity = getattr(
                task_validation,
                "source_identity",
                None,
            )
            emit_subgen_event(
                "worker_start",
                task,
                source_identity=task_source_identity,
            )
            logging.info(f"WORKER START : [{task_type.upper():<10}] {display_name:^40} | Jobs: {proc_count} processing, {queue_count} queued")

            start_time = time.time()
            if task_type == "detect_language":
                if "audio_content" in task:
                    detect_language_from_upload(task)
                else:
                    # Capture the transcription task to queue later
                    next_task = detect_language_task(task['path'], original_task_data=task)
            elif task_type == "asr":
                asr_task_worker(task)
            else: # transcribe
                gen_subtitles(
                    task['path'],
                    task['transcribe_or_translate'],
                    task['force_language'],
                    audio_tracks=task.get('audio_tracks'),
                    audio_track_index=task.get('audio_track_index'),
                    media_validation=task.get('media_validation'),
                )

                # --- METADATA REFRESH LOGIC ---
                if 'plex_item_id' in task:
                    try:
                        logging.info(f"Refreshing Plex Metadata for item {task['plex_item_id']}")
                        refresh_plex_metadata(task['plex_item_id'], task['plex_server'], task['plex_token'])
                    except Exception as e:
                        logging.error(f"Failed to refresh Plex metadata: {e}")

                if 'jellyfin_item_id' in task:
                    try:
                        logging.info(f"Refreshing Jellyfin Metadata for item {task['jellyfin_item_id']}")
                        refresh_jellyfin_metadata(task['jellyfin_item_id'], task['jellyfin_server'], task['jellyfin_token'])
                    except Exception as e:
                        logging.error(f"Failed to refresh Jellyfin metadata: {e}")
                # ------------------------------

            # Status for FINISH log
            elapsed = time.time() - start_time
            m, s = divmod(int(elapsed), 60)
            remaining_queued = len(task_queue.get_queued_tasks())
            emit_subgen_event(
                "worker_finish",
                task,
                source_identity=task_source_identity,
            )
            logging.info(f"WORKER FINISH: [{task_type.upper():<10}] {display_name:^40} in {m}m {s}s | Remaining: {remaining_queued} queued")

        except queue.Empty:
            continue
        except Exception as e:
            model_runtime_errors = (
                _model_runtime.ModelLoadProfileUnhealthy,
                _model_runtime.ModelReleaseError,
                _model_runtime.ModelRuntimeCancelled,
                _resource_management.MemoryPressureYield,
            )
            media_validation_stale = isinstance(e, _media.MediaValidationStale)
            if task:
                if media_validation_stale:
                    validation = task.get("media_validation")
                    emit_subgen_event(
                        "media_validation_stale",
                        task,
                        source_identity=getattr(validation, "source_identity", None),
                    )
                elif isinstance(e, model_runtime_errors):
                    emit_model_runtime_error(task, e)
                else:
                    validation = task.get("media_validation")
                    emit_subgen_event(
                        "worker_error",
                        task,
                        e,
                        source_identity=getattr(
                            validation,
                            "source_identity",
                            None,
                        ),
                    )
            if media_validation_stale:
                logging.warning(
                    "Media generation changed after queue admission: %s",
                    task.get("path", "unknown") if task else "unknown",
                )
            elif isinstance(e, model_runtime_errors):
                logging.error("Model runtime unavailable: %s", e)
            else:
                logging.error(f"Error processing task: {e}", exc_info=True)
        finally:
            if task:
                task_queue.task_done()
                task_queue.mark_done(task)

                # Now that the detect task is removed from processing, it's safe to queue the transcription
                if next_task:
                    if task_queue.put(next_task):
                        logging.debug(f"Queued transcription for detected language: {next_task['path']}")
                    else:
                        logging.debug(f"Transcription already queued/processing for: {next_task['path']}")

                cleanup_task_result(str(task.get("path", "")))

                delete_model()

# Create worker threads
for _ in range(concurrent_transcriptions):
    threading.Thread(target=transcription_worker, daemon=True).start()

# Define a filter class to hide common logging we don't want to see
class MultiplePatternsFilter(logging.Filter):
    def filter(self, record):
        # Define the patterns to search for
        patterns =[
            "Compression ratio threshold is not met",
            "Processing segment at",
            "Log probability threshold is",
            "Reset prompt",
            "Attempting to release",
            "released on ",
            "Attempting to acquire",
            "acquired on",
            "header parsing failed",
            "timescale not set",
            "misdetection possible",
            "srt was added",
            "doesn't have any audio to transcribe",
            "Calling on_"
        ]
        # Return False if any of the patterns are found, True otherwise
        return not any(pattern in record.getMessage() for pattern in patterns)

# Configure logging
if debug:
    level = logging.DEBUG
else:
    level = logging.INFO

logging.basicConfig(
    stream=sys.stderr,
    level=level,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S" # This removes the ,123 part
)

# Get the root logger
logger = logging.getLogger()
logger.setLevel(level) # Set the logger level

for handler in logger.handlers:
    handler.addFilter(MultiplePatternsFilter())

logging.getLogger("multipart").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)


class ProgressHandler:
    def __init__(self, filename):
        self.filename = filename
        self.start_time = time.time()
        self.last_print_time = 0
        self.interval = 5

    @staticmethod
    def _fmt_t(seconds):
        """Format seconds as [H:]MM:SS without milliseconds."""
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def __call__(self, seek, total):
        if docker_status == 'Docker' or debug:
            current_time = time.time()
            if self.last_print_time == 0 or (current_time - self.last_print_time) >= self.interval:
                self.last_print_time = current_time

                pct = int((seek / total) * 100) if total > 0 else 0
                elapsed = current_time - self.start_time
                speed = seek / elapsed if elapsed > 0 else 0
                eta = (total - seek) / speed if speed > 0 else 0

                proc = len(task_queue.get_processing_tasks())
                queued = len(task_queue.get_queued_tasks())

                clean_name = (self.filename[:37] + '..') if len(self.filename) > 40 else self.filename

                logging.info(
                    f"[ {clean_name:<40}] {pct:>3}% | "
                    f"{int(seek):>5}/{int(total):<5}s "
                    f"[{self._fmt_t(elapsed):>5}<{self._fmt_t(eta):>5}, {speed:>5.2f}s/s] | "
                    f"Jobs: {proc} processing, {queued} queued"
                )

TIME_OFFSET = 5

def appendLine(result):
    if append and result.segments:
        lastSegment = result.segments[-1]
        date_time_str = datetime.now().strftime("%d %b %Y - %H:%M:%S")
        appended_text = f"Transcribed by whisperAI with faster-whisper ({whisper_model}) on {date_time_str}"

        # Create a new segment with the updated information
        newSegment = Segment(
            start=lastSegment.start + TIME_OFFSET,
            end=lastSegment.end + TIME_OFFSET,
            text=appended_text,
            words=[], # Empty list for words
            id=lastSegment.id + 1
        )

        # Append the new segment to the result's segments
        result.segments.append(newSegment)

@app.get("/plex")
@app.get("/webhook")
@app.get("/jellyfin")
@app.get("/asr")
@app.get("/emby")
@app.get("/detect-language")
@app.get("/tautulli")
def handle_get_request(request: Request):
    return {"You accessed this request incorrectly via a GET request. See https://github.com/McCloudS/subgen for proper configuration"}

@app.get("/")
def webui():
    return {"The webui for configuration was removed on 1 October 2024, please configure via environment variables or in your Docker settings. "}

@app.get("/status")
def status():
    return {
        "version": f"Subgen {subgen_version}, stable-ts {stable_whisper.__version__}, faster-whisper {faster_whisper.__version__} ({docker_status})",
        "resource_management": _model_runtime.runtime_status(_runtime()),
    }

@app.post("/tautulli")
def receive_tautulli_webhook(
        source: Union[str, None] = Header(None),
        event: str = Body(None),
        file: str = Body(None),
):
    if source == "Tautulli":
        logging.debug(f"Tautulli event detected is: {event}")
        if((event == "added" and procaddedmedia) or (event == "played" and procmediaonplay)):
            fullpath = file
            logging.debug(f"Full file path: {fullpath}")

            gen_subtitles_queue(path_mapping(fullpath), transcribe_or_translate)
    else:
        return {
            "message": "This doesn't appear to be a properly configured Tautulli webhook, please review the instructions again!"}

    return ""

@app.post("/plex")
def receive_plex_webhook(
        user_agent: Union[str] = Header(None),
        payload: Union[str] = Form(),
):
    try:
        plex_json = json.loads(payload)
        if "PlexMediaServer" not in user_agent:
            return {"message": "This doesn't appear to be a properly configured Plex webhook, please review the instructions again"}

        event = plex_json["event"]
        logging.debug(f"Plex event detected is: {event}")

        if (event == "library.new" and procaddedmedia) or (event == "media.play" and procmediaonplay):
            rating_key = plex_json['Metadata']['ratingKey']
            fullpath = get_plex_file_name(rating_key, plexserver, plextoken)
            logging.debug(f"Full file path: {fullpath}")

            # Queue the current item with its specific ID for refreshing
            gen_subtitles_queue(
                path_mapping(fullpath),
                transcribe_or_translate,
                plex_item_id=rating_key,
                plex_server=plexserver,
                plex_token=plextoken
            )

            # Note: refresh_plex_metadata is removed here; it is now handled by the worker thread.

            if plex_queue_next_episode:
                next_key = get_next_plex_episode(plex_json['Metadata']['ratingKey'], stay_in_season=False)
                if next_key:
                    next_file = get_plex_file_name(next_key, plexserver, plextoken)
                    gen_subtitles_queue(
                        path_mapping(next_file),
                        transcribe_or_translate,
                        plex_item_id=next_key, # Pass the NEXT ID so it refreshes when done
                        plex_server=plexserver,
                        plex_token=plextoken
                    )

            if plex_queue_series or plex_queue_season:
                current_rating_key = plex_json['Metadata']['ratingKey']
                stay_in_season = plex_queue_season # Determine if we're staying in the season or not

                while current_rating_key is not None:
                    try:
                        # Queue the current episode
                        file_path = path_mapping(get_plex_file_name(current_rating_key, plexserver, plextoken))

                        gen_subtitles_queue(
                            file_path,
                            transcribe_or_translate,
                            plex_item_id=current_rating_key, # Pass the specific loop ID for refreshing
                            plex_server=plexserver,
                            plex_token=plextoken
                        )

                        logging.debug(f"Queued episode with ratingKey {current_rating_key}")

                        # Get the next episode
                        next_episode_rating_key = get_next_plex_episode(current_rating_key, stay_in_season=stay_in_season)
                        if next_episode_rating_key is None:
                            break # Exit the loop if no next episode
                        current_rating_key = next_episode_rating_key

                    except Exception as e:
                        logging.error(f"Error processing episode with ratingKey {current_rating_key} or reached end of series: {e}")
                        break # Stop processing on error

                logging.info("All episodes in the series (or season) have been queued.")

    except Exception as e:
        logging.error(f"Failed to process Plex webhook: {e}")

    return ""

@app.post("/jellyfin")
def receive_jellyfin_webhook(
        user_agent: str = Header(None),
        NotificationType: str = Body(None),
        file: str = Body(None),
        ItemId: str = Body(None),
):
    if "Jellyfin-Server" in user_agent:
        logging.debug(f"Jellyfin event detected is: {NotificationType}")
        logging.debug(f"itemid is: {ItemId}")

        if (NotificationType == "ItemAdded" and procaddedmedia) or (NotificationType == "PlaybackStart" and procmediaonplay):
            fullpath = get_jellyfin_file_name(ItemId, jellyfinserver, jellyfintoken)
            logging.debug(f"Full file path: {fullpath}")

            # Queue item with Jellyfin metadata ID for delayed refresh
            gen_subtitles_queue(
                path_mapping(fullpath),
                transcribe_or_translate,
                jellyfin_item_id=ItemId,
                jellyfin_server=jellyfinserver,
                jellyfin_token=jellyfintoken
            )

            # Note: refresh_jellyfin_metadata removed here; handled by worker.
    else:
        return {
            "message": "This doesn't appear to be a properly configured Jellyfin webhook, please review the instructions again!"}

    return ""

@app.post("/emby")
def receive_emby_webhook(
        user_agent: Union[str, None] = Header(None),
        data: Union[str, None] = Form(None),
):
    if not data:
        return ""

    data_dict = json.loads(data)
    event = data_dict['Event']
    logging.debug("Emby event detected is: " + event)

    # Check if it's a notification test event
    if event == "system.notificationtest":
        logging.info("Emby test message received!")
        return {"message": "Notification test received successfully!"}

    if (event == "library.new" and procaddedmedia) or (event == "playback.start" and procmediaonplay):
        fullpath = data_dict['Item']['Path']
        logging.debug(f"Full file path: {fullpath}")
        gen_subtitles_queue(path_mapping(fullpath), transcribe_or_translate)

    return ""

@app.post("/batch")
def batch(
        directory: str = Query(...),
        forceLanguage: Union[str, None] = Query(default=None),
        _auth: None = Depends(require_api_key),
):
    queue_existing(directory, LanguageCode.from_string(forceLanguage))

# ============================================================================
# REFACTORED /ASR ENDPOINT WITH HASH-BASED DEDUPLICATION AND BLOCKING
# ============================================================================

@app.post("/asr")
async def asr(
    task: Union[str, None] = Query(default="transcribe", enum=["transcribe", "translate"]),
    language: Union[str, None] = Query(default=None),
    video_file: Union[str, None] = Query(default=None),
    initial_prompt: Union[str, None] = Query(default=None),
    audio_file: UploadFile = File(...),
    encode: bool = Query(default=True, description="Encode audio first through ffmpeg"),
    output: Union[str, None] = Query(default="srt", enum=["txt", "vtt", "srt", "tsv", "json"]),
    word_timestamps: bool = Query(default=False, description="Word-level timestamps"),
    _auth: None = Depends(require_api_key),
):
    """
    ASR endpoint that uses audio content hash for deduplication.
    BLOCKS until processing is complete, then returns the result.

    If identical audio + task + language is already being processed,
    waits for that task to complete and returns the same result.
    """
    task_id = None

    try:
        logging.info(
            f"ASR {task.capitalize()} received for file '{video_file}'"
            if video_file
            else f"ASR {task.capitalize()} received"
        )

        # Read audio file content into memory
        file_content = await audio_file.read()

        if not file_content:
            await audio_file.close()
            return {
                "status": "error",
                "message": "Audio file is empty"
            }

        # Generate deterministic hash from audio (and optionally task/language)
        audio_hash = generate_audio_hash(
            file_content,
            task,
            language,
            output,
            word_timestamps,
            initial_prompt,
            encode,
        )

        # Keep the mapped path in the identity without discarding the
        # option-aware audio hash. Same-file requests may legitimately differ
        # by task, language, format, timestamps, prompt, or encoding behavior.
        mapped_video_file = path_mapping(video_file) if video_file else None
        if mapped_video_file:
            video_file_hash = hashlib.sha256(
                mapped_video_file.encode('utf-8')
            ).hexdigest()[:16]
            task_id = f"asr-{video_file_hash}-{audio_hash}"
            logging.debug(f"Using video-aware task ID for ASR request: {task_id}")
        else:
            task_id = f"asr-{audio_hash}"
            logging.debug(f"Generated audio hash: {audio_hash} for ASR request")

        # Handle forced language
        final_language = language
        if force_detected_language_to:
            final_language = force_detected_language_to.to_iso_639_1()
            logging.info(f"Forcing detected language to {force_detected_language_to}")

        # Create result container for this task
        with task_results_lock:
            if task_id not in task_results:
                task_results[task_id] = TaskResult()
            task_result = task_results[task_id]

        # Queue the ASR task
        asr_task_data = {
            'path': task_id, # DeduplicatedQueue uses this for dedup
            'type': 'asr',
            'task': task,
            'language': final_language,
            'video_file': mapped_video_file,
            'initial_prompt': initial_prompt,
            'audio_content': file_content,
            'encode': encode,
            'output': output,
            'word_timestamps': word_timestamps,
            'result_container': task_result,
        }

        # Try to queue (returns False if already queued/processing)
        if task_queue.put(asr_task_data):
            logging.info(f"ASR task {task_id} queued")
        else:
            logging.info(f"ASR task {task_id} already queued/processing - waiting for result")

        # EVENT LOOP BLOCK FIX: Use asyncio.to_thread so FastAPI can still respond to /status
        if await asyncio.to_thread(task_result.wait, asr_timeout):
            if task_result.error:
                logging.error(f"ASR task {task_id} failed: {task_result.error}")
                return {
                    "status": "error",
                    "task_id": task_id,
                    "message": f"ASR processing failed: {task_result.error}"
                }
            else:
                logging.info(f"ASR task {task_id} completed")
                media_type = {
                    "json": "application/json",
                    "vtt": "text/vtt",
                    "txt": "text/plain",
                    "tsv": "text/tab-separated-values",
                    "srt": "text/plain",
                }.get(output, "text/plain")
                return Response(
                    content=task_result.result,
                    media_type=media_type,
                    headers={'Source': f'{task.capitalize()}d using stable-ts from Subgen!'}
                )
        else:
            logging.error(f"ASR task {task_id} timed out")
            return {
                "status": "timeout",
                "task_id": task_id,
                "message": f"ASR processing timed out after {asr_timeout} seconds"
            }

    except Exception as e:
        logging.error(f"Error in ASR endpoint: {e}", exc_info=True)
        return {"status": "error", "message": f"Error: {str(e)}"}
    finally:
        await audio_file.close()
        # Keep a timed-out active task's result container so retries attach to
        # the same worker. The worker removes completed entries after mark_done.
        cleanup_task_result(task_id, require_inactive=True)

_OPENAI_MEDIA_TYPES = {
    "json": "application/json",
    "verbose_json": "application/json",
    "text": "text/plain",
    "srt": "text/plain",
    "vtt": "text/vtt",
}


@app.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    language: Union[str, None] = Form(default=None),
    prompt: Union[str, None] = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
    _auth: None = Depends(require_api_key),
):
    """OpenAI-compatible transcription endpoint (/v1/audio/transcriptions)."""
    task_id = None
    valid_formats = {"json", "text", "srt", "vtt", "verbose_json"}
    if response_format not in valid_formats:
        return {"error": f"response_format must be one of {sorted(valid_formats)}"}

    try:
        file_content = await file.read()
        if not file_content:
            return {"error": "Audio file is empty"}

        audio_hash = generate_audio_hash(
            file_content,
            "transcribe",
            language,
            response_format,
            initial_prompt=prompt,
        )
        task_id = f"oai-{audio_hash}"

        final_language = language
        if force_detected_language_to:
            final_language = force_detected_language_to.to_iso_639_1()

        with task_results_lock:
            if task_id not in task_results:
                task_results[task_id] = TaskResult()
            task_result = task_results[task_id]

        asr_task_data = {
            'path': task_id,
            'type': 'asr',
            'task': 'transcribe',
            'language': final_language,
            'video_file': None,
            'initial_prompt': prompt,
            'audio_content': file_content,
            'encode': True,
            'output_format': response_format,
            'word_timestamps': response_format == 'verbose_json',
            'result_container': task_result,
        }

        if task_queue.put(asr_task_data):
            logging.info(f"OpenAI transcription task {task_id} queued")
        else:
            logging.info(f"OpenAI transcription task {task_id} already queued/processing - waiting")

        if await asyncio.to_thread(task_result.wait, asr_timeout):
            if task_result.error:
                return {"error": task_result.error}
            return StreamingResponse(
                iter([task_result.result]),
                media_type=_OPENAI_MEDIA_TYPES.get(response_format, "text/plain"),
            )
        else:
            return {"error": f"Transcription timed out after {asr_timeout} seconds"}

    except Exception as e:
        logging.error(f"Error in OpenAI transcriptions endpoint: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        await file.close()
        cleanup_task_result(task_id, require_inactive=True)


@app.post("/v1/audio/translations")
async def openai_translations(
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    prompt: Union[str, None] = Form(default=None),
    response_format: str = Form(default="json"),
    temperature: float = Form(default=0.0),
    _auth: None = Depends(require_api_key),
):
    """OpenAI-compatible translation endpoint (/v1/audio/translations). Always translates to English."""
    task_id = None
    valid_formats = {"json", "text", "srt", "vtt", "verbose_json"}
    if response_format not in valid_formats:
        return {"error": f"response_format must be one of {sorted(valid_formats)}"}

    try:
        file_content = await file.read()
        if not file_content:
            return {"error": "Audio file is empty"}

        audio_hash = generate_audio_hash(
            file_content,
            "translate",
            None,
            response_format,
            initial_prompt=prompt,
        )
        task_id = f"oai-{audio_hash}"

        with task_results_lock:
            if task_id not in task_results:
                task_results[task_id] = TaskResult()
            task_result = task_results[task_id]

        asr_task_data = {
            'path': task_id,
            'type': 'asr',
            'task': 'translate',
            'language': None,
            'video_file': None,
            'initial_prompt': prompt,
            'audio_content': file_content,
            'encode': True,
            'output_format': response_format,
            'word_timestamps': response_format == 'verbose_json',
            'result_container': task_result,
        }

        if task_queue.put(asr_task_data):
            logging.info(f"OpenAI translation task {task_id} queued")
        else:
            logging.info(f"OpenAI translation task {task_id} already queued/processing - waiting")

        if await asyncio.to_thread(task_result.wait, asr_timeout):
            if task_result.error:
                return {"error": task_result.error}
            return StreamingResponse(
                iter([task_result.result]),
                media_type=_OPENAI_MEDIA_TYPES.get(response_format, "text/plain"),
            )
        else:
            return {"error": f"Translation timed out after {asr_timeout} seconds"}

    except Exception as e:
        logging.error(f"Error in OpenAI translations endpoint: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        await file.close()
        cleanup_task_result(task_id, require_inactive=True)


# ============================================================================
# ASR WORKER FUNCTION
# ============================================================================

def get_audio_start_time(video_path: str) -> float:
    return _transcription.get_audio_start_time(_runtime(), video_path)


def apply_timestamp_offset(result, offset: float) -> None:
    return _transcription.apply_timestamp_offset(_runtime(), result, offset)


def asr_task_worker(task_data: dict) -> None:
    return _transcription.asr_task_worker(_runtime(), task_data)

async def get_audio_chunk(audio_file, offset=detect_language_offset, length=detect_language_length, sample_rate=16000, audio_format=np.int16):
    return await _transcription.get_audio_chunk(
        _runtime(), audio_file, offset, length, sample_rate, audio_format
    )

# ============================================================================
# REFACTORED /DETECT-LANGUAGE ENDPOINT WITH HASH-BASED DEDUPLICATION AND BLOCKING
# ============================================================================

@app.post("/detect-language")
async def detect_language(
    audio_file: UploadFile = File(...),
    encode: bool = Query(default=True),
    video_file: Union[str, None] = Query(default=None),
    detect_lang_length: int = Query(default=detect_language_length),
    detect_lang_offset: int = Query(default=detect_language_offset),
    _auth: None = Depends(require_api_key),
):
    global active_direct_tasks

    if force_detected_language_to:
        await audio_file.close()
        return {"detected_language": force_detected_language_to.to_name(), "language_code": force_detected_language_to.to_iso_639_1()}

    task_started = False
    try:
        file_content = await audio_file.read()
        if not file_content:
            return {"detected_language": "Unknown", "language_code": "und", "status": "error"}

        logging.info("Immediate language detection (Queue Bypass)" + (f" for {video_file}" if video_file else ""))

        # Track that we are directly using the model outside the queue
        with active_direct_tasks_lock:
            active_direct_tasks += 1
        task_started = True

        # --- RUN IMMEDIATELY ---
        # EVENT LOOP BLOCK FIX: Offload heavy ops to background thread
        await asyncio.to_thread(start_model)

        if encode:
            audio_bytes = await asyncio.to_thread(
                extract_audio_segment_from_content,
                file_content,
                detect_lang_offset,
                detect_lang_length
            )
            audio_data = np.frombuffer(audio_bytes, np.int16).flatten().astype(np.float32) / 32768.0
        else:
            audio_data = await get_audio_chunk(audio_file, detect_lang_offset, detect_lang_length)

        # Offload the heavy AI inference to a background thread
        result = await asyncio.to_thread(
            transcribe_with_model,
            audio_data,
            input_sr=16000,
            verbose=False,
        )

        detected = LanguageCode.from_string(result.language)

        logging.info(f"Detect Language Result: {detected.to_name()} ({detected.to_iso_639_1()})")

        return {
            "detected_language": detected.to_name(),
            "language_code": detected.to_iso_639_1()
        }

    except Exception as e:
        logging.error(f"Error in API detect-language: {e}", exc_info=True)
        return {"detected_language": "Unknown", "language_code": "und", "status": "error"}
    finally:
        await audio_file.close()
        # Decrement counter so delete_model() knows we are done
        if task_started:
            with active_direct_tasks_lock:
                active_direct_tasks -= 1
            delete_model() # Schedules VRAM cleanup if system is idle

# ============================================================================
# DETECT LANGUAGE WORKER FOR UPLOADED AUDIO
# ============================================================================

def detect_language_from_upload(task_data: dict) -> None:
    return _transcription.detect_language_from_upload(_runtime(), task_data)

# ============================================================================
# HELPER: Extract audio segment from in-memory content
# ============================================================================

def extract_audio_segment_from_content(audio_content: bytes, start_time: int, duration: int) -> bytes:
    return _transcription.extract_audio_segment_from_content(
        _runtime(), audio_content, start_time, duration
    )

def detect_language_task(path, original_task_data=None):
    return _transcription.detect_language_task(_runtime(), path, original_task_data)

def extract_audio_segment_to_memory(input_file, start_time, duration, track_index=None):
    return _transcription.extract_audio_segment_to_memory(
        _runtime(), input_file, start_time, duration, track_index
    )

def probe_media_duration(file_path):
    return _transcription.probe_media_duration(_runtime(), file_path)

def start_model():
    return _model_runtime.start_model(_runtime())

def initialize_model_runtime():
    return _model_runtime.initialize_model_runtime(_runtime())

def release_model(reason=None):
    return _model_runtime.release_model(_runtime(), reason)

def release_after_inference_failure(error):
    return _model_runtime.release_after_inference_failure(_runtime(), error)

def wait_for_model_recovery():
    return _model_runtime.wait_for_model_recovery(
        _runtime(), model_runtime_cancel_event
    )

def check_model_runtime_cancelled():
    if model_runtime_cancel_event.is_set():
        raise _model_runtime.ModelRuntimeCancelled(
            "Model runtime operation was cancelled"
        ) from None

def observe_idle_once():
    return _model_runtime.observe_idle_once(_runtime())

def run_model_idle_observer():
    return _model_runtime.run_model_idle_observer(_runtime())

def schedule_model_cleanup():
    return _model_runtime.schedule_model_cleanup(_runtime())

def perform_model_cleanup():
    return _model_runtime.perform_model_cleanup(_runtime())

def delete_model():
    return _model_runtime.delete_model(_runtime())

def is_audio_file_extension(file_extension):
    return _media.is_audio_file_extension(_runtime(), file_extension)

def write_lrc(result, file_path):
    return _transcription.write_lrc(_runtime(), result, file_path)

def send_completion_webhook(source_file_path: str, subtitle_file_path: str, language: LanguageCode, task_type: str):
    return _transcription.send_completion_webhook(
        _runtime(), source_file_path, subtitle_file_path, language, task_type
    )

def gen_subtitles(
    file_path: str,
    transcription_type: str,
    force_language: LanguageCode = LanguageCode.NONE,
    audio_tracks=None,
    audio_track_index: int | None = None,
    media_validation=None,
) -> None:
    return _transcription.gen_subtitles(
        _runtime(),
        file_path,
        transcription_type,
        force_language,
        audio_tracks,
        audio_track_index,
        media_validation,
    )

def define_subtitle_language_naming(language: LanguageCode, type):
    return _media.define_subtitle_language_naming(_runtime(), language, type)

def name_subtitle(file_path: str, language: LanguageCode) -> str:
    return _media.name_subtitle(_runtime(), file_path, language)

def handle_multiple_audio_tracks(
    file_path: str,
    language: LanguageCode | None = None,
    audio_tracks=None,
    audio_track_index: int | None = None,
) -> bytes | None:
    return _transcription.handle_multiple_audio_tracks(
        _runtime(), file_path, language, audio_tracks, audio_track_index
    )

def extract_audio_track_to_memory(input_video_path, track_index) -> bytes | None:
    return _transcription.extract_audio_track_to_memory(
        _runtime(), input_video_path, track_index
    )

def get_audio_track_by_language(audio_tracks, language):
    return _media.get_audio_track_by_language(audio_tracks, language)

def choose_transcribe_language(file_path, forced_language, audio_tracks=None):
    return _media.choose_transcribe_language(
        _runtime(), file_path, forced_language, audio_tracks
    )

def get_audio_tracks(video_file):
    return _media.get_audio_tracks(_runtime(), video_file)

def validate_media(file_path):
    return _media.validate_media(_runtime(), file_path)

def is_media_validation_current(file_path, validation):
    return _media.is_media_validation_current(_runtime(), file_path, validation)

def find_language_audio_track(audio_tracks, find_languages):
    return _media.find_language_audio_track(audio_tracks, find_languages)

def find_default_audio_track_language(audio_tracks):
    return _media.find_default_audio_track_language(audio_tracks)

def select_audio_track(audio_tracks, language: LanguageCode):
    return _media.select_audio_track(audio_tracks, language)

def gen_subtitles_queue(file_path: str, transcription_type: str, force_language: LanguageCode = LanguageCode.NONE, **task_kwargs) -> None:
    return _media.gen_subtitles_queue(
        _runtime(), file_path, transcription_type, force_language, **task_kwargs
    )

def should_skip_file(file_path: str, target_language: LanguageCode, audio_langs=None) -> bool:
    return _media.should_skip_file(
        _runtime(), file_path, target_language, audio_langs
    )

def get_subtitle_languages(video_path):
    return _media.get_subtitle_languages(_runtime(), video_path)

def get_audio_languages(video_path):
    return _media.get_audio_languages(_runtime(), video_path)

def subtitle_exists_in_language(video_file, target_language: LanguageCode):
    return _media.subtitle_exists_in_language(
        _runtime(), video_file, target_language
    )

def has_internal_subtitle_in_language(video_file: str, target_language: LanguageCode) -> bool:
    return _media.has_internal_subtitle_in_language(
        _runtime(), video_file, target_language
    )

def has_external_subtitle_in_language(video_file: str, target_language: LanguageCode, recursion: bool = True, only_match_subgen_subtitles: bool = False) -> bool:
    return _media.has_external_subtitle_in_language(
        _runtime(),
        video_file,
        target_language,
        recursion,
        only_match_subgen_subtitles,
    )

def is_valid_subtitle_language(subtitle_parts: List[str], target_language: LanguageCode) -> bool:
    return _media.is_valid_subtitle_language(subtitle_parts, target_language)

def get_next_plex_episode(current_episode_rating_key, stay_in_season: bool = False):
    return _plex_client.get_next_plex_episode(
        current_episode_rating_key,
        plexserver,
        plextoken,
        stay_in_season=stay_in_season,
        timeout=http_timeout,
        request_client=requests,
        logger=logging,
    )

def get_plex_file_name(itemid: str, server_ip: str, plex_token: str) -> str:
    return _plex_client.get_plex_file_name(
        itemid,
        server_ip,
        plex_token,
        timeout=http_timeout,
        request_client=requests,
        logger=logging,
    )

def refresh_plex_metadata(itemid: str, server_ip: str, plex_token: str) -> None:
    return _plex_client.refresh_plex_metadata(
        itemid,
        server_ip,
        plex_token,
        timeout=http_timeout,
        request_client=requests,
        logger=logging,
    )

def refresh_jellyfin_metadata(itemid: str, server_ip: str, jellyfin_token: str) -> None:
    return _jellyfin_client.refresh_jellyfin_metadata(
        itemid,
        server_ip,
        jellyfin_token,
        timeout=http_timeout,
        request_client=requests,
        logger=logging,
    )


def get_jellyfin_file_name(item_id: str, jellyfin_url: str, jellyfin_token: str) -> str:
    return _jellyfin_client.get_jellyfin_file_name(
        item_id,
        jellyfin_url,
        jellyfin_token,
        timeout=http_timeout,
        request_client=requests,
        logger=logging,
    )

def get_jellyfin_admin(users):
    return _jellyfin_client.get_jellyfin_admin(users)

def has_audio(file_path):
    return _media.has_audio(_runtime(), file_path)

def is_valid_path(file_path):
    return _media.is_valid_path(_runtime(), file_path)

def has_video_extension(file_name):
    return _media.has_video_extension(_runtime(), file_name)

def has_audio_extension(file_name):
    return _media.has_audio_extension(_runtime(), file_name)

def path_mapping(fullpath):
    return _media.path_mapping(_runtime(), fullpath)

def is_file_stable(file_path, wait_time=2, check_intervals=3):
    return _scanner.is_file_stable(
        _runtime(), file_path, wait_time, check_intervals
    )

SKIP_MARKER = _scanner.SKIP_MARKER


def _is_in_skipped_dir(file_path: str) -> bool:
    return _scanner._is_in_skipped_dir(_runtime(), file_path)


class NewFileHandler(_scanner.NewFileHandler):
    def __init__(self):
        super().__init__(_runtime())


def queue_existing(transcribe_folders, forceLanguage: LanguageCode = LanguageCode.NONE):
    return _scanner.queue_existing(_runtime(), transcribe_folders, forceLanguage)


def transcribe_existing(transcribe_folders, forceLanguage: LanguageCode = LanguageCode.NONE):
    return _scanner.transcribe_existing(_runtime(), transcribe_folders, forceLanguage)


if __name__ == "__main__":
    import uvicorn
    logging.info(f"Subgen v{subgen_version}")
    logging.info(f"Threads: {str(whisper_threads)}, Concurrent transcriptions: {str(concurrent_transcriptions)}")
    logging.info(f"Transcribe device: {transcribe_device}, Model: {whisper_model}")
    os.environ["KMP_DUPLICATE_LIB_OK"]="TRUE"
    uvicorn.run("__main__:app", host="0.0.0.0", port=int(webhookport), reload=reload_script_on_change, use_colors=True)
