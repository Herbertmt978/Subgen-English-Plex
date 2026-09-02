"""Media probing, track selection, subtitle policy, and path helpers."""

import math
import stat
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import List

from language_code import LanguageCode
from subgen_ops_safety import FileIdentity, file_identity

VIDEO_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
    ".vob",
    ".rm",
    ".rmvb",
    ".ts",
    ".m4v",
    ".f4v",
    ".svq3",
    ".asf",
    ".m2ts",
    ".divx",
    ".xvid",
)

AUDIO_EXTENSIONS = (
    ".mp3",
    ".wav",
    ".aac",
    ".flac",
    ".ogg",
    ".wma",
    ".alac",
    ".m4a",
    ".opus",
    ".aiff",
    ".aif",
    ".pcm",
    ".ra",
    ".ram",
    ".mid",
    ".midi",
    ".ape",
    ".wv",
    ".amr",
    ".vox",
    ".tak",
    ".spx",
    ".m4b",
    ".mka",
)

MEDIA_PROBE_TIMEOUT_SECONDS = 10.0
MAX_FFPROBE_RESPONSE_BYTES = 256 * 1024
MAX_PYAV_RESPONSE_BYTES = 16 * 1024
MAX_AUDIO_TRACKS = 64
MAX_TRACK_TEXT = 128
FFMPEG_INVALID_DATA = -1094995529


class ValidatorOutcome(str, Enum):
    """One bounded parser's opinion about a local media generation."""

    AUDIO_PRESENT = "audio_present"
    NO_AUDIO = "no_audio"
    INVALID_FORMAT = "invalid_format"
    INDETERMINATE = "indeterminate"


class MediaOutcome(str, Enum):
    """Conservative aggregate used by the local-media admission gate."""

    VALID_AUDIO = "valid_audio"
    NO_AUDIO = "no_audio"
    PROBE_INDETERMINATE = "probe_indeterminate"
    INVALID_MEDIA = "invalid_media"


@dataclass(frozen=True)
class AudioTrack:
    """Bounded immutable audio metadata carried into one queued task."""

    index: int
    codec: str = "Unknown"
    channels: int = 0
    language: LanguageCode = field(default_factory=lambda: LanguageCode.NONE)
    title: str = "None"
    default: bool = False
    forced: bool = False
    original: bool = False
    commentary: bool = False

    def as_task_dict(self) -> dict:
        return {
            "index": self.index,
            "codec": self.codec,
            "channels": self.channels,
            "language": self.language,
            "title": self.title,
            "default": self.default,
            "forced": self.forced,
            "original": self.original,
            "commentary": self.commentary,
        }


@dataclass(frozen=True)
class ValidatorEvidence:
    """Sanitized output from one validator; never contains parser dumps."""

    outcome: ValidatorOutcome
    duration_seconds: float | None = None
    audio_tracks: tuple[AudioTrack, ...] = ()
    detail_code: str | None = None


@dataclass(frozen=True)
class MediaValidation:
    """One exact-generation admission decision and reusable probe data."""

    outcome: MediaOutcome
    ffprobe: ValidatorEvidence
    pyav: ValidatorEvidence
    source_identity: FileIdentity | None = None
    duration_seconds: float | None = None
    audio_tracks: tuple[AudioTrack, ...] = ()
    detail_code: str | None = None


@dataclass(frozen=True)
class _SourceSnapshot:
    identity: FileIdentity
    mode: int
    link_count: int


@dataclass(frozen=True)
class _BoundedProcessResult:
    status: str
    returncode: int | None = None
    stdout: bytes = b""


class MediaValidationStale(RuntimeError):
    """The path no longer names the generation admitted by the queue."""


def aggregate_validator_outcomes(ffprobe, pyav) -> MediaOutcome:
    """Apply the complete conservative two-validator truth table."""

    ffprobe = ValidatorOutcome(ffprobe)
    pyav = ValidatorOutcome(pyav)
    outcomes = (ffprobe, pyav)
    if outcomes == (
        ValidatorOutcome.INVALID_FORMAT,
        ValidatorOutcome.INVALID_FORMAT,
    ):
        return MediaOutcome.INVALID_MEDIA
    if ValidatorOutcome.AUDIO_PRESENT in outcomes:
        return MediaOutcome.VALID_AUDIO
    if ValidatorOutcome.NO_AUDIO in outcomes:
        return MediaOutcome.NO_AUDIO
    return MediaOutcome.PROBE_INDETERMINATE


def _source_snapshot(runtime, file_path) -> _SourceSnapshot | None:
    try:
        metadata = runtime.os.lstat(file_path)
    except (OSError, TypeError, ValueError):
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    try:
        identity = file_identity(metadata)
    except (TypeError, ValueError):
        return None
    return _SourceSnapshot(
        identity=identity,
        mode=int(metadata.st_mode),
        link_count=int(metadata.st_nlink),
    )


def _kill_and_reap(runtime, process) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except (OSError, runtime.subprocess.TimeoutExpired):
        pass


