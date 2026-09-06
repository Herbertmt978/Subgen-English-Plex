// Experimental resident Vulkan worker. Protocol v1 uses private stdin/stdout;
// no listening socket, shell execution or model-switch endpoint is exposed.
#include "whisper.h"
#include "ggml-backend.h"
#include "json.hpp"
#include "vulkan_observation.hpp"
#include "runtime_modules.hpp"
#include "audio_bounds.hpp"
#include "timestamp_rules.hpp"
#include "physical_device_identity.hpp"
#include "private_command.hpp"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

using json = nlohmann::ordered_json;
struct WorkerError : std::runtime_error { using std::runtime_error::runtime_error; };
static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559);
static std::mutex output_mutex;
constexpr size_t MAX_RESULT = 8 * 1024 * 1024;
constexpr size_t MAX_SAMPLES = 1810 * 16000; // 30 minutes plus overlap
static std::string expected_backend;
static std::string selected_uuid;
using BudgetReader = bool (*)(int, size_t, size_t *, uint64_t *, uint64_t *, uint64_t *, uint32_t *);
static BudgetReader budget_reader = nullptr;
static PhysicalUuidReader uuid_reader = nullptr;
static std::atomic<bool> gpu_selected{false}, gpu_failed{false};

static void backend_log(ggml_log_level, const char * text, void *) noexcept {
    try {
        std::fputs(text, stderr);
        const std::string message(text);
        if (message.find("using " + expected_backend + " backend") != std::string::npos)
            gpu_selected = true;
        if (message.find("failed to initialize " + expected_backend + " backend") != std::string::npos)
            gpu_failed = true;
    } catch (...) {
        // Logging must never unwind through whisper/ggml's C callback ABI.
        gpu_failed = true;
    }
}

static void emit(const json & packet) {
    std::lock_guard<std::mutex> lock(output_mutex);
    const auto text = packet.dump();
    if (text.size() > MAX_RESULT) throw WorkerError("result_limit");
    std::cout << text << '\n' << std::flush;
    if (!std::cout) throw WorkerError("output_closed");
}

static json selected_observation() {
    if (!matches_physical_uuid(uuid_reader, selected_uuid))
        throw WorkerError("allocating_instance_identity_mismatch");
    const auto observation = read_vulkan_observations();
    for (auto device : observation.at("devices")) {
        if (device.at("uuid") == selected_uuid) {
            uint64_t sizes[16]{}, budgets[16]{}, usages[16]{};
            uint32_t flags[16]{};
            size_t count = 0;
            if (!budget_reader || !budget_reader(0, 16, &count, sizes, budgets, usages, flags) || count == 0 || count > 16)
                throw WorkerError("allocating_instance_budget_unavailable");
            auto heaps = json::array();
            for (size_t i = 0; i < count; ++i) {
                const auto budget = std::min(sizes[i], budgets[i]);
                heaps.push_back({{"index", i}, {"size_bytes", sizes[i]},
                    {"device_local", bool(flags[i] & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)},
                    {"budget_bytes", budget}, {"usage_bytes", usages[i]},
                    {"available_bytes", usages[i] >= budget ? 0 : budget - usages[i]}});
            }
            device["heaps"] = heaps;
            device["budget_supported"] = true;
            return {{"protocol", 1}, {"usage_scope", "process"},
                    {"query_scope", "allocating_instance"}, {"devices", json::array({device})}};
        }
    }
    throw WorkerError("selected_device_disappeared");
}

static int integer_argument(const char * text, int minimum, int maximum) {
    size_t used = 0;
    const int value = std::stoi(text, &used);
    if (used != std::string(text).size() || value < minimum || value > maximum)
        throw WorkerError("invalid_argument");
    return value;
}

struct Progress {
    int request_id;
    int last = -1;
    std::atomic<bool> failed{false};
};

static void progress_callback(whisper_context *, whisper_state *, int percent, void * opaque) {
    auto & progress = *static_cast<Progress *>(opaque);
    try {
        if (percent < 0 || percent > 100 || percent <= progress.last) return;
        progress.last = percent;
        emit({{"event", "progress"}, {"request_id", progress.request_id}, {"percent", percent},
              {"memory", selected_observation()}});
    } catch (...) {
        progress.failed = true; // never throw through the native callback ABI
    }
}

static void timestamp_filter(whisper_context * ctx, whisper_state *,
                             const whisper_token_data * tokens, int count,
                             float * logits, void *) noexcept {
    if (count <= 0) return;
    const int begin = whisper_token_beg(ctx);
    const auto end = std::min<int64_t>(whisper_n_vocab(ctx),
        native_timestamp_floor(tokens, static_cast<size_t>(count), begin));
    for (int64_t i = begin; i < end; ++i) {
        logits[i] = -std::numeric_limits<float>::infinity();
    }
}

