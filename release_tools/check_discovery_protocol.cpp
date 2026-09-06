// Model/driver-free C++ checks for the exact native discovery protocol owner.
#include "discovery_protocol.hpp"
#include <cassert>
#include <iostream>
#include <sstream>
#include <vector>

int main() {
    int calls = 0;
    auto discover = [&calls]() {
        ++calls;
        return nlohmann::ordered_json{{"protocol", 1}, {"devices", nlohmann::ordered_json::array()}};
    };
    for (const std::string & invalid : std::vector<std::string>{
        "", "{\"operation\":\"discover\"}", "[]\n", "null\n",
        "{\"operation\":\"load\"}\n", "{\"operation\":\"discover\",\"extra\":1}\n",
        "{\"operation\":\"discover\",\"operation\":\"discover\"}\n",
        std::string(16385, 'x') + "\n"}) {
        std::istringstream input(invalid);
        std::ostringstream output;
        assert(serve_managed_discovery(input, output, discover) == 1);
        assert(calls == 0);
        assert(nlohmann::ordered_json::parse(output.str()).at("code") == "discovery_failed");
    }
    {
        std::istringstream input("{\"operation\": \"discover\"}\n{\"operation\": \"unload\"}\n");
        std::ostringstream output;
        assert(serve_managed_discovery(input, output, discover) == 0);
        assert(calls == 1);
        std::istringstream packets(output.str());
        std::string line;
        assert(read_private_command(packets, line));
        assert(nlohmann::ordered_json::parse(line).at("event") == "discovered");
        assert(read_private_command(packets, line));
        assert(nlohmann::ordered_json::parse(line).at("event") == "released");
        assert(!read_private_command(packets, line));
    }
    {
        std::istringstream input("{\"operation\":\"discover\"}\n{\"operation\":\"load\"}\n");
        std::ostringstream output;
        assert(serve_managed_discovery(input, output, discover) == 1);
        assert(calls == 2);
        assert(output.str().find("discovery_failed") != std::string::npos);
    }
    {
        std::istringstream input(std::string(16384, 'x') + "\n");
        std::string line;
        assert(read_private_command(input, line) && line.size() == 16384);
    }
    struct ExistingWorkerError : std::runtime_error { using std::runtime_error::runtime_error; };
    for (const auto & bad : {std::string(16385, 'x'), std::string("partial")}) {
        std::istringstream input(bad);
        std::string line;
        bool retained_type = false;
        try { read_private_command<ExistingWorkerError>(input, line); }
        catch (const ExistingWorkerError &) { retained_type = true; }
        assert(retained_type);
    }
    std::cout << "13 native discovery/command cases passed; no GPU queries\n";
}
