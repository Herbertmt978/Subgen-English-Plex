# Experimental Vulkan worker

This is the optional v0.5 Vulkan backend. The selected-device Python runtime
supplies admission, model identity and worker lifecycle management. Public
Linux/Windows packages and the tested hardware are described in
`../docs/DEVICE_BUNDLE.md` and `../docs/RELEASE_NOTES_0.5.0.md`. The experimental
label reflects limited hardware coverage, not a requirement to assemble private
test binaries yourself.

The worker keeps one model loaded and accepts bounded chunk requests over
private pipes. It waits for the parent to establish process limits before
loading. The parent supplies a physical GPU UUID; the worker resolves and
checks that GPU instead of silently falling back to CPU. The patched backend
reports its own physical UUID before loading and on every memory observation.
Device names do not establish identity, so matching names no longer cause a
refusal. Wrong, missing or changed UUIDs still fail. The matching logic has
negative tests; multiple physically identical cards have not been tested.

## Building

The native result emitter bounds a final cue that crosses the real PCM end to
that end, keeping the cue's start and text. Whisper's last decoding window is
padded and can predict an endpoint after the supplied audio. The log reports
both times without dialogue. This narrow boundary rule does not reorder cues:
overlaps, reversed times, speech starting beyond the input, non-final overruns
and overruns exceeding one decoder window still fail. The Python result parser
continues to reject every out-of-bounds result it receives.

Run `release_tools/check_native_audio_bounds.cpp` with a C++17 compiler and
`-I native`. This model-free check includes the captured 310-second overrun.
Real unpadded inference and ordered joins must also pass on a rebuilt worker.

The worker keeps whisper.cpp's default non-speech suppression setting. Forcing
the CUDA-style setting lost a following sentence in the Intel regression tests,
even with an explicit English input language. Restoring the native default
retained it without changing timestamps or filtering the resulting words.

Completely zero-valued PCM is handled before inference: the worker reports an
empty result, completed progress and its current memory observation. Automatic
language is `und` (undetermined); an explicit language is preserved. This is not
a loudness threshold. Any nonzero sample, however quiet, still reaches Whisper.
The model remains available for the next chunk. The audio-boundary check above
also covers signed zero, empty input, tiny nonzero samples and nonfinite values;
the input reader separately rejects empty or invalid PCM before this check.

The worker also applies OpenAI Whisper's positive-duration timestamp rule during
token selection. A new cue may start exactly when the previous cue ends, but
its closing timestamp must advance. The pinned native backend's older rule
allowed repeated zero-duration cues; a large-v3 translation test produced eight
copies at 80 seconds and was correctly rejected by the result validator.
This correction changes decoding, not finished subtitle times or text. The
existing result checks still reject invalid output. Compile and run
`release_tools/check_native_timestamp_rules.cpp` with C++17 and `-I native` for
the model-free cases, then rerun actual inference. Rebuild the worker and update
its artifact hash; an old executable does not contain this correction.

Optional `runtime_artifacts` binds provisioned executable/library hashes to
the worker's actual loaded-module inventory, then rechecks those files after
initialization. Missing or shadowed libraries refuse the load. The parent never
opens paths supplied only by the child, and routine receipts omit private
paths. Windows load binding is tested. The Linux inventory now resolves loader
aliases inside the child process; a C++17 compile/run check passed for absolute
and relative symlink paths. The current isolated Intel container also passed
end-to-end worker manifest loading and normal-provider inference with the
matching libraries. Public packaging remains unfinished. A locally observed manifest is not signed release
provenance or complete OS/driver attestation.

Experimental callers can supply `model_artifact_identity` to the resident
worker. It checks the exact GGML file size and SHA-256 before starting the
process and again after model initialization. A mismatch refuses the load;
replacement during loading terminates the child. The model family and
English-only/multilingual receipt and loaded GGML weight format must also
agree. Missing or contradictory weight-format metadata terminates the child;
an unknown format is refused before launch. Hashing is streamed with
the lifecycle owner's cancellation and deadline checks.

This does not turn a native Windows run into an OCI/CUDA memory envelope.
The weight format is read from the initialized Whisper context, not guessed
from the filename. GGML "float16" means mostly F16 weights with some F32
tensors; it does not promise every computation uses F16. Source-checkpoint and
conversion provenance still need verification during provisioning. Matching model names or converted
file hashes cannot establish cross-backend equivalence. Unknown source
provenance does not match, even against itself; a known checkpoint match alone
does not establish equivalent precision or combined memory admission.
Immutable provisioning paths, native-library identity and resource accounting
remain prerequisites for public integration. These file checks are not
loaded-module attestation.

