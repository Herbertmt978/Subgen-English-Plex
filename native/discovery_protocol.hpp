#pragma once
#include "json.hpp"
#include "private_command.hpp"
#include <ostream>
#include <set>

inline void require_discovery_command(std::istream & input, const char * operation) {
    std::string line;
    if (!read_private_command(input, line)) throw std::runtime_error("command_required");
    std::set<std::string> keys;
    auto command = nlohmann::ordered_json::parse(line,
        [&keys](int, nlohmann::ordered_json::parse_event_t event, nlohmann::ordered_json & value) {
            if (event == nlohmann::ordered_json::parse_event_t::key &&
                !keys.insert(value.get<std::string>()).second)
                throw std::runtime_error("duplicate_key");
            return true;
        });
    if (!command.is_object() || command.size() != 1 ||
        !command.contains("operation") || command.at("operation") != operation)
        throw std::runtime_error("invalid_command");
}

template<class Discover>
int serve_managed_discovery(std::istream & input, std::ostream & output, Discover discover) {
    using packet = nlohmann::ordered_json;
    try {
        require_discovery_command(input, "discover");
        // No device/driver query before the parent has installed process limits.
        output << packet{{"event", "discovered"}, {"protocol", 1},
                         {"observation", discover()}}.dump() << '\n' << std::flush;
        if (!output) throw std::runtime_error("discovery_output_failed");
        require_discovery_command(input, "unload");
        output << packet{{"event", "released"}, {"protocol", 1}}.dump() << '\n' << std::flush;
        return output ? 0 : 1;
    } catch (...) {
        output << "{\"event\":\"error\",\"code\":\"discovery_failed\"}\n" << std::flush;
        return 1;
    }
}
