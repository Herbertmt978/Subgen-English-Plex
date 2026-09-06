#include "vulkan_observation.hpp"
#include "discovery_protocol.hpp"

int main(int argc, char ** argv) {
    if (argc == 2 && std::string(argv[1]) == "--managed")
        return serve_managed_discovery(std::cin, std::cout, read_vulkan_observations);
    if (argc != 1) return 1;
    try {
        std::cout << read_vulkan_observations().dump() << '\n';
        return std::cout ? 0 : 1;
    } catch (...) {
        std::cerr << "Vulkan resource probe failed; memory availability is unknown\n";
        return 1;
    }
}
