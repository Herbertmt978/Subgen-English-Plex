"""Audio extraction, language detection, ASR, and subtitle output algorithms."""

import hashlib as _hashlib
import math as _math
import stat as _stat
import tempfile

from . import runtime_receipts as _runtime_receipts
from . import segmentation as _segmentation


class MediaDurationError(RuntimeError):
    """A local media duration could not be established safely."""


class AudioSegmentExtractionError(RuntimeError):
    """One selected source interval could not be extracted."""


def _duration_ms(duration):
    """Convert one positive finite media duration to its gate cursor unit."""

    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not _math.isfinite(duration)
    ):
        raise MediaDurationError("Media duration cannot be represented in milliseconds")
    milliseconds = int(round(float(duration) * 1000.0))
    if milliseconds <= 0:
        raise MediaDurationError("Media duration cannot be represented in milliseconds")
    return milliseconds


def _cursor_ms(cursor):
    """Convert one nonnegative committed source cursor to milliseconds."""

    if (
        isinstance(cursor, bool)
        or not isinstance(cursor, (int, float))
        or not _math.isfinite(cursor)
    ):
        raise RuntimeError("Workload cursor cannot be represented in milliseconds")
    milliseconds = int(round(float(cursor) * 1000.0))
    if milliseconds < 0:
        raise RuntimeError("Workload cursor cannot be represented in milliseconds")
    return milliseconds


def _file_sha256(file_path):
    """Hash the complete disposable fixture without retaining its media bytes."""

    digest = _hashlib.sha256()
    with open(file_path, "rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _gate_workload_sha256(
    runtime,
    file_path,
    transcription_type,
    force_language,
    total_duration_ms,
):
    """Return the private fixture identity only for the owner-operated gate."""

    coordinator = getattr(runtime, "runtime_receipt_coordinator", None)
    if not bool(getattr(coordinator, "gate_enabled", False)):
        return None
    language = force_language.to_iso_639_1()
    if transcription_type not in {"transcribe", "translate"}:
        raise RuntimeError("Task 11B workload task is not transcribe or translate")
    if (
        not isinstance(language, str)
        or len(language) != 2
        or not language.isascii()
        or not language.isalpha()
        or language.casefold() != language
    ):
        raise RuntimeError("Task 11B workload language is not lowercase ISO-639-1")
    identity = {
        "fixture_sha256": _file_sha256(file_path),
        "task": transcription_type,
        "language": language,
        "cursor_start_ms": 0,
        "total_duration_ms": total_duration_ms,
    }
    return _hashlib.sha256(_runtime_receipts.canonical_json_line(identity)).hexdigest()


def _receipt_runtime_state_locked(runtime, coordinator):
    """Capture private gate state through the model-runtime owner."""

    if not bool(getattr(coordinator, "gate_enabled", False)):
        return None
    owner = getattr(runtime, "_model_runtime", None)
    snapshot = getattr(owner, "runtime_receipt_state_locked", None)
    if not callable(snapshot):
        raise RuntimeError("Task 11B runtime receipt state owner is unavailable")
    return snapshot(runtime)


def _with_receipt_condition(runtime, operation):
    """Run one coordinator mutation under the shared model condition."""

    coordinator = getattr(runtime, "runtime_receipt_coordinator", None)
    if coordinator is None:
        return None
    condition = getattr(runtime, "model_runtime_condition", None)
    if condition is None:
        if bool(getattr(coordinator, "gate_enabled", False)):
            raise RuntimeError("Task 11B model runtime condition is unavailable")
        return operation(coordinator, None)
    with condition:
        return operation(
            coordinator,
            _receipt_runtime_state_locked(runtime, coordinator),
        )


def _begin_workload(runtime, workload_sha256):
    return _with_receipt_condition(
        runtime,
        lambda coordinator, state: coordinator.begin_workload_locked(
            workload_sha256,
            cursor_ms=0,
            runtime_state=state,
        ),
    )


def _record_workload_chunk(runtime, token, *, cursor_ms, chunk_uncommitted):
    if token is None:
        return False
    return _with_receipt_condition(
        runtime,
        lambda coordinator, state: coordinator.record_chunk_locked(
            token,
            cursor_ms=cursor_ms,
            chunk_uncommitted=chunk_uncommitted,
            runtime_state=state,
        ),
    )


def _abort_workload(runtime, token):
    if token is None:
        return None
    return _with_receipt_condition(
        runtime,
        lambda coordinator, state: coordinator.abort_workload_locked(token, state),
    )


def _complete_workload(runtime, token, terminal_cursor_ms):
    if token is None:
        return None
    return _with_receipt_condition(
        runtime,
        lambda coordinator, state: coordinator.complete_workload_locked(
            token,
            terminal_cursor_ms=terminal_cursor_ms,
            runtime_state=state,
        ),
    )


def get_audio_start_time(runtime, video_path: str) -> float:
    """Return a significant audio-stream start offset reported by ffprobe."""
    if not video_path or not runtime.os.path.isfile(video_path):
        return 0.0

    try:
        result = runtime.subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=start_time",
                "-of",
                "json",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return 0.0

        data = runtime.json.loads(result.stdout)
        streams = data.get("streams", [])
        if streams:
            start_time = float(streams[0].get("start_time", 0))
            if start_time > 0.1:
                runtime.logging.info(
                    f"Detected audio start_time offset: {start_time:.3f}s for "
                    f"{runtime.os.path.basename(video_path)}"
                )
                return start_time
    except (
        runtime.subprocess.TimeoutExpired,
        runtime.json.JSONDecodeError,
        ValueError,
        OSError,
    ) as exc:
        runtime.logging.debug(
            f"Could not detect audio start_time for {video_path}: {exc}"
        )

    return 0.0


