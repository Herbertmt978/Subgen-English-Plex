FROM mccloud/subgen@sha256:128a16bae4f6296fbddd95be3ff47a1c10815fdac6489a66e0b022f2b98c9076

LABEL org.opencontainers.image.title="subgen-english-plex" \
      org.opencontainers.image.description="Custom Subgen image for English subtitle generation and translation in Plex-style libraries." \
      org.opencontainers.image.source="https://github.com/Herbertmt978/subgen-english-plex"

COPY subgen_override.py /subgen/subgen.py
COPY language_code.py /subgen/language_code.py
COPY subgen_failure_markers.py /subgen/subgen_failure_markers.py
COPY subgen_ops_safety.py /subgen/subgen_ops_safety.py
COPY subgen_core /subgen/subgen_core
