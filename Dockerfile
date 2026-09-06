FROM mccloud/subgen@sha256:128a16bae4f6296fbddd95be3ff47a1c10815fdac6489a66e0b022f2b98c9076 AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && python3 -m pip install --no-cache-dir paho-mqtt==2.1.0 \
    && apt-get purge -y --auto-remove python3-pip \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="subgen-english-plex" \
      org.opencontainers.image.description="Custom Subgen image for English subtitle generation and translation in Plex-style libraries." \
      org.opencontainers.image.source="https://github.com/Herbertmt978/subgen-english-plex"

# Correct the pinned backend before packaging the application. Unknown source
# hashes fail the build; no runtime monkey-patch or relaxed timing validation.
COPY apply_stable_ts_fix.py /subgen/apply_stable_ts_fix.py
RUN python3 /subgen/apply_stable_ts_fix.py
COPY apply_faster_whisper_fix.py /subgen/apply_faster_whisper_fix.py
RUN python3 /subgen/apply_faster_whisper_fix.py

COPY subgen_override.py /subgen/subgen.py
COPY language_code.py /subgen/language_code.py
COPY subgen_failure_markers.py /subgen/subgen_failure_markers.py
COPY subgen_ops_safety.py /subgen/subgen_ops_safety.py
COPY profile_model_envelopes.py /subgen/profile_model_envelopes.py
COPY subgen_core /subgen/subgen_core

# The pinned upstream launcher waits on a child process without forwarding
# container signals.  Run the immutable packaged application as PID 1 so
# Uvicorn receives SIGTERM and can execute the lifespan shutdown path.
STOPSIGNAL SIGTERM
CMD ["python3", "-u", "/subgen/subgen.py"]

# Optional cross-vendor image. Build in the runtime's distribution so the
# worker cannot accidentally require a newer glibc than the installed app.
FROM mccloud/subgen@sha256:128a16bae4f6296fbddd95be3ff47a1c10815fdac6489a66e0b022f2b98c9076 AS vulkan-build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake git curl xz-utils libvulkan-dev spirv-headers \
    && rm -rf /var/lib/apt/lists/*
# LunarG's versioned SDK supplies glslc on Jammy. Verify the published SHA-256
# before extraction; no SDK or development tools are copied into the runtime.
RUN curl --fail --location --retry 3 \
    https://sdk.lunarg.com/sdk/download/1.3.296.0/linux/vulkan_sdk.tar.xz -o /tmp/vulkan-sdk.tar.xz \
    && echo '79b0a1593dadc46180526250836f3e53688a9a5fb42a0e5859eb72316dc4d53e  /tmp/vulkan-sdk.tar.xz' | sha256sum -c - \
    && mkdir /build-sdk && tar -xJf /tmp/vulkan-sdk.tar.xz -C /build-sdk \
    && rm /tmp/vulkan-sdk.tar.xz
ENV VULKAN_SDK=/build-sdk/1.3.296.0/x86_64
ENV PATH=${VULKAN_SDK}/bin:${PATH}
ENV CMAKE_PREFIX_PATH=${VULKAN_SDK}
RUN git init /build/whisper.cpp \
    && git -C /build/whisper.cpp remote add origin https://github.com/ggml-org/whisper.cpp.git \
    && git -C /build/whisper.cpp fetch --depth 1 origin 52a939a2a762224e255d366c1182b2af4dd1a032 \
    && git -C /build/whisper.cpp checkout --detach FETCH_HEAD \
    && test "$(git -C /build/whisper.cpp rev-parse HEAD)" = 52a939a2a762224e255d366c1182b2af4dd1a032
COPY native /build/native
RUN cd /build/whisper.cpp \
    && git apply --check /build/native/patches/whisper-cpp-vulkan-budget.patch \
    && git apply /build/native/patches/whisper-cpp-vulkan-budget.patch \
    && git apply --check /build/native/patches/whisper-cpp-language-segments.patch \
    && git apply /build/native/patches/whisper-cpp-language-segments.patch \
    && git apply --check /build/native/patches/whisper-cpp-request-seed.patch \
    && git apply /build/native/patches/whisper-cpp-request-seed.patch
RUN cmake -S /build/whisper.cpp -B /build/backend -DGGML_VULKAN=ON \
    -DGGML_NATIVE=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /build/backend --parallel 2 \
    && cmake -S /build/native -B /build/worker -DCMAKE_BUILD_TYPE=Release \
       -DWHISPER_CPP_SOURCE=/build/whisper.cpp -DWHISPER_CPP_BUILD=/build/backend \
    && cmake --build /build/worker --parallel 2
RUN mkdir /native-runtime \
    && cp /build/worker/subgen-whisper-worker /build/worker/subgen-vulkan-probe /native-runtime/ \
    && find /build/backend -type f -name '*.so*' -exec cp {} /native-runtime/ \; \
    && find /build/backend -type l -name '*.so*' -exec cp -P {} /native-runtime/ \; \
    && cp /build/whisper.cpp/LICENSE /native-runtime/WHISPER-LICENSE

FROM runtime AS vulkan
RUN apt-get update && apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-provisioning.txt /subgen/requirements-provisioning.txt
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip \
    && python3 -m pip install --no-cache-dir -r /subgen/requirements-provisioning.txt \
    && apt-get purge -y --auto-remove python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=vulkan-build /native-runtime /opt/subgen-vulkan
ENV LD_LIBRARY_PATH=/opt/subgen-vulkan:${LD_LIBRARY_PATH}

# No --target keeps the established CPU/CUDA image. Native compilers and
# Vulkan drivers are installed only when explicitly building --target vulkan.
FROM runtime AS default