def apply_timestamp_offset(runtime, result, offset: float) -> None:
    """Shift segment word timestamps onto the source container timeline."""
    if offset <= 0:
        return

    for segment in result.segments:
        if hasattr(segment, "words") and segment.words:
            for word in segment.words:
                word.start += offset
                word.end += offset
        else:
            if hasattr(segment, "_default_start"):
                segment._default_start += offset
            if hasattr(segment, "_default_end"):
                segment._default_end += offset

    runtime.logging.info(
        f"Applied +{offset:.3f}s timestamp offset to {len(result.segments)} segments"
    )


def asr_task_worker(runtime, task_data: dict) -> None:
    """Process one queued ASR request and signal its blocking result container."""
    result = None
    task_id = task_data.get("path", "unknown")
    result_container = task_data.get("result_container")

    try:
        task = task_data["task"]
        language = task_data["language"]
        video_file = task_data.get("video_file")
        initial_prompt = task_data.get("initial_prompt")
        file_content = task_data["audio_content"]
        encode = task_data["encode"]

        runtime.start_model()

        args = {}
        display_name = runtime.os.path.basename(video_file) if video_file else task_id
        args["progress_callback"] = runtime.ProgressHandler(display_name)

        if encode:
            args["audio"] = file_content
        else:
            args["audio"] = (
                runtime.np.frombuffer(file_content, runtime.np.int16)
                .flatten()
                .astype(runtime.np.float32)
                / 32768.0
            )
            args["input_sr"] = 16000

        if runtime.custom_regroup and runtime.custom_regroup.lower() != "default":
            args["regroup"] = runtime.custom_regroup

        if initial_prompt:
            args["initial_prompt"] = initial_prompt

        args.update(runtime.kwargs)

        audio_offset = runtime.get_audio_start_time(video_file) if video_file else 0.0
        result = _unsegmented_inference_with_recovery(
            runtime,
            lambda: runtime.transcribe_with_model(
                task=task,
                language=language,
                **args,
                verbose=None,
            ),
            "uploaded ASR request",
        )

        if audio_offset > 0:
            runtime.apply_timestamp_offset(result, audio_offset)

        runtime.appendLine(result)

        if result_container:
            output_format = task_data.get("output_format") or task_data.get(
                "output", "srt"
            )
            word_level = task_data.get("word_timestamps", runtime.word_level_highlight)
            if output_format == "json":
                formatted = runtime.json.dumps({"text": result.text.strip()})
            elif output_format in {"text", "txt"}:
                formatted = result.text.strip()
            elif output_format == "vtt":
                formatted = result.to_srt_vtt(
                    filepath=None,
                    word_level=word_level,
                    vtt=True,
                )
            elif output_format == "tsv":
                formatted = "start\tend\ttext\n" + "\n".join(
                    f"{segment.start:.3f}\t{segment.end:.3f}\t{segment.text.strip()}"
                    for segment in result.segments
                )
            elif output_format == "verbose_json":
                segments = []
                for index, segment in enumerate(result.segments):
                    formatted_segment = {
                        "id": index,
                        "seek": 0,
                        "start": round(segment.start, 3),
                        "end": round(segment.end, 3),
                        "text": segment.text,
                        "tokens": [],
                        "temperature": 0.0,
                        "avg_logprob": 0.0,
                        "compression_ratio": 1.0,
                        "no_speech_prob": 0.0,
                    }
                    if segment.words:
                        formatted_segment["words"] = [
                            {
                                "word": word.word,
                                "start": round(word.start, 3),
                                "end": round(word.end, 3),
                            }
                            for word in segment.words
                        ]
                    segments.append(formatted_segment)
                formatted = runtime.json.dumps(
                    {
                        "task": task,
                        "language": result.language,
                        "duration": round(result.segments[-1].end, 3)
                        if result.segments
                        else 0.0,
                        "text": result.text.strip(),
                        "segments": segments,
                    }
                )
            else:
                formatted = result.to_srt_vtt(
                    filepath=None,
                    word_level=word_level,
                )
            result_container.set_result(formatted)

    except Exception as exc:
        runtime.logging.error(
            f"Error processing ASR (ID: {task_id}): {exc}",
            exc_info=True,
        )
        if result_container:
            result_container.set_error(str(exc))

    finally:
        runtime.delete_model()


async def get_audio_chunk(
    runtime,
    audio_file,
    offset,
    length,
    sample_rate,
    audio_format,
):
    """Read and normalize one PCM chunk from an async uploaded file."""
    bytes_per_sample = runtime.np.dtype(audio_format).itemsize
    start_byte = offset * sample_rate * bytes_per_sample
    length_in_bytes = length * sample_rate * bytes_per_sample
    await audio_file.seek(start_byte)
    chunk = await audio_file.read(length_in_bytes)
    return (
        runtime.np.frombuffer(chunk, dtype=audio_format)
        .flatten()
        .astype(runtime.np.float32)
        / 32768.0
    )


