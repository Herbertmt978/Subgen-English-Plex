"""Audio extraction, language detection, ASR, and subtitle output algorithms."""


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
        result = runtime.transcribe_with_model(
            task=task,
            language=language,
            **args,
            verbose=None,
        )

        if audio_offset > 0:
            runtime.apply_timestamp_offset(result, audio_offset)

        runtime.appendLine(result)

        if result_container:
            output_format = task_data.get("output_format") or task_data.get(
                "output", "srt"
            )
            word_level = task_data.get(
                "word_timestamps", runtime.word_level_highlight
            )
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

        result = runtime.transcribe_with_model(**args)
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
    detected_language = runtime.LanguageCode.NONE

    try:
        runtime.logging.info(
            f"Detecting language of file: {path} "
            f"({runtime.detect_language_length}s starting at "
            f"{runtime.detect_language_offset}s)"
        )

        runtime.start_model()
        audio_track_index = (original_task_data or {}).get("audio_track_index")
        audio_segment = runtime.extract_audio_segment_to_memory(
            path,
            runtime.detect_language_offset,
            int(runtime.detect_language_length),
            track_index=audio_track_index,
        )
        result = runtime.transcribe_with_model(audio_segment, verbose=False)
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


def write_lrc(runtime, result, file_path):
    """Write timestamped lyrics while removing embedded text newlines."""
    opener = getattr(runtime, "open", open)
    with opener(file_path, "w") as file:
        for segment in result.segments:
            minutes, seconds = divmod(int(segment.start), 60)
            fraction = int((segment.start - int(segment.start)) * 100)
            text = segment.text[:].replace("\n", "")
            file.write(f"[{minutes:02d}:{seconds:02d}.{fraction:02d}]{text}\n")


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

    event_status = f"{task_type}d" if task_type in ["transcribe", "translate"] else task_type
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


def gen_subtitles(
    runtime,
    file_path: str,
    transcription_type: str,
    force_language,
    audio_tracks=None,
    audio_track_index=None,
) -> None:
    """Transcribe one selected audio track and write its subtitle output."""
    try:
        runtime.start_model()

        file_name, file_extension = runtime.os.path.splitext(file_path)
        is_audio_file = runtime.is_audio_file_extension(file_extension)

        data = file_path
        extracted_audio_file = runtime.handle_multiple_audio_tracks(
            file_path,
            force_language,
            audio_tracks=audio_tracks,
            audio_track_index=audio_track_index,
        )
        if extracted_audio_file:
            data = extracted_audio_file

        args = {}
        display_name = runtime.os.path.basename(file_path)
        args["progress_callback"] = runtime.ProgressHandler(display_name)

        if runtime.custom_regroup and runtime.custom_regroup.lower() != "default":
            args["regroup"] = runtime.custom_regroup
        args.update(runtime.kwargs)

        result = runtime.transcribe_with_model(
            data,
            language=force_language.to_iso_639_1(),
            task=transcription_type,
            verbose=None,
            **args,
        )

        runtime.appendLine(result)
        output_language = runtime.LanguageCode.from_string(result.language)

        if is_audio_file and runtime.lrc_for_audio_files:
            subtitle_file_path = file_name + ".lrc"
            runtime.write_lrc(result, subtitle_file_path)
        else:
            subtitle_file_path = runtime.name_subtitle(file_path, output_language)
            result.to_srt_vtt(
                subtitle_file_path,
                word_level=runtime.word_level_highlight,
            )

        runtime.send_completion_webhook(
            file_path,
            subtitle_file_path,
            output_language,
            transcription_type,
        )

        with runtime.task_results_lock:
            if file_path in runtime.task_results:
                runtime.task_results[file_path].set_result(
                    result.to_srt_vtt(
                        filepath=None,
                        word_level=runtime.word_level_highlight,
                    )
                )

    except Exception as exc:
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