static std::vector<float> read_audio(const std::string & path) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) throw WorkerError("audio_open");
    const auto size = input.tellg();
    if (size <= 0 || size % 4 != 0 || size > static_cast<std::streamoff>(MAX_SAMPLES * 4))
        throw WorkerError("audio_size");
    std::vector<float> audio(static_cast<size_t>(size) / 4);
    input.seekg(0);
    if (!input.read(reinterpret_cast<char *>(audio.data()), size))
        throw WorkerError("audio_read");
    for (float sample : audio)
        if (!std::isfinite(sample) || sample < -1.0f || sample > 1.0f)
            throw WorkerError("audio_samples");
    return audio;
}

int main(int argc, char ** argv) {
    std::unique_ptr<whisper_context, decltype(&whisper_free)> context(nullptr, whisper_free);
    try {
        if (argc != 4) throw WorkerError("usage_model_gpu_threads");
        if (subgen_whisper_language_segments_v2() != 2)
            throw WorkerError("segment_language_backend_required");
        const int gpu = integer_argument(argv[2], 0, 15);
        const int threads = integer_argument(argv[3], 1, 256);
        const uint16_t endian = 1;
        if (*reinterpret_cast<const uint8_t *>(&endian) != 1)
            throw WorkerError("little_endian_required");
        // Parent establishes its job/cgroup and admission reservation first.
        std::string load_command;
        if (!read_private_command<WorkerError>(std::cin, load_command))
            throw WorkerError("load_command_required");
        const auto load = json::parse(load_command);
        if (load.at("operation") != "load" || gpu != 0)
            throw WorkerError("explicit_physical_device_required");
        selected_uuid = load.at("physical_uuid").get<std::string>();
        if (selected_uuid.size() != 32 || selected_uuid.find_first_not_of("0123456789abcdef") != std::string::npos ||
            selected_uuid == std::string(32, '0')) throw WorkerError("invalid_physical_uuid");
        const auto topology = read_vulkan_observations();
        json selected;
        for (const auto & candidate : topology.at("devices"))
            if (candidate.at("uuid") == selected_uuid) {
                if (!selected.is_null()) throw WorkerError("ambiguous_physical_uuid");
                selected = candidate;
            }
        if (selected.is_null()) throw WorkerError("physical_device_unavailable");
        // Resolve the physical mask, then independently verify ggml's own UUID.
        // Display names are not identity: identical cards may be selected safely.
        const auto mask = std::to_string(selected.at("physical_index").get<int>());
#ifdef _WIN32
        if (_putenv_s("GGML_VK_VISIBLE_DEVICES", mask.c_str()) != 0)
#else
        if (setenv("GGML_VK_VISIBLE_DEVICES", mask.c_str(), 1) != 0)
#endif
            throw WorkerError("device_mask_failed");
        ggml_backend_load_all();
        ggml_backend_dev_t device = nullptr;
        int index = 0;
        for (size_t i = 0; i < ggml_backend_dev_count(); ++i) {
            auto candidate = ggml_backend_dev_get(i);
            const auto type = ggml_backend_dev_type(candidate);
            if (type != GGML_BACKEND_DEVICE_TYPE_GPU && type != GGML_BACKEND_DEVICE_TYPE_IGPU)
                continue;
            if (index++ == gpu) { device = candidate; break; }
        }
        if (!device || std::string(ggml_backend_reg_name(ggml_backend_dev_backend_reg(device))) != "Vulkan")
            throw WorkerError("requested_vulkan_device_unavailable");
        budget_reader = reinterpret_cast<BudgetReader>(ggml_backend_reg_get_proc_address(
            ggml_backend_dev_backend_reg(device), "subgen_vk_memory_budget_v1"));
        if (!budget_reader) throw WorkerError("required_budget_bridge_missing");
        uuid_reader = reinterpret_cast<PhysicalUuidReader>(ggml_backend_reg_get_proc_address(
            ggml_backend_dev_backend_reg(device), "subgen_vk_physical_uuid_v1"));
        if (!uuid_reader) throw WorkerError("required_identity_bridge_missing");
        if (!matches_physical_uuid(uuid_reader, selected_uuid))
            throw WorkerError("physical_device_binding_failed");
        ggml_backend_dev_props actual{};
        ggml_backend_dev_get_props(device, &actual);
        if (selected.at("name") != actual.description ||
            (selected.at("memory_topology") == "shared") != (actual.type == GGML_BACKEND_DEVICE_TYPE_IGPU) ||
            (!selected.at("pci_id").is_null() && (!actual.device_id || selected.at("pci_id") != actual.device_id)))
            throw WorkerError("physical_device_binding_failed");
        expected_backend = ggml_backend_dev_name(device);
        whisper_log_set(backend_log, nullptr);
        auto options = whisper_context_default_params();
        options.use_gpu = true;
        options.gpu_device = gpu;
        context.reset(whisper_init_from_file_with_params(argv[1], options));
        if (!context || !gpu_selected || gpu_failed)
            throw WorkerError("vulkan_load_not_confirmed");
        emit({{"event", "ready"}, {"protocol", 1}, {"backend", "Vulkan"},
              {"device", expected_backend}, {"device_description", ggml_backend_dev_description(device)},
              {"model_type", whisper_model_type_readable(context.get())},
              {"model_ftype", whisper_model_ftype(context.get())},
              {"runtime_modules", loaded_runtime_modules()},
              {"multilingual", whisper_is_multilingual(context.get()) != 0},
              {"memory", selected_observation()}});
        std::string line;
        int last_request = 0;
        while (read_private_command<WorkerError>(std::cin, line)) {
            const auto command = json::parse(line);
            const auto operation = command.at("operation").get<std::string>();
            if (operation == "unload") {
                context.reset();
                emit({{"event", "released"}, {"protocol", 1}, {"memory", selected_observation()}});
                return 0;
            }
            if (operation == "observe") {
                emit({{"event", "memory"}, {"memory", selected_observation()}});
                continue;
            }
            if (operation != "transcribe" || !command.at("request_id").is_number_integer())
                throw WorkerError("invalid_operation");
            const auto request_value = command.at("request_id").get<int64_t>();
            if (request_value <= 0 || request_value > 2147483647)
                throw WorkerError("invalid_request_id");
            const int request = static_cast<int>(request_value);
            if (request <= last_request) throw WorkerError("request_order");
            last_request = request;
            const auto language = command.at("language").get<std::string>();
            if (language != "auto" && whisper_lang_id(language.c_str()) < 0) throw WorkerError("invalid_language");
            const bool translate = command.at("translate").get<bool>();
            if (!whisper_is_multilingual(context.get()) && (translate || (language != "en" && language != "auto")))
                throw WorkerError("multilingual_model_required");
            auto audio = read_audio(command.at("audio_path").get<std::string>());
            Progress progress{request};
            if (native_digital_silence(audio.data(), audio.size())) {
                std::cerr << "Chunk contains digital silence; no speech to transcribe.\n";
                progress_callback(context.get(), nullptr, 100, &progress);
                if (progress.failed) throw WorkerError("inference_failed");
                emit({{"event", "result"}, {"request_id", request},
                      {"memory", selected_observation()},
                      {"result", {{"result", {{"language", language == "auto" ? "und" : language}}},
                                  {"transcription", json::array()}}}});
                continue;
            }
            auto params = whisper_full_default_params(WHISPER_SAMPLING_BEAM_SEARCH);
            params.n_threads = threads;
            params.language = !whisper_is_multilingual(context.get()) ? "en" : language.c_str();
            params.translate = translate;
            params.no_context = true;
            // Keep the native suppression default. Forcing the CUDA-like flag
            // can lose following speech at recording/language transitions.
            params.print_progress = params.print_realtime = params.print_timestamps = params.print_special = false;
            params.token_timestamps = false;
            params.logits_filter_callback = timestamp_filter;
            params.beam_search.beam_size = 5;
            params.progress_callback = progress_callback;
            params.progress_callback_user_data = &progress;
            params.abort_callback = [](void * pointer) { return static_cast<Progress *>(pointer)->failed.load(); };
            params.abort_callback_user_data = &progress;
            if (whisper_full(context.get(), params, audio.data(), static_cast<int>(audio.size())) != 0 || progress.failed)
                throw WorkerError("inference_failed");
            auto segments = json::array();
            const int count = whisper_full_n_segments(context.get());
            if (count < 0 || count > 20000) throw WorkerError("segment_limit");
            size_t text_bytes = 0;
            const int64_t audio_end_ms = static_cast<int64_t>(audio.size()) * 1000 / WHISPER_SAMPLE_RATE;
            int64_t previous_end_ms = 0;
            for (int i = 0; i < count; ++i) {
                const std::string text = whisper_full_get_segment_text(context.get(), i);
                text_bytes += text.size();
                if (text_bytes > MAX_RESULT) throw WorkerError("text_limit");
                const int64_t start_ms = whisper_full_get_segment_t0(context.get(), i) * 10;
                const int64_t predicted_end_ms = whisper_full_get_segment_t1(context.get(), i) * 10;
                int64_t end_ms;
                try {
                    end_ms = native_audio_end_ms(start_ms, predicted_end_ms, previous_end_ms,
                        audio_end_ms, i == count - 1, WHISPER_CHUNK_SIZE * 1000);
                } catch (const std::runtime_error &) {
                    throw WorkerError("invalid_segment_timing");
                }
                if (end_ms != predicted_end_ms) {
                    std::cerr << "Final cue ended beyond extracted audio: " << predicted_end_ms
                              << " ms; bounded to audio end " << end_ms
                              << " ms. Cue start and text preserved.\n";
                }
                previous_end_ms = end_ms;
                segments.push_back({{"offsets", {{"from", start_ms}, {"to", end_ms}}},
                                    {"text", text}});
            }
            emit({{"event", "result"}, {"request_id", request},
                  {"memory", selected_observation()},
                  {"result", {{"result", {{"language", whisper_lang_str(whisper_full_lang_id(context.get()))}}},
                              {"transcription", segments}}}});
        }
        context.reset();
        return 0;
    } catch (const WorkerError & error) {
        // Only our fixed codes are exposed, never parser input, paths or dialogue.
        try { emit({{"event", "error"}, {"code", error.what()}}); } catch (...) {}
        context.reset();
        return 1;
    } catch (const std::exception &) {
        try { emit({{"event", "error"}, {"code", "worker_failed"}}); } catch (...) {}
        context.reset();
        return 1;
    }
}