def detect_language_from_upload(runtime, task_data: dict) -> None:
    """Detect language for uploaded audio and signal its result container."""
    detected_language = runtime.LanguageCode.NONE
    task_id = task_data.get("path", "unknown")
    result_container = task_data.get("result_container")

    try:
        video_file = task_data.get("video_file")
        file_content = task_data["audio_content"]
        encode = task_data["encode"]
        detect_lang_length = task_data["detect_lang_length"]
        detect_lang_offset = task_data["detect_lang_offset"]

        runtime.logging.info(
            f"Detecting language for '{video_file}' ({detect_lang_length}s starting at "
            f"{detect_lang_offset}s) - ID: {task_id}"
            if video_file
            else f"Detecting language ({detect_lang_length}s starting at "
            f"{detect_lang_offset}s) - ID: {task_id}"
        )

        runtime.start_model()

        args = {"progress_callback": None}
        if encode:
            audio_bytes = runtime.extract_audio_segment_from_content(
                file_content,
                detect_lang_offset,
                detect_lang_length,
            )
            args["audio"] = audio_bytes
            args["input_sr"] = 16000
        else:
            args["audio"] = (
                runtime.np.frombuffer(file_content, runtime.np.int16)
                .flatten()
                .astype(runtime.np.float32)
                / 32768.0
            )
            args["input_sr"] = 16000

        args.update(runtime.kwargs)
        args["verbose"] = False

        result = _unsegmented_inference_with_recovery(
            runtime,
            lambda: runtime.transcribe_with_model(**args),
            "uploaded language detection",
        )
        detected_language = runtime.LanguageCode.from_string(result.language)
        language_code = detected_language.to_iso_639_1()

        runtime.logging.info(
            f"Detected language: {detected_language.to_name()} ({language_code}) - "
            f"ID: {task_id}"
        )

        if result_container:
            result_container.set_result(
                {
                    "detected_language": detected_language.to_name(),
                    "language_code": language_code,
                }
            )

    except Exception as exc:
        runtime.logging.error(
            f"Error detecting language (ID: {task_id}) for "
            f"'{task_data.get('video_file')}': {exc}"
            if task_data.get("video_file")
            else f"Error detecting language (ID: {task_id}): {exc}",
            exc_info=True,
        )
        if result_container:
            result_container.set_error(str(exc))

    finally:
        runtime.delete_model()


def extract_audio_segment_from_content(
    runtime,
    audio_content: bytes,
    start_time: int,
    duration: int,
) -> bytes:
    """Extract a WAV segment from in-memory audio, preserving fallback behavior."""
    try:
        runtime.logging.info(
            f"Extracting audio segment: start_time={start_time}s, duration={duration}s"
        )
        out, _ = (
            runtime.ffmpeg.input("pipe:0", ss=start_time, t=duration)
            .output("pipe:1", format="wav", acodec="pcm_s16le", ar=16000)
            .run(
                input=audio_content,
                capture_stdout=True,
                capture_stderr=True,
            )
        )
        if not out:
            raise ValueError("FFmpeg output is empty")
        return out
    except runtime.ffmpeg.Error as exc:
        runtime.logging.error(f"FFmpeg error: {exc.stderr.decode()}")
        return audio_content
    except Exception as exc:
        runtime.logging.error(f"Error extracting audio segment: {str(exc)}")
        return audio_content


def detect_language_task(runtime, path, original_task_data=None):
    """Detect a local file's language and return its follow-up transcription task."""
    media_validation = (original_task_data or {}).get("media_validation")
    _ensure_media_validation_current(runtime, path, media_validation)
    if (
        media_validation is not None
        and runtime.segmentation_enabled
        and media_validation.duration_seconds is None
    ):
        raise MediaDurationError("Validated media has no usable duration")
    detected_language = runtime.LanguageCode.NONE

    try:
        runtime.logging.info(
            f"Detecting language of file: {path} "
            f"({runtime.detect_language_length}s starting at "
            f"{runtime.detect_language_offset}s)"
        )

        _ensure_media_validation_current(runtime, path, media_validation)
        runtime.start_model()
        audio_track_index = (original_task_data or {}).get("audio_track_index")
        audio_segment = runtime.extract_audio_segment_to_memory(
            path,
            runtime.detect_language_offset,
            int(runtime.detect_language_length),
            track_index=audio_track_index,
        )
        _ensure_media_validation_current(runtime, path, media_validation)
        result = _unsegmented_inference_with_recovery(
            runtime,
            lambda: runtime.transcribe_with_model(audio_segment, verbose=False),
            "local language detection",
        )
        _ensure_media_validation_current(runtime, path, media_validation)
        detected_language = runtime.LanguageCode.from_string(result.language)

        runtime.logging.info(f"Detected language: {detected_language.to_name()}")

        selected_audio_language = (original_task_data or {}).get(
            "selected_audio_language"
        )
        if (
            runtime.notify_on_english_audio_mismatch
            and selected_audio_language == runtime.LanguageCode.ENGLISH
            and detected_language
            and detected_language != runtime.LanguageCode.ENGLISH
        ):
            selected_track = next(
                (
                    track
                    for track in (original_task_data or {}).get("audio_tracks", [])
                    if track.get("index") == audio_track_index
                ),
                {},
            )
            audio_summary = (
                f"index={audio_track_index}, language="
                f"{selected_audio_language.to_name()}, "
                f"title={selected_track.get('title', 'unknown')}"
            )
            runtime.logging.warning(
                "ENGLISH_AUDIO_MISMATCH | %s | detected=%s | audio=%s",
                path,
                detected_language.to_name(),
                audio_summary,
            )

    except runtime._media.MediaValidationStale:
        raise
    except Exception as exc:
        runtime.logging.error(
            f"Error detecting language for file: {exc}",
            exc_info=True,
        )

    finally:
        runtime.delete_model()

    task_data = {
        "path": path,
        "type": "transcribe",
        "transcribe_or_translate": runtime.transcribe_or_translate,
        "force_language": detected_language,
    }
    if original_task_data:
        for key, value in original_task_data.items():
            if key not in task_data:
                task_data[key] = value
    return task_data


