#include "physical_device_identity.hpp"
#include <cassert>
#include <iostream>

static std::array<uint8_t, 16> physical{};
static bool available = true;
static bool read_uuid(int device, size_t capacity, uint8_t * output) {
    assert(device == 0 && capacity == 16 && output);
    if (!available) return false;
    for (size_t i = 0; i < physical.size(); ++i) output[i] = physical[i];
    return true;
}

int main() {
    const std::string requested = "00112233445566778899aabbccddeeff";
    assert(!matches_physical_uuid(nullptr, requested));
    assert(!matches_physical_uuid(read_uuid, std::string(32, '0')));
    for (size_t i = 0; i < physical.size(); ++i) physical[i] = static_cast<uint8_t>(i * 17);
    assert(matches_physical_uuid(read_uuid, requested));
    assert(!matches_physical_uuid(read_uuid, requested.substr(1)));
    assert(!matches_physical_uuid(read_uuid, requested + '0'));
    assert(!matches_physical_uuid(read_uuid, "00112233445566778899AABBCCDDEEFF"));
    assert(!matches_physical_uuid(read_uuid, "00112233445566778899aabbccddeefg"));
    available = false;
    assert(!matches_physical_uuid(read_uuid, requested));
    available = true;
    physical[15] ^= 1; // Different physical card, even if its name is identical.
    assert(!matches_physical_uuid(read_uuid, requested));
    physical[15] ^= 1;
    assert(matches_physical_uuid(read_uuid, requested));
    physical[0] = 1; // A changed allocating-instance identity cannot borrow the old budget.
    assert(!matches_physical_uuid(read_uuid, requested));
    std::cout << "11 native physical identity checks passed\n";
}