def _run_bounded_process(
    runtime,
    command,
    *,
    timeout_seconds,
    max_stdout_bytes,
    cwd=None,
    creationflags=0,
) -> _BoundedProcessResult:
    """Run without a shell while retaining at most ``max_stdout_bytes``.

    A private temporary file avoids pipe readers that can remain blocked when
    an unexpected descendant inherits stdout. The file size is polled during
    the bounded wall-clock interval and only a capped payload is read back.
    """

    try:
        output = tempfile.TemporaryFile(mode="w+b")
    except OSError:
        return _BoundedProcessResult("io_error")
    with output:
        try:
            process = runtime.subprocess.Popen(
                command,
                stdin=runtime.subprocess.DEVNULL,
                stdout=output,
                stderr=runtime.subprocess.DEVNULL,
                shell=False,
                cwd=cwd,
                creationflags=creationflags,
            )
        except OSError:
            return _BoundedProcessResult("spawn_error")

        deadline = runtime.time.monotonic() + float(timeout_seconds)
        status = "completed"
        returncode = None
        while True:
            try:
                output_size = runtime.os.fstat(output.fileno()).st_size
                returncode = process.poll()
            except OSError:
                status = "io_error"
                _kill_and_reap(runtime, process)
                break
            if output_size > max_stdout_bytes:
                status = "overflow"
                _kill_and_reap(runtime, process)
                break
            if returncode is not None:
                break
            remaining = deadline - runtime.time.monotonic()
            if remaining <= 0:
                status = "timeout"
                _kill_and_reap(runtime, process)
                break
            runtime.time.sleep(min(0.02, remaining))

        if status != "completed":
            return _BoundedProcessResult(
                status=status,
                returncode=getattr(process, "returncode", returncode),
            )
        try:
            output.seek(0)
            stdout = output.read(max_stdout_bytes + 1)
        except OSError:
            return _BoundedProcessResult(
                "io_error",
                returncode=getattr(process, "returncode", returncode),
            )
        if len(stdout) > max_stdout_bytes:
            return _BoundedProcessResult(
                "overflow",
                returncode=getattr(process, "returncode", returncode),
            )
        return _BoundedProcessResult(
            "completed",
            returncode=getattr(process, "returncode", returncode),
            stdout=stdout,
        )


def _bounded_text(value, default):
    if not isinstance(value, str):
        return default
    return value[:MAX_TRACK_TEXT]


def _finite_duration(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _audio_track_from_mapping(stream) -> AudioTrack | None:
    if not isinstance(stream, dict):
        return None
    try:
        index = stream["index"]
        if isinstance(index, bool):
            return None
        index = int(index)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if index < 0:
        return None

    codec = _bounded_text(stream.get("codec_name", stream.get("codec")), "")
    if not codec or codec.casefold() in {"none", "unknown", "n/a"}:
        return None
    channels = stream.get("channels", 0)
    try:
        channels = int(channels)
    except (TypeError, ValueError, OverflowError):
        channels = 0
    if channels < 0 or channels > 128:
        channels = 0

    tags = stream.get("tags", {})
    if not isinstance(tags, dict):
        tags = {}
    language_value = _bounded_text(
        tags.get("language", stream.get("language")),
        "Unknown",
    )
    try:
        language = LanguageCode.from_iso_639_2(language_value)
    except (TypeError, ValueError):
        language = LanguageCode.NONE
    title = _bounded_text(tags.get("title", stream.get("title")), "None")

    disposition = stream.get("disposition", {})
    if not isinstance(disposition, dict):
        disposition = {}
    return AudioTrack(
        index=index,
        codec=codec,
        channels=channels,
        language=language,
        title=title,
        default=disposition.get("default", stream.get("default", 0)) in (1, True),
        forced=disposition.get("forced", stream.get("forced", 0)) in (1, True),
        original=disposition.get("original", stream.get("original", 0)) in (1, True),
        commentary="commentary" in title.casefold(),
    )


def _normalized_audio_tracks(streams) -> tuple[AudioTrack, ...] | None:
    if not isinstance(streams, list) or len(streams) > MAX_AUDIO_TRACKS:
        return None
    tracks = []
    for stream in streams:
        track = _audio_track_from_mapping(stream)
        if track is not None:
            tracks.append(track)
    return tuple(tracks)


def _probe_ffprobe(runtime, file_path) -> ValidatorEvidence:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_error",
        "-select_streams",
        "a",
        "-show_entries",
        (
            "format=format_name,duration:"
            "stream=index,codec_type,codec_name,channels,duration:"
            "stream_tags=language,title:"
            "stream_disposition=default,forced,original"
        ),
        "-of",
        "json",
        file_path,
    ]
    completed = _run_bounded_process(
        runtime,
        command,
        timeout_seconds=MEDIA_PROBE_TIMEOUT_SECONDS,
        max_stdout_bytes=MAX_FFPROBE_RESPONSE_BYTES,
    )
    if completed.status != "completed":
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code=f"ffprobe_{completed.status}",
        )
    try:
        payload = runtime.json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, runtime.json.JSONDecodeError):
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="ffprobe_malformed_reply",
        )
    if not isinstance(payload, dict):
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="ffprobe_malformed_reply",
        )

    format_data = payload.get("format", {})
    if not isinstance(format_data, dict):
        format_data = {}
    format_name = format_data.get("format_name")
    recognized_format = isinstance(format_name, str) and bool(format_name.strip())
    streams = payload.get("streams", [])
    if not isinstance(streams, list) or len(streams) > MAX_AUDIO_TRACKS:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="ffprobe_ambiguous_streams",
        )
    for stream in streams:
        if not isinstance(stream, dict):
            return ValidatorEvidence(
                ValidatorOutcome.INDETERMINATE,
                detail_code="ffprobe_ambiguous_streams",
            )
        stream_index = stream.get("index")
        if (
            isinstance(stream_index, bool)
            or not isinstance(stream_index, int)
            or stream_index < 0
            or stream.get("codec_type") != "audio"
        ):
            return ValidatorEvidence(
                ValidatorOutcome.INDETERMINATE,
                detail_code="ffprobe_ambiguous_streams",
            )
        codec_name = stream.get("codec_name")
        if (
            not isinstance(codec_name, str)
            or not codec_name.strip()
            or codec_name.casefold() in {"unknown", "n/a"}
        ):
            return ValidatorEvidence(
                ValidatorOutcome.INDETERMINATE,
                detail_code="ffprobe_ambiguous_streams",
            )
    tracks = _normalized_audio_tracks(streams)
    if tracks is None:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="ffprobe_ambiguous_streams",
        )
    duration = _finite_duration(format_data.get("duration"))
    if duration is None:
        stream_durations = [
            candidate
            for candidate in (
                _finite_duration(stream.get("duration")) for stream in streams
            )
            if candidate is not None
        ]
        if stream_durations:
            duration = max(stream_durations)

    if completed.returncode != 0:
        error_data = payload.get("error", {})
        error_code = error_data.get("code") if isinstance(error_data, dict) else None
        if (
            not isinstance(error_code, bool)
            and error_code == FFMPEG_INVALID_DATA
            and not recognized_format
            and not tracks
        ):
            return ValidatorEvidence(
                ValidatorOutcome.INVALID_FORMAT,
                detail_code="ffprobe_invalid_data",
            )
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="ffprobe_failed",
        )
    if tracks:
        return ValidatorEvidence(
            ValidatorOutcome.AUDIO_PRESENT,
            duration_seconds=duration,
            audio_tracks=tracks,
            detail_code="ffprobe_audio_present",
        )
    if recognized_format:
        return ValidatorEvidence(
            ValidatorOutcome.NO_AUDIO,
            duration_seconds=duration,
            detail_code="ffprobe_no_audio",
        )
    return ValidatorEvidence(
        ValidatorOutcome.INDETERMINATE,
        detail_code="ffprobe_unrecognized_container",
    )


