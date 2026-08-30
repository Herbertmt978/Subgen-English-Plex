"""Media probing, track selection, subtitle policy, and path helpers."""

from typing import List

from language_code import LanguageCode


VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mpg", ".mpeg",
    ".3gp", ".ogv", ".vob", ".rm", ".rmvb", ".ts", ".m4v", ".f4v", ".svq3",
    ".asf", ".m2ts", ".divx", ".xvid",
)

AUDIO_EXTENSIONS = (
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma", ".alac", ".m4a", ".opus",
    ".aiff", ".aif", ".pcm", ".ra", ".ram", ".mid", ".midi", ".ape", ".wv",
    ".amr", ".vox", ".tak", ".spx", ".m4b", ".mka",
)


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
    subgen_part = ".subgen" if runtime.show_in_subname_subgen else ""
    model_part = f".{runtime.whisper_model}" if runtime.show_in_subname_model else ""
    lang_part = runtime.define_subtitle_language_naming(
        language,
        runtime.subtitle_language_naming_type,
    )
    return (
        f"{runtime.os.path.splitext(file_path)[0]}"
        f"{subgen_part}{model_part}.{lang_part}.srt"
    )


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

    default_track = next((track for track in audio_tracks if track.get("default")), None)
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

    if not runtime.has_audio(file_path):
        runtime.logging.debug(f"{file_path} doesn't have any audio to transcribe!")
        return

    audio_tracks = runtime.get_audio_tracks(file_path)
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
        selected_track.get("language", force_language) if selected_track else force_language
    )

    if runtime.should_skip_file(file_path, force_language, audio_langs=audio_langs):
        return

    if (
        runtime.should_whisper_detect_audio_language
        and not explicitly_forced_language
        and not runtime.force_detected_language_to
    ):
        detect_task = {
            "path": file_path,
            "type": "detect_language",
            "audio_tracks": audio_tracks,
            "selected_audio_language": selected_audio_language,
            "audio_track_index": selected_track_index,
        }
        detect_task.update(task_kwargs)
        runtime.task_queue.put(detect_task)
        return

    task = {
        "path": file_path,
        "transcribe_or_translate": transcription_type,
        "force_language": force_language,
        "audio_track_index": selected_track_index,
        "audio_tracks": audio_tracks,
    }
    task.update(task_kwargs)
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
            preferred_names = [lang.to_name() for lang in runtime.preferred_audio_languages]
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
                lang_tag = stream.metadata.get("language", "") if stream.metadata else ""
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
        ".srt", ".vtt", ".sub", ".ass", ".ssa", ".idx",
        ".sbv", ".pgs", ".ttml", ".lrc",
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

            subtitle_parts = subtitle_name[len(video_name):].lstrip(".").split(".")
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
    """Return whether a supported media file has a usable audio stream."""
    try:
        if not runtime.is_valid_path(file_path):
            return False
        if not (
            runtime.has_video_extension(file_path)
            or runtime.has_audio_extension(file_path)
        ):
            return False

        with runtime.av.open(file_path) as container:
            for stream in container.streams:
                if stream.type == "audio":
                    if stream.codec_context and stream.codec_context.name != "none":
                        return True
                    runtime.logging.debug(
                        f"Unsupported or missing codec for audio stream in {file_path}"
                    )
            return False
    except (runtime.av.FFmpegError, UnicodeDecodeError) as exc:
        runtime.emit_subgen_event(
            "file_error",
            {"path": file_path, "type": "probe"},
            exc,
        )
        runtime.logging.warning(f"Unable to inspect media file {file_path}")
        return False


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
