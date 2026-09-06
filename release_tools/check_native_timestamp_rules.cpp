#include "timestamp_rules.hpp"
#include <cassert>
#include <climits>
#include <initializer_list>
#include <iostream>
#include <vector>

struct Token { int id; };
static int64_t floor_for(std::initializer_list<int> ids) {
    std::vector<Token> tokens;
    for (auto id : ids) tokens.push_back({id});
    return native_timestamp_floor(tokens.data(), tokens.size(), 100);
}

int main() {
    assert(floor_for({}) == 100);
    assert(floor_for({1,2}) == 100);
    assert(floor_for({100}) == 101);
    assert(floor_for({100,1}) == 101); // A spoken cue cannot close at its start.
    assert(floor_for({100,1,120}) == 120); // Next cue may start at this end.
    assert(floor_for({100,1,120,120}) == 121);
    assert(floor_for({100,1,120,120,2}) == 121);
    assert(floor_for({100,1,120,120,2,140}) == 140);
    // Captured repeat-at-80s pattern, using relative timestamp token ids.
    assert(floor_for({100,1,500,500,2,3,4}) == 501);
    assert(floor_for({100,1,500,500,2,3,4,501}) == 501);
    assert(floor_for({INT_MAX,1}) == static_cast<int64_t>(INT_MAX)+1);
    std::cout << "11 native timestamp-rule checks passed\n";
}