def _pyav_payload(outcome, *, tracks=(), duration=None, detail_code=None):
    return {
        "schema_version": 1,
        "outcome": ValidatorOutcome(outcome).value,
        "duration_seconds": _finite_duration(duration),
        "audio_tracks": list(tracks),
        "detail_code": detail_code,
    }


def _is_pyav_invalid_data(av_module, error) -> bool:
    candidates = (
        getattr(av_module, "InvalidDataError", None),
        getattr(getattr(av_module, "error", None), "InvalidDataError", None),
    )
    return any(
        isinstance(candidate, type) and isinstance(error, candidate)
        for candidate in candidates
    )


def _pyav_disposition_mapping(av_module, stream):
    disposition = getattr(stream, "disposition", None)
    if isinstance(disposition, dict):
        return {
            name: disposition.get(name, False) in (1, True)
            for name in ("default", "forced", "original")
        }
    disposition_type = getattr(
        getattr(av_module, "stream", None),
        "Disposition",
        type(disposition),
    )
    fallback_masks = {"default": 0x0001, "original": 0x0004, "forced": 0x0040}
    flags = {}
    for name in ("default", "forced", "original"):
        member = getattr(disposition_type, name, None)
        if member is not None and disposition is not None:
            try:
                flags[name] = bool(disposition & member)
                continue
            except (TypeError, ValueError):
                pass
        try:
            numeric = int(disposition)
        except (TypeError, ValueError, OverflowError):
            flags[name] = False
        else:
            flags[name] = bool(numeric & fallback_masks[name])
    return flags


def _classify_with_pyav_module(av_module, file_path):
    """Pure child operation: open and request no more than one audio frame."""

    try:
        container = av_module.open(file_path)
    except Exception as exc:
        if _is_pyav_invalid_data(av_module, exc):
            return _pyav_payload(
                ValidatorOutcome.INVALID_FORMAT,
                detail_code="pyav_invalid_data",
            )
        return _pyav_payload(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_open_failed",
        )

    try:
        with container:
            audio_streams = [
                stream
                for stream in container.streams
                if getattr(stream, "type", None) == "audio"
            ]
            usable_streams = []
            for stream in audio_streams:
                context = getattr(stream, "codec_context", None)
                codec = getattr(context, "name", None)
                if codec and str(codec).casefold() != "none":
                    usable_streams.append(stream)
            if not usable_streams:
                return _pyav_payload(
                    ValidatorOutcome.NO_AUDIO,
                    detail_code="pyav_no_audio",
                )
            if len(usable_streams) > MAX_AUDIO_TRACKS:
                return _pyav_payload(
                    ValidatorOutcome.INDETERMINATE,
                    detail_code="pyav_too_many_audio_streams",
                )

            stream = usable_streams[0]
            try:
                next(iter(container.decode(stream)))
            except StopIteration:
                return _pyav_payload(
                    ValidatorOutcome.NO_AUDIO,
                    detail_code="pyav_no_audio_frame",
                )
            except Exception:
                return _pyav_payload(
                    ValidatorOutcome.INDETERMINATE,
                    detail_code="pyav_decode_failed",
                )

            tracks = []
            for audio_stream in usable_streams:
                context = getattr(audio_stream, "codec_context", None)
                metadata = getattr(audio_stream, "metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                tracks.append(
                    {
                        "index": int(getattr(audio_stream, "index", 0)),
                        "codec_name": str(getattr(context, "name", "Unknown"))[
                            :MAX_TRACK_TEXT
                        ],
                        "channels": int(getattr(context, "channels", 0) or 0),
                        "tags": {
                            "language": str(metadata.get("language", "Unknown"))[
                                :MAX_TRACK_TEXT
                            ],
                            "title": str(metadata.get("title", "None"))[
                                :MAX_TRACK_TEXT
                            ],
                        },
                        "disposition": _pyav_disposition_mapping(
                            av_module,
                            audio_stream,
                        ),
                    }
                )
            container_duration = getattr(container, "duration", None)
            time_base = getattr(av_module, "time_base", 1_000_000)
            try:
                duration = float(container_duration) / float(time_base)
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                duration = None
            return _pyav_payload(
                ValidatorOutcome.AUDIO_PRESENT,
                tracks=tracks,
                duration=duration,
                detail_code="pyav_audio_present",
            )
    except Exception:
        return _pyav_payload(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_probe_failed",
        )


