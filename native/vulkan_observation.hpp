#pragma once
// Shared read-only Vulkan topology and process-budget observation owner.
#include <vulkan/vulkan.h>
#include "json.hpp"
#include <algorithm>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using json = nlohmann::ordered_json;

static void check(VkResult result) {
    if (result != VK_SUCCESS) throw std::runtime_error("vulkan_query_failed");
}

static std::string hexadecimal(const uint8_t * bytes, size_t size) {
    constexpr char digits[] = "0123456789abcdef";
    std::string value;
    value.reserve(size * 2);
    for (size_t i = 0; i < size; ++i) {
        value += digits[bytes[i] >> 4];
        value += digits[bytes[i] & 15];
    }
    return value;
}

inline json read_vulkan_observations() {
    VkInstance instance = VK_NULL_HANDLE;
    try {
        VkApplicationInfo application{};
        application.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
        application.pApplicationName = "Subgen resource probe";
        application.apiVersion = VK_API_VERSION_1_1;
        VkInstanceCreateInfo create{};
        create.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
        create.pApplicationInfo = &application;
        check(vkCreateInstance(&create, nullptr, &instance));
        uint32_t count = 0;
        check(vkEnumeratePhysicalDevices(instance, &count, nullptr));
        if (count > 32) throw std::runtime_error("device_count_bound");
        std::vector<VkPhysicalDevice> devices(count);
        if (count) check(vkEnumeratePhysicalDevices(instance, &count, devices.data()));
        auto observations = json::array();
        for (uint32_t index = 0; index < count; ++index) {
            auto device = devices[index];
            uint32_t extension_count = 0;
            check(vkEnumerateDeviceExtensionProperties(device, nullptr, &extension_count, nullptr));
            if (extension_count > 4096) throw std::runtime_error("extension_count_bound");
            std::vector<VkExtensionProperties> extensions(extension_count);
            if (extension_count) check(vkEnumerateDeviceExtensionProperties(device, nullptr, &extension_count, extensions.data()));
            const auto supports = [&](const char * name) {
                return std::any_of(extensions.begin(), extensions.end(), [&](const auto & extension) {
                    return std::strcmp(extension.extensionName, name) == 0;
                });
            };
            const bool budget_supported = supports(VK_EXT_MEMORY_BUDGET_EXTENSION_NAME);
            const bool pci_supported = supports(VK_EXT_PCI_BUS_INFO_EXTENSION_NAME);
            VkPhysicalDeviceIDProperties identity{};
            identity.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ID_PROPERTIES;
            VkPhysicalDevicePCIBusInfoPropertiesEXT pci{};
            pci.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PCI_BUS_INFO_PROPERTIES_EXT;
            if (pci_supported) identity.pNext = &pci;
            VkPhysicalDeviceProperties2 properties{};
            properties.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2;
            properties.pNext = &identity;
            vkGetPhysicalDeviceProperties2(device, &properties);
            const auto & p = properties.properties;
            // Ignore CPU/software devices. An absent supported GPU stays absent.
            if (p.deviceType != VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU &&
                p.deviceType != VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) continue;
            const bool shared = p.deviceType == VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU;
            VkPhysicalDeviceMemoryBudgetPropertiesEXT budget{};
            budget.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_BUDGET_PROPERTIES_EXT;
            VkPhysicalDeviceMemoryProperties2 memory{};
            memory.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MEMORY_PROPERTIES_2;
            if (budget_supported) memory.pNext = &budget;
            vkGetPhysicalDeviceMemoryProperties2(device, &memory);
            if (memory.memoryProperties.memoryHeapCount > VK_MAX_MEMORY_HEAPS)
                throw std::runtime_error("heap_count_bound");
            auto heaps = json::array();
            for (uint32_t h = 0; h < memory.memoryProperties.memoryHeapCount; ++h) {
                const auto & heap = memory.memoryProperties.memoryHeaps[h];
                json observation = {{"index", h}, {"size_bytes", heap.size},
                                    {"device_local", bool(heap.flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)}};
                if (budget_supported) {
                    // Budget can change and usage may exceed it. Never unsigned-wrap.
                    const auto bounded_budget = std::min(budget.heapBudget[h], heap.size);
                    observation["budget_bytes"] = bounded_budget;
                    observation["usage_bytes"] = budget.heapUsage[h];
                    observation["available_bytes"] = budget.heapUsage[h] >= bounded_budget ? 0 : bounded_budget - budget.heapUsage[h];
                } else {
                    observation["budget_bytes"] = nullptr;
                    observation["usage_bytes"] = nullptr;
                    observation["available_bytes"] = nullptr;
                }
                heaps.push_back(observation);
            }
            char pci_id[40] = {};
            if (pci_supported)
                std::snprintf(pci_id, sizeof(pci_id), "%04x:%02x:%02x.%x", pci.pciDomain, pci.pciBus, pci.pciDevice, pci.pciFunction);
            observations.push_back({{"physical_index", index}, {"name", p.deviceName},
                {"uuid", hexadecimal(identity.deviceUUID, VK_UUID_SIZE)},
                {"pci_id", pci_supported ? json(pci_id) : json(nullptr)},
                {"vendor_id", p.vendorID}, {"device_id", p.deviceID},
                {"driver_version_raw", p.driverVersion}, {"api_version_raw", p.apiVersion},
                {"memory_topology", shared ? "shared" : "dedicated"},
                {"budget_supported", budget_supported}, {"heaps", heaps}});
        }
        // VK_EXT_memory_budget heapUsage describes this process, not device-wide
        // usage. A standalone probe must not impersonate a resident worker.
        const auto result = json({{"protocol", 1}, {"usage_scope", "process"}, {"devices", observations}});
        if (result.dump().size() > 262144) throw std::runtime_error("output_bound");
        vkDestroyInstance(instance, nullptr);
        instance = VK_NULL_HANDLE;
        return result;
    } catch (...) {
        if (instance) vkDestroyInstance(instance, nullptr);
        throw;
    }
}
