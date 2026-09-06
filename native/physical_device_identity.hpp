#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

using PhysicalUuidReader = bool (*)(int, size_t, uint8_t *);

// Match the backend's own physical device, never its display name. Each worker
// exposes only its selected device as backend index zero, regardless of the
// machine's physical inventory order or how many cards share the same name.
inline bool matches_physical_uuid(PhysicalUuidReader reader, const std::string & expected) {
    if (!reader || expected.size() != 32) return false;
    std::array<uint8_t, 16> observed{};
    if (!reader(0, observed.size(), observed.data())) return false;
    constexpr char digits[] = "0123456789abcdef";
    bool nonzero = false;
    for (size_t i = 0; i < observed.size(); ++i) {
        nonzero = nonzero || observed[i] != 0;
        if (expected[2 * i] != digits[observed[i] >> 4] ||
            expected[2 * i + 1] != digits[observed[i] & 15]) return false;
    }
    return nonzero;
}