def _pyav_child_main(argv=None) -> int:
    import json
    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3 or arguments[:2] != ["--pyav-probe-child", "--"]:
        return 64
    try:
        import av
    except Exception:
        payload = _pyav_payload(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_import_failed",
        )
    else:
        payload = _classify_with_pyav_module(av, arguments[2])
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    return 0


def _probe_pyav(runtime, file_path) -> ValidatorEvidence:
    package_root = runtime.os.path.abspath(
        runtime.os.path.join(runtime.os.path.dirname(__file__), runtime.os.pardir)
    )
    creationflags = (
        getattr(runtime.subprocess, "CREATE_NO_WINDOW", 0)
        if runtime.os.name == "nt"
        else 0
    )
    child_path = runtime.os.path.abspath(file_path)
    completed = _run_bounded_process(
        runtime,
        [
            runtime.sys.executable,
            "-m",
            "subgen_core.media",
            "--pyav-probe-child",
            "--",
            child_path,
        ],
        timeout_seconds=MEDIA_PROBE_TIMEOUT_SECONDS,
        max_stdout_bytes=MAX_PYAV_RESPONSE_BYTES,
        cwd=package_root,
        creationflags=creationflags,
    )
    if completed.status != "completed" or completed.returncode != 0:
        detail = (
            f"pyav_{completed.status}"
            if completed.status != "completed"
            else "pyav_child_failed"
        )
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code=detail,
        )
    try:
        payload = runtime.json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, runtime.json.JSONDecodeError):
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    expected_keys = {
        "schema_version",
        "outcome",
        "duration_seconds",
        "audio_tracks",
        "detail_code",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    if payload.get("schema_version") != 1:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    try:
        outcome = ValidatorOutcome(payload.get("outcome"))
    except ValueError:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    detail_code = payload.get("detail_code")
    if detail_code is not None and (
        not isinstance(detail_code, str) or len(detail_code) > 64
    ):
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    tracks = _normalized_audio_tracks(payload.get("audio_tracks"))
    if tracks is None:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    if outcome == ValidatorOutcome.AUDIO_PRESENT and not tracks:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    if outcome != ValidatorOutcome.AUDIO_PRESENT and tracks:
        return ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="pyav_malformed_reply",
        )
    return ValidatorEvidence(
        outcome,
        duration_seconds=_finite_duration(payload.get("duration_seconds")),
        audio_tracks=tracks,
        detail_code=detail_code,
    )


def _indeterminate_validation(
    ffprobe,
    pyav,
    source_identity,
    detail_code,
) -> MediaValidation:
    return MediaValidation(
        outcome=MediaOutcome.PROBE_INDETERMINATE,
        ffprobe=ffprobe,
        pyav=pyav,
        source_identity=source_identity,
        detail_code=detail_code,
    )


def validate_media(runtime, file_path):
    """Classify one unchanged regular-file generation with two validators."""

    initial = _source_snapshot(runtime, file_path)
    if initial is None:
        unavailable = ValidatorEvidence(
            ValidatorOutcome.INDETERMINATE,
            detail_code="source_unavailable",
        )
        return _indeterminate_validation(
            unavailable,
            unavailable,
            None,
            "source_unavailable",
        )

    ffprobe = _probe_ffprobe(runtime, file_path)
    between = _source_snapshot(runtime, file_path)
    if between != initial:
        return _indeterminate_validation(
            ffprobe,
            ValidatorEvidence(
                ValidatorOutcome.INDETERMINATE,
                detail_code="source_generation_changed",
            ),
            initial.identity,
            "source_generation_changed",
        )

    pyav = _probe_pyav(runtime, file_path)
    final = _source_snapshot(runtime, file_path)
    if final != initial:
        return _indeterminate_validation(
            ffprobe,
            pyav,
            initial.identity,
            "source_generation_changed",
        )

    outcome = aggregate_validator_outcomes(ffprobe.outcome, pyav.outcome)
    audio_evidence = next(
        (
            evidence
            for evidence in (ffprobe, pyav)
            if evidence.outcome == ValidatorOutcome.AUDIO_PRESENT
        ),
        None,
    )
    duration = next(
        (
            evidence.duration_seconds
            for evidence in (ffprobe, pyav)
            if evidence.duration_seconds is not None
        ),
        None,
    )
    detail_code = {
        MediaOutcome.VALID_AUDIO: "usable_audio_confirmed",
        MediaOutcome.NO_AUDIO: "valid_container_without_usable_audio",
        MediaOutcome.INVALID_MEDIA: "dual_parser_invalid",
        MediaOutcome.PROBE_INDETERMINATE: "validator_evidence_indeterminate",
    }[outcome]
    return MediaValidation(
        outcome=outcome,
        ffprobe=ffprobe,
        pyav=pyav,
        source_identity=initial.identity,
        duration_seconds=duration,
        audio_tracks=audio_evidence.audio_tracks if audio_evidence else (),
        detail_code=detail_code,
    )


