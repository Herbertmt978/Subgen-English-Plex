#include "audio_bounds.hpp"
#include <cassert>
#include <iostream>
#include <limits>

static void rejects(int64_t start, int64_t end, int64_t previous, int64_t audio,
                    bool final = true) {
    bool rejected = false;
    try { native_audio_end_ms(start, end, previous, audio, final, 30000); }
    catch (const std::runtime_error &) { rejected = true; }
    assert(rejected);
}

int main() {
    // Captured physical NVIDIA regression; preserve start and spoken text at caller.
    assert(native_audio_end_ms(304780, 311420, 304780, 310000, true, 30000) == 310000);
    assert(native_audio_end_ms(100, 500, 0, 1000, false, 30000) == 500);
    assert(native_audio_end_ms(900, 1000, 500, 1000, true, 30000) == 1000);
    assert(native_audio_end_ms(999, 31000, 500, 1000, true, 30000) == 1000);
    rejects(999, 31001, 500, 1000); // Not an arbitrary corrupted endpoint.
    rejects(900, 1001, 500, 1000, false);
    rejects(1000, 1100, 500, 1000);
    rejects(1100, 1200, 500, 1000);
    rejects(400, 600, 500, 1000);
    rejects(900, 800, 500, 1000);
    rejects(900, 900, 500, 1000);
    rejects(-1, 100, 0, 1000);
    rejects(0, 100, -1, 1000);
    rejects(0, 100, 0, 0);
    rejects(0, 100, 0, 1810001);
    const float zeros[] = {0.0f, -0.0f, 0.0f};
    const float quiet[] = {0.0f, 1.0f / 32768.0f, 0.0f};
    const float tiny[] = {0.0f, std::numeric_limits<float>::denorm_min()};
    const float invalid[] = {0.0f, std::numeric_limits<float>::quiet_NaN()};
    assert(native_digital_silence(zeros, 3));
    assert(!native_digital_silence(zeros, 0));
    assert(!native_digital_silence(nullptr, 1));
    assert(!native_digital_silence(quiet, 3));
    assert(!native_digital_silence(tiny, 2));
    assert(!native_digital_silence(invalid, 2));
    std::cout << "21 native audio-boundary/silence checks passed\n";
}