def extract_audio_segment_to_memory(
    runtime,
    input_file,
    start_time,
    duration,
    track_index=None,
):
    """Extract one selected WAV segment from an upload or local media path."""
    try:
        if hasattr(input_file, "file") and hasattr(input_file.file, "read"):
            input_file.file.seek(0)
            input_stream = "pipe:0"
            input_kwargs = {"input": input_file.file.read()}
        elif isinstance(input_file, str):
            input_stream = input_file
            input_kwargs = {}
        else:
            raise ValueError(
                "Invalid input: input_file must be a file path or an UploadFile object."
            )

        runtime.logging.info(
            f"Extracting audio from: {input_stream}, start_time: {start_time}, "
            f"duration: {duration}"
        )
        output_kwargs = {
            "format": "wav",
            "acodec": "pcm_s16le",
            "ar": 16000,
            "ac": 1,
        }
        if track_index is not None:
            output_kwargs["map"] = f"0:{track_index}"

        out, _ = (
            runtime.ffmpeg.input(input_stream, ss=start_time, t=duration)
            .output("pipe:1", **output_kwargs)
            .run(
                capture_stdout=True,
                capture_stderr=True,
                **input_kwargs,
            )
        )
        if not out:
            raise ValueError("FFmpeg output is empty, possibly due to invalid input.")
        return out
    except runtime.ffmpeg.Error as exc:
        runtime.logging.error(f"FFmpeg error: {exc.stderr.decode()}")
        return None
    except Exception as exc:
        runtime.logging.error(f"Error: {str(exc)}")
        return None


def probe_media_duration(runtime, file_path):
    """Return one finite positive container duration from bounded FFprobe JSON."""
    try:
        completed = runtime.subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (runtime.subprocess.TimeoutExpired, OSError) as exc:
        raise MediaDurationError(
            "Unable to determine media duration within the bounded probe"
        ) from exc

    if completed.returncode != 0:
        raise MediaDurationError("FFprobe could not determine media duration")

    try:
        payload = runtime.json.loads(completed.stdout)
        duration = float(payload["format"]["duration"])
    except (
        KeyError,
        TypeError,
        ValueError,
        runtime.json.JSONDecodeError,
    ) as exc:
        raise MediaDurationError("FFprobe returned no usable media duration") from exc
    if not runtime.math.isfinite(duration) or duration <= 0:
        raise MediaDurationError("FFprobe returned no usable media duration")
    return duration


def _render_lrc(result):
    lines = []
    for segment in result.segments:
        minutes, seconds = divmod(int(segment.start), 60)
        fraction = int((segment.start - int(segment.start)) * 100)
        text = segment.text[:].replace("\n", "")
        lines.append(f"[{minutes:02d}:{seconds:02d}.{fraction:02d}]{text}\n")
    return "".join(lines)


def write_lrc(runtime, result, file_path):
    """Write timestamped lyrics while removing embedded text newlines."""
    opener = getattr(runtime, "open", open)
    with opener(file_path, "w") as file:
        file.write(_render_lrc(result))


def send_completion_webhook(
    runtime,
    source_file_path: str,
    subtitle_file_path: str,
    language,
    task_type: str,
):
    """Post the completion payload when a downstream webhook is configured."""
    if not runtime.webhook_url_completed:
        return

    event_status = (
        f"{task_type}d" if task_type in ["transcribe", "translate"] else task_type
    )
    payload = {
        "event": event_status,
        "file": runtime.os.path.abspath(source_file_path),
        "subtitle": runtime.os.path.abspath(subtitle_file_path),
        "language": language.to_iso_639_1(),
    }

    try:
        runtime.logging.info(
            f"Sending completion webhook ({event_status}) to "
            f"{runtime.webhook_url_completed}"
        )
        response = runtime.requests.post(
            runtime.webhook_url_completed,
            json=payload,
            timeout=10,
        )
        response.raise_for_status()
        runtime.logging.debug(
            f"Webhook successfully delivered. Status code: {response.status_code}"
        )
    except Exception as exc:
        runtime.logging.error(f"Failed to send completion webhook: {exc}")


def _transcription_arguments(runtime, progress_callback):
    args = {"progress_callback": progress_callback}
    if runtime.custom_regroup and runtime.custom_regroup.lower() != "default":
        args["regroup"] = runtime.custom_regroup
    args.update(runtime.kwargs)
    return args


def _is_inference_allocation_control(runtime, error):
    model_runtime = getattr(runtime, "_model_runtime", None)
    failure_type = getattr(
        model_runtime,
        "ModelInferenceAllocationFailure",
        None,
    )
    return isinstance(failure_type, type) and isinstance(error, failure_type)


def _is_inference_control(runtime, error):
    resources = getattr(runtime, "_resource_management", None)
    pressure_type = getattr(resources, "MemoryPressureYield", None)
    return (
        isinstance(pressure_type, type) and isinstance(error, pressure_type)
    ) or _is_inference_allocation_control(runtime, error)


def _ensure_media_validation_current(runtime, file_path, media_validation):
    if media_validation is not None and not runtime.is_media_validation_current(
        file_path,
        media_validation,
    ):
        raise runtime._media.MediaValidationStale(
            "Media generation changed after admission"
        )


def _whole_transcription_attempt(
    runtime,
    file_path,
    transcription_type,
    force_language,
    audio_tracks,
    audio_track_index,
    progress_callback,
    media_validation=None,
):
    """Run one whole-file attempt and return control errors without payload refs."""
    data = file_path
    extracted_audio = None
    control_error = None
    try:
        _ensure_media_validation_current(runtime, file_path, media_validation)
        extracted_audio = runtime.handle_multiple_audio_tracks(
            file_path,
            force_language,
            audio_tracks=audio_tracks,
            audio_track_index=audio_track_index,
        )
        if extracted_audio:
            data = extracted_audio
        _ensure_media_validation_current(runtime, file_path, media_validation)
        try:
            result = runtime.transcribe_with_model(
                data,
                language=force_language.to_iso_639_1(),
                task=transcription_type,
                verbose=None,
                **_transcription_arguments(runtime, progress_callback),
            )
            _ensure_media_validation_current(runtime, file_path, media_validation)
        except Exception as exc:
            if not _is_inference_control(runtime, exc):
                raise
            control_error = exc.with_traceback(None)
            control_error.__context__ = None
            control_error.__cause__ = None
            result = None
    finally:
        data = None
        extracted_audio = None
    return result, control_error