def is_media_validation_current(runtime, file_path, validation):
    """Return whether a queued admission still names the same generation."""

    if (
        not isinstance(validation, MediaValidation)
        or validation.source_identity is None
    ):
        return False
    current = _source_snapshot(runtime, file_path)
    return current is not None and current.identity == validation.source_identity


def _task_audio_tracks(tracks):
    return [
        track.as_task_dict() if isinstance(track, AudioTrack) else dict(track)
        for track in tracks
    ]


def is_audio_file_extension(runtime, file_extension):
    return file_extension.casefold() in runtime.AUDIO_EXTENSIONS


def define_subtitle_language_naming(runtime, language: LanguageCode, type):
    """Return the configured language component for an output subtitle name."""
    if runtime.subtitle_language_name:
        return runtime.subtitle_language_name
    if runtime.transcribe_or_translate == "translate":
        language = LanguageCode.ENGLISH
    switch_dict = {
        "ISO_639_1": language.to_iso_639_1,
        "ISO_639_2_T": language.to_iso_639_2_t,
        "ISO_639_2_B": language.to_iso_639_2_b,
        "NAME": language.to_name,
        "NATIVE": lambda: language.to_name(in_english=False),
    }
    return switch_dict.get(type, language.to_name)()


def name_subtitle(runtime, file_path: str, language: LanguageCode) -> str:
    """Return the subtitle path Subgen will write for a media file."""
    gate_config = getattr(runtime, "task11b_gate_config", None)
    if gate_config is not None:
        file_path = gate_config.map_output_media_path(
            file_path,
            filesystem=runtime.os,
        )
    subgen_part = ".subgen" if runtime.show_in_subname_subgen else ""
    model_part = f".{runtime.whisper_model}" if runtime.show_in_subname_model else ""
    lang_part = runtime.define_subtitle_language_naming(
        language,
        runtime.subtitle_language_naming_type,
    )
    subtitle_path = (
        f"{runtime.os.path.splitext(file_path)[0]}"
        f"{subgen_part}{model_part}.{lang_part}.srt"
    )
    if gate_config is not None:
        gate_config.validate_output_artifact_path(
            subtitle_path,
            filesystem=runtime.os,
        )
    return subtitle_path


def get_audio_track_by_language(audio_tracks, language):
    """Return the first audio track matching ``language``."""
    for track in audio_tracks:
        if track["language"] == language:
            return track
    return None


def choose_transcribe_language(runtime, file_path, forced_language, audio_tracks=None):
    """Choose the language used for detection and transcription."""
    if forced_language:
        runtime.logging.debug(
            f"ENV FORCE_LANGUAGE is set: Forcing language to {forced_language}"
        )
        return forced_language

    if runtime.force_detected_language_to:
        runtime.logging.debug(
            "ENV FORCE_DETECTED_LANGUAGE_TO is set: Forcing detected language to "
            f"{runtime.force_detected_language_to}"
        )
        return runtime.force_detected_language_to

    if audio_tracks is None:
        audio_tracks = runtime.get_audio_tracks(file_path)

    preferred_track_language = runtime.find_language_audio_track(
        audio_tracks,
        runtime.preferred_audio_languages,
    )
    if preferred_track_language:
        return preferred_track_language

    default_language = runtime.find_default_audio_track_language(audio_tracks)
    if default_language:
        runtime.logging.debug(f"Default language found: {default_language}")
        return default_language

    return LanguageCode.NONE


def get_audio_tracks(runtime, video_file):
    """Extract audio stream metadata from a media file."""
    try:
        probe = runtime.ffmpeg.probe(video_file, select_streams="a")
        audio_streams = probe.get("streams", [])
        audio_tracks = []
        for stream in audio_streams:
            audio_track = {
                "index": int(stream.get("index", 0)),
                "codec": stream.get("codec_name", "Unknown"),
                "channels": int(stream.get("channels", 0)),
                "language": LanguageCode.from_iso_639_2(
                    stream.get("tags", {}).get("language", "Unknown")
                ),
                "title": stream.get("tags", {}).get("title", "None"),
                "default": stream.get("disposition", {}).get("default", 0) == 1,
                "forced": stream.get("disposition", {}).get("forced", 0) == 1,
                "original": stream.get("disposition", {}).get("original", 0) == 1,
                "commentary": "commentary"
                in stream.get("tags", {}).get("title", "").lower(),
            }
            audio_tracks.append(audio_track)
        return audio_tracks
    except runtime.ffmpeg.Error as exc:
        runtime.logging.error(f"FFmpeg error: {exc.stderr}")
        return []
    except Exception as exc:
        runtime.logging.error(
            f"An error occurred while reading audio track information: {str(exc)}"
        )
        return []


def find_language_audio_track(audio_tracks, find_languages):
    """Return the first preferred language represented by an audio track."""
    for language in find_languages:
        for track in audio_tracks:
            if track["language"] == language:
                return language
    return None


def find_default_audio_track_language(audio_tracks):
    """Return the language of the default audio track, if present."""
    for track in audio_tracks:
        if track["default"] is True:
            return track["language"]
    return None


