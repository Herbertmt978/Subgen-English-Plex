#pragma once
#include <cstddef>
#include <cstdint>
#include <stdexcept>

// This is exact digital silence, not a loudness/VAD threshold. Even the
// smallest nonzero sample must still reach the speech recognizer.
inline bool native_digital_silence(const float * samples, size_t count) {
    if (!samples || count == 0) return false;
    for (size_t i = 0; i < count; ++i)
        if (samples[i] != 0.0f) return false;
    return true;
}

// whisper.cpp predicts timestamps over a padded decoder window. Only the final
// cue's right edge may be intersected with the actual PCM input. This is not a
// general timestamp repair: never move a start, merge cues, or discard speech.
inline int64_t native_audio_end_ms(int64_t start, int64_t end, int64_t previous_end,
                                 int64_t audio_end, bool final_segment,
                                 int64_t decoder_window_ms) {
    if (audio_end <= 0 || audio_end > 1810000 || decoder_window_ms <= 0 ||
        decoder_window_ms > 30000 || previous_end < 0 || start < previous_end ||
        start >= audio_end || end <= start) {
        throw std::runtime_error("invalid_segment_timing");
    }
    if (end <= audio_end) return end;
    if (!final_segment || end - audio_end > decoder_window_ms) {
        throw std::runtime_error("invalid_segment_timing");
    }
    return audio_end;
}
