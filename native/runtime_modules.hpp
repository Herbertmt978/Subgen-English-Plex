// Read-only loaded-module inventory for the private native worker protocol.
// Paths are observations, not instructions to open arbitrary files. The parent
// must first match them to its provisioned manifest. No module is loaded here.
#pragma once
#include <algorithm>
#include <stdexcept>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <psapi.h>
#else
#include <cstdlib>
#include <link.h>
#include <unistd.h>
#endif

static std::vector<std::string> loaded_runtime_modules() {
    std::vector<std::string> paths;
#ifdef _WIN32
    HMODULE modules[256]{};
    DWORD required = 0;
    if (!K32EnumProcessModules(GetCurrentProcess(), modules, sizeof(modules), &required)
            || required == 0 || required > sizeof(modules) || required % sizeof(HMODULE))
        throw std::runtime_error("runtime_module_inventory_unavailable");
    for (size_t index = 0; index < required / sizeof(HMODULE); ++index) {
        wchar_t path[32768]{};
        const auto size = GetModuleFileNameW(modules[index], path, 32768);
        if (!size || size >= 32768) throw std::runtime_error("runtime_module_path_unavailable");
        const int bytes = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, path,
            static_cast<int>(size), nullptr, 0, nullptr, nullptr);
        if (bytes <= 0 || bytes > 32768) throw std::runtime_error("runtime_module_path_invalid");
        std::string encoded(bytes, '\0');
        if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, path, static_cast<int>(size),
                &encoded[0], bytes, nullptr, nullptr) != bytes)
            throw std::runtime_error("runtime_module_path_invalid");
        paths.push_back(std::move(encoded));
    }
#else
    struct Inventory {
        std::vector<std::string> * paths;
        bool failed = false;
    } inventory{&paths};
    // Never unwind C++ exceptions through the loader's C callback.
    const auto visitor = [](dl_phdr_info * info, size_t, void * opaque) noexcept -> int {
        auto & state = *static_cast<Inventory *>(opaque);
        try {
            if (state.paths->size() >= 256) throw std::runtime_error("module_bound");
            if (info->dlpi_name && info->dlpi_name[0]) {
                std::string path(info->dlpi_name);
                if (path.size() > 32768) throw std::runtime_error("path_bound");
                // The virtual DSO has no on-disk file and is not a library path.
                if (path.rfind("linux-vdso", 0) != 0) {
                    // The loader can report a relative name or a SONAME
                    // symlink. Resolve it here, inside the observing process;
                    // the parent still never opens child-supplied-only paths.
                    char * resolved = realpath(path.c_str(), nullptr);
                    if (!resolved) throw std::runtime_error("module_path_unresolved");
                    try {
                        std::string canonical(resolved);
                        if (canonical.empty() || canonical[0] != '/' || canonical.size() > 32768)
                            throw std::runtime_error("module_path_invalid");
                        state.paths->push_back(std::move(canonical));
                    } catch (...) {
                        free(resolved);
                        throw;
                    }
                    free(resolved);
                }
            }
        } catch (...) {
            state.failed = true;
            return 1;
        }
        return 0;
    };
    if (dl_iterate_phdr(visitor, &inventory) != 0 || inventory.failed)
        throw std::runtime_error("runtime_module_inventory_unavailable");
    char executable[32768]{};
    const auto size = readlink("/proc/self/exe", executable, sizeof(executable));
    if (size <= 0 || size >= static_cast<ssize_t>(sizeof(executable)))
        throw std::runtime_error("runtime_executable_unavailable");
    paths.emplace_back(executable, static_cast<size_t>(size));
#endif
    std::sort(paths.begin(), paths.end());
    paths.erase(std::unique(paths.begin(), paths.end()), paths.end());
    if (paths.empty() || paths.size() > 256) throw std::runtime_error("runtime_module_inventory_invalid");
    return paths;
}