def select_audio_track(audio_tracks, language: LanguageCode):
    """Return the exact track used for detection and transcription."""
    if language:
        language_track = get_audio_track_by_language(audio_tracks, language)
        if language_track:
            return language_track

    default_track = next(
        (track for track in audio_tracks if track.get("default")), None
    )
    return default_track or (audio_tracks[0] if audio_tracks else None)


def gen_subtitles_queue(
    runtime,
    file_path: str,
    transcription_type: str,
    force_language: LanguageCode = LanguageCode.NONE,
    **task_kwargs,
) -> None:
    """Apply media policy and enqueue one transcription or detection task."""
    if runtime.task_queue.is_active(file_path):
        runtime.logging.debug(
            f"Ignored: {runtime.os.path.basename(file_path)} is already queued or processing."
        )
        return

    if runtime.skip_marked_failed_files:
        marker_decision = runtime.failure_marker_reader.check(file_path)
        marker_task = {"path": file_path, "type": transcription_type}
        if marker_decision.status == "matched":
            if marker_decision.report:
                runtime.logging.warning(
                    "Skipping exact failed media generation: %s",
                    file_path,
                )
                runtime.emit_subgen_event(
                    "failure_marker_skip",
                    marker_task,
                    marker_decision.detail,
                )
            return
        if marker_decision.status == "stale" and marker_decision.report:
            runtime.logging.info(
                "Failure marker is stale for replacement media: %s",
                file_path,
            )
            runtime.emit_subgen_event(
                "failure_marker_stale",
                marker_task,
                marker_decision.detail,
            )
        elif marker_decision.status == "unavailable" and marker_decision.report:
            runtime.logging.warning(
                "Failure marker registry is unavailable; processing normally: %s",
                file_path,
            )
            runtime.emit_subgen_event(
                "failure_marker_read_failed",
                marker_task,
                marker_decision.detail,
            )

    validation = runtime.validate_media(file_path)
    runtime.logging.info(
        "MEDIA_VALIDATION outcome=%s ffprobe=%s pyav=%s path=%s",
        validation.outcome.value,
        validation.ffprobe.outcome.value,
        validation.pyav.outcome.value,
        file_path,
    )
    if validation.outcome == MediaOutcome.NO_AUDIO:
        runtime.logging.debug(f"{file_path} doesn't have any audio to transcribe!")
        return
    if validation.outcome in {
        MediaOutcome.INVALID_MEDIA,
        MediaOutcome.PROBE_INDETERMINATE,
    }:
        runtime.emit_subgen_event(
            "media_validation_failed",
            {"path": file_path, "type": "transcribe"},
            failure_class=validation.outcome.value,
            source_identity=validation.source_identity,
            validator_outcomes={
                "ffprobe": validation.ffprobe.outcome.value,
                "pyav": validation.pyav.outcome.value,
            },
            validation_detail=validation.detail_code,
        )
        return

    audio_tracks = _task_audio_tracks(validation.audio_tracks)
    audio_langs = [track["language"] for track in audio_tracks]

    explicitly_forced_language = bool(force_language)
    force_language = runtime.choose_transcribe_language(
        file_path,
        force_language,
        audio_tracks=audio_tracks,
    )
    selected_track = runtime.select_audio_track(audio_tracks, force_language)
    selected_track_index = selected_track.get("index") if selected_track else None
    selected_audio_language = (
        selected_track.get("language", force_language)
        if selected_track
        else force_language
    )

    if runtime.should_skip_file(file_path, force_language, audio_langs=audio_langs):
        return

    reserved_task_fields = {
        "path",
        "type",
        "transcribe_or_translate",
        "force_language",
        "audio_track_index",
        "audio_tracks",
        "selected_audio_language",
        "media_validation",
        "media_duration",
    }
    blocked_task_fields = sorted(reserved_task_fields.intersection(task_kwargs))
    if blocked_task_fields:
        runtime.logging.warning(
            "Ignoring reserved queued-task fields: %s",
            ", ".join(blocked_task_fields),
        )
    task_metadata = {
        key: value
        for key, value in task_kwargs.items()
        if key not in reserved_task_fields
    }

    if (
        runtime.should_whisper_detect_audio_language
        and not explicitly_forced_language
        and not runtime.force_detected_language_to
    ):
        detect_task = {
            **task_metadata,
            "path": file_path,
            "type": "detect_language",
            "audio_tracks": audio_tracks,
            "selected_audio_language": selected_audio_language,
            "audio_track_index": selected_track_index,
            "media_validation": validation,
            "media_duration": validation.duration_seconds,
        }
        runtime.task_queue.put(detect_task)
        return

    task = {
        **task_metadata,
        "path": file_path,
        "transcribe_or_translate": transcription_type,
        "force_language": force_language,
        "audio_track_index": selected_track_index,
        "audio_tracks": audio_tracks,
        "media_validation": validation,
        "media_duration": validation.duration_seconds,
    }
    runtime.task_queue.put(task)