Use whisper.cpp revision `52a939a2a762224e255d366c1182b2af4dd1a032`.
Apply `patches/whisper-cpp-vulkan-budget.patch` to a clean checkout of that exact
revision, then build its Vulkan backend. The patch adds read-only budget and
physical-UUID queries; it does not change inference, allocation or the original
memory API. Older budget-only builds are refused before model loading.
Run `git apply --check` before applying it. Do not apply it blindly to a newer
upstream version or link this worker against an unpatched backend.

Also apply `patches/whisper-cpp-language-segments.patch` to that pinned revision.
In auto mode this checks the leading five seconds at each position inside the
native decoder's existing loop, updates its language token and clears carried
text when the language changes. It also prevents an ending timestamp from
skipping the remainder of that window when more speech may follow.
Explicit language choices are unchanged. It adds no subtitle joins and keeps the
same model resident. It currently repeats the encoder for each language check;
performance and recognition quality need hardware qualification. The worker
links against `subgen_whisper_language_segments_v2`, so an older library cannot
silently claim this behaviour. Rebuild both the library and worker and regenerate
their artifact hashes; the old native receipt is no longer applicable.

Apply `patches/whisper-cpp-request-seed.patch` as well. The pinned backend resets
the other beam workers for each request but leaves worker zero's sampling state
behind. This patch gives all beams a consistent request seed, so a previous
file or task cannot change the next request's random-sampling state. It does
not remove temperature fallback or change the chosen model. Rebuild the library
and regenerate its artifact hashes; the library from before this patch is not
the same candidate.

Configure this directory with CMake, supplying `WHISPER_CPP_SOURCE` and
`WHISPER_CPP_BUILD`. A C++17 compiler and Vulkan SDK headers/library are required.
The resulting worker needs the matching whisper/ggml runtime libraries and
Vulkan driver. The standalone probe only discovers devices and driver budgets;
it does not load a model.

## Managed discovery

The probe also accepts `--managed` for Subgen's supervised discovery path. It
waits for the parent's discovery command before querying Vulkan, then waits for
release before exiting. The same bounded pipe transport supervises CUDA and
Vulkan discovery; there is no Python wrapper launching an untracked native
child. Running the probe without arguments keeps its original standalone output.
The managed protocol has model-free native and Python tests. Public provisioning
and hardware qualification are still unfinished.

## Why the budget patch is necessary


On the tested AMD Windows driver, a separate Vulkan instance reports zero
usage even when another instance in the same process has allocated memory.
A controlled 64 MiB allocation was visible to the allocating instance and
invisible to the separate instance. Querying the right process was not enough.

The worker therefore reads heap budgets through the instance that ggml uses
for inference and checks that instance's physical device UUID. It refuses a
missing query bridge. Unsupported or failed
telemetry must not be interpreted as free memory. The standalone probe remains
useful for identity and topology, but is not a model-memory measurement.

These are driver estimates, not a complete memory envelope. Keep shared GPU
memory and host RAM from being counted twice, preserve system/container limits,
and validate actual load, working peaks and release on the intended backend.
Do not sum independent GPUs' VRAM or claim every driver behaves like this one.

`release_tools/check_native_modules.cpp` is the model-free Linux inventory
check. Compile it with `-std=c++17 -I native -ldl`, then pass a library path.
Use a relative symlink to a small test library to cover the loader-alias case.

`release_tools/check_multi_device_result.py` exercises the N-worker scheduling
core against installed stable-ts, with no inference or model download. It
checks two-hour synthetic inputs, 2–5 workers, out-of-order finishes, shrinking
retries, ordered SRT output and temporary-file cleanup. It does not qualify
multi-GPU performance or memory recovery.

`release_tools/check_native_device_identity.cpp` exercises UUID matching,
missing/failed readers, invalid identities and device changes. Compile it with
C++17 and `-I native`. Real mixed-device loading still needs the exact rebuilt
worker/library hashes; earlier budget-only inference is not reusable evidence.
## Installed subtitle-result compatibility check

`release_tools/check_vulkan_result.py` checks the Python adapter with the
actual installed stable-ts, outside pytest's mocked machine-learning modules.
Run it with the checkout on `PYTHONPATH`. It uses synthetic native-format
receipts and extracted WAV audio, so it needs no GPU or model download.

The check runs three chunks through the existing coordinator and disk-backed
journal. It checks segment-only timestamps, overlap ownership, text appearing
once in the final SRT, empty results, and rejection of a malformed second chunk
without joining partial output. Temporary audio and journals must be cleaned
up. This is a result-contract test, not inference, subtitle-quality or adaptive
memory qualification. The native/backend identity and admission integration
remain required before this backend can be selected publicly.
