FROM mccloud/subgen:latest

LABEL org.opencontainers.image.title="subgen-english-plex" \
      org.opencontainers.image.description="Custom Subgen image for English subtitle generation and translation in Plex-style libraries." \
      org.opencontainers.image.source="https://github.com/Herbertmt978/subgen-english-plex"

COPY subgen_override.py /subgen/subgen.py
COPY language_code.py /subgen/language_code.py