def should_skip_file(
    runtime,
    file_path: str,
    target_language: LanguageCode,
    audio_langs=None,
) -> bool:
    """Return whether existing media/subtitles and configured policy skip a file."""
    base_name = runtime.os.path.basename(file_path)
    file_name, file_ext = runtime.os.path.splitext(base_name)
    if runtime.transcribe_or_translate == "translate":
        target_language = LanguageCode.ENGLISH

    if runtime.is_audio_file_extension(file_ext) and runtime.lrc_for_audio_files:
        lrc_path = runtime.os.path.join(
            runtime.os.path.dirname(file_path),
            f"{file_name}.lrc",
        )
        if runtime.os.path.exists(lrc_path):
            runtime.logging.info(f"Skipping {base_name}: LRC file already exists.")
            return True

    if target_language == LanguageCode.NONE:
        if runtime.skip_unknown_language:
            runtime.logging.info(
                f"Skipping {base_name}: Audio language unknown and "
                "SKIP_UNKNOWN_LANGUAGE is enabled."
            )
            return True
        if (
            runtime.skip_if_no_audio_language_but_subtitles_exist
            and runtime.get_subtitle_languages(file_path)
        ):
            runtime.logging.info(
                f"Skipping {base_name}: Audio language unknown but internal subtitles "
                "already exist."
            )
            return True

    if audio_langs is None:
        audio_langs = runtime.get_audio_languages(file_path)

    if runtime.limit_to_preferred_audio_languages:
        if not any(lang in runtime.preferred_audio_languages for lang in audio_langs):
            preferred_names = [
                lang.to_name() for lang in runtime.preferred_audio_languages
            ]
            runtime.logging.info(
                f"Skipping {base_name}: No preferred audio tracks found "
                f"(looking for {', '.join(preferred_names)})"
            )
            return True

    if any(lang in runtime.skip_audio_languages for lang in audio_langs):
        runtime.logging.info(
            f"Skipping {base_name}: Contains a skipped audio language."
        )
        return True

    if runtime.skip_if_target_subtitle_exists:
        named_output_configured = (
            runtime.subtitle_language_name
            and LanguageCode.is_valid_language(runtime.subtitle_language_name)
        )
        if not (target_language == LanguageCode.NONE and named_output_configured):
            if runtime.subtitle_exists_in_language(file_path, target_language):
                if target_language == LanguageCode.NONE:
                    runtime.logging.info(
                        f"Skipping {base_name}: Subtitles already exist and audio language "
                        "could not be detected from file metadata."
                    )
                else:
                    lang_name = target_language.to_name()
                    runtime.logging.info(
                        f"Skipping {base_name}: Subtitles already exist in {lang_name}."
                    )
                return True

        if named_output_configured:
            external_lang = LanguageCode.from_string(runtime.subtitle_language_name)
            if runtime.has_external_subtitle_in_language(
                file_path,
                external_lang,
                recursion=True,
                only_match_subgen_subtitles=runtime.only_match_subgen_subtitles,
            ):
                runtime.logging.info(
                    f"Skipping {base_name}: Subtitles already exist in custom name "
                    f"'{runtime.subtitle_language_name}'."
                )
                return True

        expected_output = runtime.name_subtitle(file_path, target_language)
        if runtime.os.path.exists(expected_output):
            runtime.logging.info(
                f"Skipping {base_name}: Generated subtitle "
                f"'{runtime.os.path.basename(expected_output)}' already exists."
            )
            return True

    if (
        runtime.skip_if_internal_sub_language
        and runtime.has_internal_subtitle_in_language(
            file_path,
            runtime.skip_if_internal_sub_language,
        )
    ):
        lang_name = runtime.skip_if_internal_sub_language.to_name()
        runtime.logging.info(
            f"Skipping {base_name}: Internal subtitles in {lang_name} already exist."
        )
        return True

    if runtime.skip_subtitle_languages and any(
        lang in runtime.skip_subtitle_languages
        for lang in runtime.get_subtitle_languages(file_path)
    ):
        runtime.logging.info(
            f"Skipping {base_name}: Contains a skipped subtitle language."
        )
        return True

    if (
        runtime.skip_if_external_sub_exists
        and runtime.subtitle_language_name
        and LanguageCode.is_valid_language(runtime.subtitle_language_name)
    ):
        external_lang = LanguageCode.from_string(runtime.subtitle_language_name)
        if runtime.has_external_subtitle_in_language(
            file_path,
            external_lang,
            recursion=True,
            only_match_subgen_subtitles=runtime.only_match_subgen_subtitles,
        ):
            lang_name = external_lang.to_name()
            runtime.logging.info(
                f"Skipping {base_name}: External subtitles in {lang_name} already exist."
            )
            return True

    return False


def get_subtitle_languages(runtime, video_path):
    """Return language codes for non-ignored embedded subtitle streams."""
    languages = []
    try:
        with runtime.av.open(video_path) as container:
            for stream in container.streams.subtitles:
                if runtime.ignore_forced_subtitles and bool(
                    stream.disposition & runtime.av.stream.Disposition.forced
                ):
                    runtime.logging.debug(
                        "get_subtitle_languages: skipping forced subtitle stream in "
                        f"{video_path}"
                    )
                    continue
                lang_code = stream.metadata.get("language")
                if lang_code:
                    languages.append(LanguageCode.from_iso_639_2(lang_code))
                else:
                    languages.append(LanguageCode.NONE)
    except Exception as exc:
        runtime.logging.warning(
            f"Could not read subtitle streams from {video_path}: {exc}"
        )
    return languages


def get_audio_languages(runtime, video_path):
    """Return language codes for all audio streams in a media file."""
    audio_tracks = runtime.get_audio_tracks(video_path)
    return [track["language"] for track in audio_tracks]


def subtitle_exists_in_language(runtime, video_file, target_language: LanguageCode):
    """Return whether matching internal or external subtitle coverage exists."""
    internal = (
        not runtime.only_match_subgen_subtitles
    ) and runtime.has_internal_subtitle_in_language(video_file, target_language)
    external = runtime.has_external_subtitle_in_language(
        video_file,
        target_language,
        recursion=True,
        only_match_subgen_subtitles=runtime.only_match_subgen_subtitles,
    )
    return internal or external