def _wait_for_inference_recovery(runtime):
    controller = getattr(runtime, "model_pressure_controller", None)
    before = getattr(controller, "external_pressure_recovery_generation", 0)
    if type(before) is not int or before < 0:
        before = 0
    runtime.check_model_runtime_cancelled()
    if runtime.wait_for_model_recovery():
        runtime.check_model_runtime_cancelled()
        after = getattr(controller, "external_pressure_recovery_generation", 0)
        if type(after) is not int or after < before:
            after = before
        return before, after
    runtime.check_model_runtime_cancelled()
    raise RuntimeError("Model recovery ended without reopening inference admission")


def _release_and_wait(runtime, error):
    runtime.release_after_inference_failure(error)
    _wait_for_inference_recovery(runtime)


def _unsegmented_inference_with_recovery(runtime, infer, context):
    """Retry non-local inference without segmentation.

    The caller-owned upload/detection buffer intentionally remains resident;
    this helper consumes the model release ticket but cannot shrink that input.
    """
    warned = False
    while True:
        control_error = None
        try:
            return infer()
        except Exception as exc:
            if not _is_inference_control(runtime, exc):
                raise
            control_error = exc.with_traceback(None)
            control_error.__context__ = None
            control_error.__cause__ = None

        _release_and_wait(runtime, control_error)
        if _is_inference_allocation_control(runtime, control_error):
            raise control_error.with_traceback(None)
        if not warned:
            runtime.logging.warning(
                "%s remains whole-file and is retrying after memory pressure",
                context,
            )
            warned = True


def _selected_audio_track_index(
    runtime,
    file_path,
    force_language,
    audio_tracks,
    audio_track_index,
):
    tracks = audio_tracks
    if tracks is None:
        tracks = runtime.get_audio_tracks(file_path)
    tracks = tuple(tracks or ())
    if not tracks:
        return None
    selected = next(
        (track for track in tracks if track.get("index") == audio_track_index),
        None,
    )
    if force_language is not None:
        selected = selected or runtime.get_audio_track_by_language(
            tracks,
            force_language,
        )
    if selected is None:
        selected = tracks[0]
    return selected.get("index")


def _controller_is_healthy(runtime):
    controller = getattr(runtime, "model_pressure_controller", None)
    if controller is None:
        return True
    normal = getattr(controller, "NORMAL", "normal")
    return bool(
        getattr(controller, "state", normal) == normal
        and getattr(controller, "admission_open", True)
        and not getattr(runtime, "model_admission_closed", False)
    )


def _segmented_transcription(
    runtime,
    file_path,
    transcription_type,
    force_language,
    audio_tracks,
    audio_track_index,
    media_duration,
    adaptive,
    progress_callback,
    media_validation=None,
    workload_token=None,
):
    track_index = _selected_audio_track_index(
        runtime,
        file_path,
        force_language,
        audio_tracks,
        audio_track_index,
    )
    runtime.logging.info(
        "Using adaptive segmented transcription for %s (%.1fs, %ss baseline)",
        runtime.os.path.basename(file_path),
        media_duration,
        adaptive.baseline_seconds,
    )

    def extract_chunk(window):
        _ensure_media_validation_current(runtime, file_path, media_validation)
        audio = runtime.extract_audio_segment_to_memory(
            file_path,
            window.extract_start,
            window.extract_duration,
            track_index=track_index,
        )
        if audio is None:
            raise AudioSegmentExtractionError(
                f"Failed to extract selected audio interval {window.ordinal}"
            )
        _ensure_media_validation_current(runtime, file_path, media_validation)
        return audio

    def transcribe_chunk(audio, _window, mapped_progress):
        _ensure_media_validation_current(runtime, file_path, media_validation)
        result = runtime.transcribe_with_model(
            audio,
            language=force_language.to_iso_639_1(),
            task=transcription_type,
            verbose=None,
            **_transcription_arguments(runtime, mapped_progress),
        )
        _ensure_media_validation_current(runtime, file_path, media_validation)
        return result

    result_factory = getattr(runtime.stable_whisper, "WhisperResult", None)
    if not callable(result_factory):
        raise RuntimeError("stable-ts WhisperResult construction is unavailable")

    result = _segmentation.run_segmented_transcription(
        media_duration=media_duration,
        adaptive=adaptive,
        extract_chunk=extract_chunk,
        transcribe_chunk=transcribe_chunk,
        release_failure=lambda error, _window: runtime.release_after_inference_failure(
            error
        ),
        wait_for_recovery=lambda _error, _window: _wait_for_inference_recovery(runtime),
        result_factory=result_factory,
        segment_factory=runtime.Segment,
        check_cancelled=runtime.check_model_runtime_cancelled,
        healthy_after_chunk=lambda: _controller_is_healthy(runtime),
        is_allocation_failure=lambda error: _is_inference_allocation_control(
            runtime, error
        ),
        progress_callback=progress_callback,
        chunk_started=lambda window: _record_workload_chunk(
            runtime,
            workload_token,
            cursor_ms=_cursor_ms(window.core_start),
            chunk_uncommitted=True,
        ),
        chunk_unwound=lambda window: _record_workload_chunk(
            runtime,
            workload_token,
            cursor_ms=_cursor_ms(window.core_start),
            chunk_uncommitted=False,
        ),
        chunk_committed=lambda _window, state: _record_workload_chunk(
            runtime,
            workload_token,
            cursor_ms=_cursor_ms(state.cursor),
            chunk_uncommitted=False,
        ),
    )
    _ensure_media_validation_current(runtime, file_path, media_validation)
    return result


