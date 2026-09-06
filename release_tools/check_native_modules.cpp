// Linux module-path contract test. Build with -I native, -std=c++17 and -ldl.
// Supply a relative library alias to exercise loader-name canonicalization.
#include "runtime_modules.hpp"
#include <dlfcn.h>
#include <iostream>
int main(int argc, char ** argv) {
    if (argc != 2) return 1;
    void * handle = dlopen(argv[1], RTLD_NOW);
    if (!handle) return 2;
    char * canonical = realpath(argv[1], nullptr);
    if (!canonical) { dlclose(handle); return 3; }
    const auto paths = loaded_runtime_modules();
    bool found = false;
    for (const auto & path : paths) {
        if (path.empty() || path[0] != '/') { free(canonical); dlclose(handle); return 4; }
        if (path == canonical) found = true;
    }
    free(canonical);
    dlclose(handle);
    std::cout << "module_count=" << paths.size() << " canonical_library_found=" << found << '\n';
    return found ? 0 : 5;
}
