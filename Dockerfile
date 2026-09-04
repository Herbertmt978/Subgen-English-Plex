FROM mccloud/subgen@sha256:128a16bae4f6296fbddd95be3ff47a1c10815fdac6489a66e0b022f2b98c9076

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && python3 -m pip install --no-cache-dir paho-mqtt==2.1.0 \
    && apt-get purge -y --auto-remove python3-pip \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="subgen-english-plex" \
      org.opencontainers.image.description="Custom Subgen image for English subtitle generation and translation in Plex-style libraries." \
      org.opencontainers.image.source="https://github.com/Herbertmt978/subgen-english-plex"

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