def _fsync_parent_directory(runtime, file_path):
    gate_required = bool(
        getattr(
            getattr(runtime, "runtime_receipt_coordinator", None),
            "gate_enabled",
            False,
        )
    )
    directory_flag = getattr(runtime.os, "O_DIRECTORY", None)
    if directory_flag is None:
        if gate_required:
            raise OSError("Task 11B requires durable subtitle directory sync")
        return
    directory = runtime.os.path.dirname(file_path) or "."
    try:
        descriptor = runtime.os.open(
            directory,
            runtime.os.O_RDONLY | directory_flag,
        )
        try:
            runtime.os.fsync(descriptor)
        finally:
            runtime.os.close(descriptor)
    except OSError as exc:
        runtime.logging.warning(
            "Subtitle directory sync unavailable after atomic publish (%s, errno=%s)",
            type(exc).__name__,
            exc.errno,
        )
        if gate_required:
            raise


def _gate_output_media_path(runtime, file_path):
    config = getattr(runtime, "task11b_gate_config", None)
    if config is None:
        return file_path
    return config.map_output_media_path(file_path, filesystem=runtime.os)


def _validate_gate_output_artifact(runtime, file_path):
    config = getattr(runtime, "task11b_gate_config", None)
    if config is not None:
        config.validate_output_artifact_path(file_path, filesystem=runtime.os)


def _gate_output_enabled(runtime):
    config = getattr(runtime, "task11b_gate_config", None)
    return bool(getattr(config, "enabled", False))


def _gate_staging_identity(runtime, descriptor, temporary_path):
    """Pin one fresh regular staging inode before gate publication."""

    try:
        opened = runtime.os.fstat(descriptor)
        named = runtime.os.lstat(temporary_path)
    except (AttributeError, OSError) as exc:
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle staging inode was unavailable"
        ) from exc
    identity = (opened.st_dev, opened.st_ino)
    if (
        not _stat.S_ISREG(opened.st_mode)
        or not _stat.S_ISREG(named.st_mode)
        or _stat.S_ISLNK(named.st_mode)
        or (named.st_dev, named.st_ino) != identity
        or opened.st_nlink != 1
        or named.st_nlink != 1
    ):
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle staging inode changed or was unsafe"
        )
    return identity


def _verify_gate_link_identity(runtime, descriptor, path, identity, *, links):
    """Require a named gate artifact to remain the pinned staging inode."""

    try:
        opened = runtime.os.fstat(descriptor)
        named = runtime.os.lstat(path)
    except (AttributeError, OSError) as exc:
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle artifact identity was unavailable"
        ) from exc
    if (
        not _stat.S_ISREG(opened.st_mode)
        or not _stat.S_ISREG(named.st_mode)
        or _stat.S_ISLNK(named.st_mode)
        or (opened.st_dev, opened.st_ino) != identity
        or (named.st_dev, named.st_ino) != identity
        or opened.st_nlink != links
        or named.st_nlink != links
    ):
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle artifact no longer matched its staging inode"
        )


def _publish_gate_staging(runtime, descriptor, temporary_path, file_path):
    """Fsync and atomically install one create-once Task 11B artifact."""

    identity = _gate_staging_identity(runtime, descriptor, temporary_path)
    try:
        runtime.os.fchmod(descriptor, 0o644)
        runtime.os.fsync(descriptor)
        runtime.os.lseek(descriptor, 0, runtime.os.SEEK_SET)
        with runtime.os.fdopen(
            runtime.os.dup(descriptor),
            "r",
            encoding="utf-8",
            newline="",
        ) as staged:
            payload = staged.read()
    except (AttributeError, OSError, UnicodeError) as exc:
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle staging file could not be durably read"
        ) from exc

    _verify_gate_link_identity(
        runtime,
        descriptor,
        temporary_path,
        identity,
        links=1,
    )
    _validate_gate_output_artifact(runtime, file_path)
    try:
        runtime.os.link(
            temporary_path,
            file_path,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle artifact appeared before no-replace publication"
        ) from exc
    except (AttributeError, OSError, TypeError) as exc:
        raise _runtime_receipts.RuntimeReceiptError(
            "Task 11B subtitle artifact could not be installed without replacement"
        ) from exc

    _verify_gate_link_identity(
        runtime,
        descriptor,
        file_path,
        identity,
        links=2,
    )
    runtime.os.unlink(temporary_path)
    _verify_gate_link_identity(
        runtime,
        descriptor,
        file_path,
        identity,
        links=1,
    )
    _fsync_parent_directory(runtime, file_path)
    return payload


def _atomic_publish(runtime, file_path, write_temporary):
    if not callable(write_temporary):
        raise TypeError("Atomic subtitle writer must be callable")
    _validate_gate_output_artifact(runtime, file_path)
    directory = runtime.os.path.dirname(file_path) or "."
    prefix = f".{runtime.os.path.basename(file_path)}."
    output_extension = runtime.os.path.splitext(file_path)[1]
    staging_suffix = f".tmp{output_extension}" if output_extension else ".tmp"
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=prefix,
        suffix=staging_suffix,
        dir=directory,
    )
    gate_output = _gate_output_enabled(runtime)
    try:
        if gate_output:
            write_temporary(temporary_path)
            return _publish_gate_staging(
                runtime,
                descriptor,
                temporary_path,
                file_path,
            )

        runtime.os.close(descriptor)
        descriptor = None
        write_temporary(temporary_path)
        try:
            published_mode = runtime.os.stat(file_path).st_mode & 0o777
        except OSError:
            published_mode = 0o644
        runtime.os.chmod(temporary_path, published_mode)
        staged_descriptor = runtime.os.open(temporary_path, runtime.os.O_RDWR)
        with runtime.os.fdopen(
            staged_descriptor,
            "r",
            encoding="utf-8",
            newline="",
        ) as staged:
            payload = staged.read()
            runtime.os.fsync(staged.fileno())
        _validate_gate_output_artifact(runtime, file_path)
        runtime.os.replace(temporary_path, file_path)
        _fsync_parent_directory(runtime, file_path)
        return payload
    except BaseException:
        try:
            runtime.os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            runtime.logging.warning(
                "Could not remove failed subtitle staging file (%s)",
                type(cleanup_error).__name__,
            )
        raise
    finally:
        if descriptor is not None:
            runtime.os.close(descriptor)


