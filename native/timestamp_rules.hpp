#pragma once
#include <cstddef>
#include <cstdint>

// Match OpenAI Whisper's ApplyTimestampRules: an opening timestamp may equal
// the previous closing timestamp, but a closing timestamp must advance. This
// constrains decoding; it never changes or deletes an already generated cue.
template<class Token>
inline int64_t native_timestamp_floor(const Token * tokens, size_t count,
                                      int timestamp_begin) noexcept {
    for (size_t i = count; i > 0; --i) {
        if (tokens[i - 1].id < timestamp_begin) continue;
        const bool last_timestamp = tokens[count - 1].id >= timestamp_begin;
        const bool penultimate_timestamp = count < 2 || tokens[count - 2].id >= timestamp_begin;
        return static_cast<int64_t>(tokens[i - 1].id)
            + (last_timestamp && !penultimate_timestamp ? 0 : 1);
    }
    return timestamp_begin;
}
