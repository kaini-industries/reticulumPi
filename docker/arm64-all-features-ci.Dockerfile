# syntax=docker/dockerfile:1.7
ARG PYTHON_BOOKWORM_IMAGE=python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361

FROM ${PYTHON_BOOKWORM_IMAGE}

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# RPi.GPIO and radio decoders intentionally retain reviewed, hash-pinned source
# distributions. This compiler exists only in the disposable ARM64 CI gate.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY constraints/production-universal-all-features.txt /tmp/constraints/all-features.txt
COPY dist/reticulumpi-*.whl /tmp/wheels/

RUN set -eu; \
    set -- /tmp/wheels/reticulumpi-*.whl; \
    test "$#" -eq 1; \
    python -m pip install --require-hashes --requirement /tmp/constraints/all-features.txt; \
    python -m pip install --no-deps "${1}"; \
    python -m pip check; \
    python -c 'import nacl.bindings; from importlib.metadata import version; [version(name) for name in ("RPi.GPIO", "meshtastic", "meshcore", "PyNaCl", "pyModeS", "pynmea2", "pyserial", "sgp4", "smbus2")]'; \
    rm -rf /tmp/constraints /tmp/wheels

CMD ["python", "-m", "pip", "check"]