def _task_result_is_waiting(runtime, file_path):
    with runtime.task_results_lock:
        return file_path in runtime.task_results


def _publish_segmented_result(
    runtime,
    result,
    file_path,
    file_name,
    transcription_type,
    output_language,
    is_audio_file,
):
    task_waiting = _task_result_is_waiting(runtime, file_path)
    if is_audio_file and runtime.lrc_for_audio_files:
        subtitle_file_path = file_name + ".lrc"
        task_payload = (
            result.to_srt_vtt(
                filepath=None,
                word_level=runtime.word_level_highlight,
            )
            if task_waiting
            else None
        )
        _atomic_publish(
            runtime,
            subtitle_file_path,
            lambda temporary_path: runtime.write_lrc(result, temporary_path),
        )
    else:
        subtitle_file_path = runtime.name_subtitle(file_path, output_language)
        task_payload = _atomic_publish(
            runtime,
            subtitle_file_path,
            lambda temporary_path: result.to_srt_vtt(
                temporary_path,
                word_level=runtime.word_level_highlight,
            ),
        )

    runtime.send_completion_webhook(
        file_path,
        subtitle_file_path,
        output_language,
        transcription_type,
    )
    if task_waiting:
        with runtime.task_results_lock:
            if file_path in runtime.task_results:
                runtime.task_results[file_path].set_result(task_payload)


def _publish_legacy_result(
    runtime,
    result,
    file_path,
    file_name,
    transcription_type,
    output_language,
    is_audio_file,
):
    """Preserve the opt-out inference path while publishing it durably."""

    return _publish_segmented_result(
        runtime,
        result,
        file_path,
        file_name,
        transcription_type,
        output_language,
        is_audio_file,
    )