def has_internal_subtitle_in_language(
    runtime,
    video_file: str,
    target_language: LanguageCode,
) -> bool:
    """Return whether a non-ignored embedded subtitle track matches a language."""
    try:
        with runtime.av.open(video_file) as container:
            for stream in container.streams:
                lang_tag = (
                    stream.metadata.get("language", "") if stream.metadata else ""
                )
                is_forced = bool(
                    stream.disposition & runtime.av.stream.Disposition.forced
                )
                runtime.logging.debug(
                    f"has_internal_subtitle_in_language: stream #{stream.index} "
                    f"type={stream.type!r} lang={lang_tag!r} forced={is_forced} "
                    f"target={target_language}"
                )
                if stream.type == "subtitle" and "language" in stream.metadata:
                    if runtime.ignore_forced_subtitles and is_forced:
                        runtime.logging.debug(
                            f"Skipping forced subtitle stream (language={lang_tag}) in "
                            f"{video_file}"
                        )
                        continue
                    stream_language = LanguageCode.from_string(lang_tag.lower())
                    if stream_language == target_language:
                        return True
            return False
    except Exception as exc:
        runtime.logging.error(
            "An error occurred while checking the file with pyav: "
            f"{type(exc).__name__}: {exc}"
        )
        return False


def has_external_subtitle_in_language(
    runtime,
    video_file: str,
    target_language: LanguageCode,
    recursion: bool = True,
    only_match_subgen_subtitles: bool = False,
) -> bool:
    """Return whether a matching subtitle file exists beside the media file."""
    subtitle_extensions = {
        ".srt",
        ".vtt",
        ".sub",
        ".ass",
        ".ssa",
        ".idx",
        ".sbv",
        ".pgs",
        ".ttml",
        ".lrc",
    }
    video_folder = runtime.os.path.dirname(video_file)
    video_name = runtime.os.path.splitext(runtime.os.path.basename(video_file))[0]

    try:
        dir_entries = runtime.os.listdir(video_folder)
    except OSError as exc:
        runtime.logging.warning(f"Could not list directory {video_folder}: {exc}")
        return False

    for file_name in dir_entries:
        file_path = runtime.os.path.join(video_folder, file_name)
        if runtime.os.path.isfile(file_path) and file_path.endswith(
            tuple(subtitle_extensions)
        ):
            subtitle_name, _extension = runtime.os.path.splitext(file_name)
            if not subtitle_name.startswith(video_name):
                continue

            subtitle_parts = subtitle_name[len(video_name) :].lstrip(".").split(".")
            has_subgen = "subgen" in subtitle_parts

            if target_language == LanguageCode.NONE:
                if only_match_subgen_subtitles:
                    if has_subgen:
                        return True
                    continue
                return True

            if runtime.is_valid_subtitle_language(subtitle_parts, target_language):
                if only_match_subgen_subtitles and not has_subgen:
                    continue
                runtime.logging.debug(
                    f"Found matching subtitle: {file_name} for language "
                    f"{target_language.name} (subgen={has_subgen})"
                )
                return True
        elif runtime.os.path.isdir(file_path) and recursion:
            if runtime.has_external_subtitle_in_language(
                runtime.os.path.join(
                    file_path,
                    runtime.os.path.basename(video_file),
                ),
                target_language,
                False,
                only_match_subgen_subtitles,
            ):
                return True

    return False


def is_valid_subtitle_language(
    subtitle_parts: List[str],
    target_language: LanguageCode,
) -> bool:
    """Return whether any subtitle name component identifies a language."""
    return any(
        LanguageCode.from_string(part) == target_language for part in subtitle_parts
    )


def has_audio(runtime, file_path):
    """Compatibility predicate over the side-effect-free canonical classifier."""
    return runtime.validate_media(file_path).outcome == MediaOutcome.VALID_AUDIO


def is_valid_path(runtime, file_path):
    """Return whether a path identifies a regular file."""
    if not runtime.os.path.isfile(file_path):
        if not runtime.os.path.isdir(file_path):
            runtime.logging.warning(
                f"{file_path} is neither a file nor a directory. Are your volumes correct?"
            )
            return False
        runtime.logging.debug(
            f"{file_path} is a directory, skipping processing as a file."
        )
        return False
    return True


def has_video_extension(runtime, file_name):
    """Return whether a file has a supported, non-skipped video extension."""
    file_extension = runtime.os.path.splitext(file_name)[1].lower()
    return (
        file_extension in runtime.VIDEO_EXTENSIONS
        and file_extension not in runtime.skip_video_extensions
    )


def has_audio_extension(runtime, file_name):
    """Return whether a file has a supported audio extension."""
    file_extension = runtime.os.path.splitext(file_name)[1].lower()
    return file_extension in runtime.AUDIO_EXTENSIONS


def path_mapping(runtime, fullpath):
    """Apply the configured path mapping, if enabled."""
    if runtime.use_path_mapping:
        mapped_path = fullpath.replace(
            runtime.path_mapping_from,
            runtime.path_mapping_to,
        )
        runtime.logging.debug("Updated path: " + mapped_path)
        return mapped_path
    return fullpath


if __name__ == "__main__":  # pragma: no cover - exercised by the isolated child gate
    raise SystemExit(_pyav_child_main())