def gen_subtitles(
    runtime,
    file_path: str,
    transcription_type: str,
    force_language,
    audio_tracks=None,
    audio_track_index=None,
    media_validation=None,
) -> None:
    """Transcribe one selected audio track and write its subtitle output."""
    _ensure_media_validation_current(runtime, file_path, media_validation)
    workload_token = None
    terminal_cursor_ms = 0
    try:
        _, file_extension = runtime.os.path.splitext(file_path)
        output_media_path = _gate_output_media_path(runtime, file_path)
        file_name = runtime.os.path.splitext(output_media_path)[0]
        is_audio_file = runtime.is_audio_file_extension(file_extension)
        display_name = runtime.os.path.basename(file_path)
        progress_callback = runtime.ProgressHandler(display_name)

        if runtime.segmentation_enabled:
            if media_validation is None:
                media_duration = runtime.probe_media_duration(file_path)
            else:
                media_duration = media_validation.duration_seconds
                if media_duration is None:
                    raise MediaDurationError("Validated media has no usable duration")
            terminal_cursor_ms = _duration_ms(media_duration)

        _ensure_media_validation_current(runtime, file_path, media_validation)
        workload_sha256 = _gate_workload_sha256(
            runtime,
            file_path,
            transcription_type,
            force_language,
            terminal_cursor_ms,
        )
        if bool(
            getattr(
                getattr(runtime, "runtime_receipt_coordinator", None),
                "gate_enabled",
                False,
            )
        ):
            _ensure_media_validation_current(runtime, file_path, media_validation)
        workload_token = _begin_workload(runtime, workload_sha256)
        runtime.start_model()

        if runtime.segmentation_enabled:
            baseline_seconds = runtime.model_chunk_baseline_seconds
            if (
                isinstance(baseline_seconds, bool)
                or not isinstance(baseline_seconds, int)
                or baseline_seconds < 5 * 60
            ):
                raise RuntimeError(
                    "Model runtime did not publish a valid segmentation baseline"
                )
            adaptive = runtime._resource_management.AdaptiveChunkState(baseline_seconds)
            if media_duration > adaptive.current_seconds:
                result = _segmented_transcription(
                    runtime,
                    file_path,
                    transcription_type,
                    force_language,
                    audio_tracks,
                    audio_track_index,
                    media_duration,
                    adaptive,
                    progress_callback,
                    media_validation,
                    workload_token,
                )
            else:
                _record_workload_chunk(
                    runtime,
                    workload_token,
                    cursor_ms=0,
                    chunk_uncommitted=True,
                )
                try:
                    result, control_error = _whole_transcription_attempt(
                        runtime,
                        file_path,
                        transcription_type,
                        force_language,
                        audio_tracks,
                        audio_track_index,
                        progress_callback,
                        media_validation,
                    )
                except BaseException:
                    _record_workload_chunk(
                        runtime,
                        workload_token,
                        cursor_ms=0,
                        chunk_uncommitted=False,
                    )
                    raise
                _record_workload_chunk(
                    runtime,
                    workload_token,
                    cursor_ms=terminal_cursor_ms if control_error is None else 0,
                    chunk_uncommitted=False,
                )
                if control_error is not None:
                    runtime.release_after_inference_failure(control_error)
                    runtime.check_model_runtime_cancelled()
                    if _is_inference_allocation_control(runtime, control_error):
                        exhausted = adaptive.record_allocation_failure()
                    else:
                        adaptive.record_pressure_yield()
                        exhausted = False
                    recovery_window = _wait_for_inference_recovery(runtime)
                    if _is_inference_allocation_control(
                        runtime, control_error
                    ) and _segmentation._external_pressure_recovered(recovery_window):
                        adaptive.record_external_pressure_recovery()
                        exhausted = False
                    if exhausted:
                        raise control_error.with_traceback(None)
                    result = _segmented_transcription(
                        runtime,
                        file_path,
                        transcription_type,
                        force_language,
                        audio_tracks,
                        audio_track_index,
                        media_duration,
                        adaptive,
                        progress_callback,
                        media_validation,
                        workload_token,
                    )
        else:
            warned_about_whole_retry = False
            while True:
                _record_workload_chunk(
                    runtime,
                    workload_token,
                    cursor_ms=0,
                    chunk_uncommitted=True,
                )
                try:
                    result, control_error = _whole_transcription_attempt(
                        runtime,
                        file_path,
                        transcription_type,
                        force_language,
                        audio_tracks,
                        audio_track_index,
                        progress_callback,
                        media_validation,
                    )
                except BaseException:
                    _record_workload_chunk(
                        runtime,
                        workload_token,
                        cursor_ms=0,
                        chunk_uncommitted=False,
                    )
                    raise
                _record_workload_chunk(
                    runtime,
                    workload_token,
                    cursor_ms=0,
                    chunk_uncommitted=False,
                )
                if control_error is None:
                    break
                if not _is_inference_allocation_control(runtime, control_error):
                    if not warned_about_whole_retry:
                        runtime.logging.warning(
                            "SEGMENTATION_ENABLED=False: retrying the whole file "
                            "after memory pressure; peak memory cannot be reduced"
                        )
                        warned_about_whole_retry = True
                    _release_and_wait(runtime, control_error)
                    continue
                _release_and_wait(runtime, control_error)
                raise control_error.with_traceback(None)

        _ensure_media_validation_current(runtime, file_path, media_validation)
        runtime.appendLine(result)
        output_language = runtime.LanguageCode.from_string(result.language)
        _ensure_media_validation_current(runtime, file_path, media_validation)
        if runtime.segmentation_enabled:
            _publish_segmented_result(
                runtime,
                result,
                file_path,
                file_name,
                transcription_type,
                output_language,
                is_audio_file,
            )
        else:
            _publish_legacy_result(
                runtime,
                result,
                file_path,
                file_name,
                transcription_type,
                output_language,
                is_audio_file,
            )
        _complete_workload(runtime, workload_token, terminal_cursor_ms)
        workload_token = None

    except Exception as exc:
        if workload_token is not None:
            _abort_workload(runtime, workload_token)
            workload_token = None
        model_runtime_owner = getattr(runtime, "_model_runtime", None)
        resource_owner = getattr(runtime, "_resource_management", None)
        runtime_error_types = tuple(
            error_type
            for error_type in (
                getattr(model_runtime_owner, "ModelLoadProfileUnhealthy", None),
                getattr(model_runtime_owner, "ModelReleaseError", None),
                getattr(model_runtime_owner, "ModelRuntimeCancelled", None),
                getattr(resource_owner, "MemoryPressureYield", None),
            )
            if isinstance(error_type, type)
        )
        if isinstance(exc, runtime_error_types):
            runtime.logging.error("Model runtime unavailable: %s", exc)
        else:
            runtime.logging.error(
                f"Error processing or transcribing {file_path} in "
                f"{force_language}: {exc}",
                exc_info=True,
            )
        with runtime.task_results_lock:
            if file_path in runtime.task_results:
                runtime.task_results[file_path].set_error(str(exc))
        raise

    finally:
        runtime.delete_model()


def handle_multiple_audio_tracks(
    runtime,
    file_path: str,
    language=None,
    audio_tracks=None,
    audio_track_index=None,
):
    """Extract the preselected stream when a file has multiple audio tracks."""
    audio_bytes = None
    if audio_tracks is None:
        audio_tracks = runtime.get_audio_tracks(file_path)

    if len(audio_tracks) > 1:
        runtime.logging.debug(
            f"Handling multiple audio tracks from {file_path} and planning to "
            f"extract audio track of language {language}"
        )
        runtime.logging.debug(
            "Audio tracks:\n"
            + "\n".join(
                [
                    f"  - {track['index']}: {track['codec']} {track['language']} "
                    f"{('default' if track['default'] else '')}"
                    for track in audio_tracks
                ]
            )
        )

        audio_track = next(
            (
                track
                for track in audio_tracks
                if track.get("index") == audio_track_index
            ),
            None,
        )
        if audio_track is None and language is not None:
            audio_track = runtime.get_audio_track_by_language(audio_tracks, language)
        if audio_track is None:
            audio_track = audio_tracks[0]

        audio_bytes = runtime.extract_audio_track_to_memory(
            file_path,
            audio_track["index"],
        )
        if audio_bytes is None:
            runtime.logging.error(
                f"Failed to extract audio track {audio_track['index']} from {file_path}"
            )
            return None
    return audio_bytes


def extract_audio_track_to_memory(runtime, input_video_path, track_index):
    """Extract one audio stream to in-memory mono 16 kHz WAV bytes."""
    if track_index is None:
        runtime.logging.warning(
            f"Skipping audio track extraction for {input_video_path} because track "
            "index is None"
        )
        return None

    try:
        out, _ = (
            runtime.ffmpeg.input(input_video_path)
            .output(
                "pipe:",
                map=f"0:{track_index}",
                format="wav",
                ac=1,
                ar=16000,
                loglevel="quiet",
            )
            .run(capture_stdout=True, capture_stderr=True)
        )
        return out
    except runtime.ffmpeg.Error as exc:
        runtime.logging.error(f"FFmpeg error: {exc.stderr.decode()}")
        return None
