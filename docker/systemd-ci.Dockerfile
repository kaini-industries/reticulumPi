# syntax=docker/dockerfile:1.7
ARG PYTHON_BOOKWORM_IMAGE=python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361

FROM ${PYTHON_BOOKWORM_IMAGE}

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        dbus \
        minisign=0.11-1 \
        procps \
        python3 \
        python3-venv \
        sudo \
        systemd \
        systemd-sysv && \
    rm -rf /var/lib/apt/lists/* && \
    systemctl mask \
        console-getty.service \
        getty@.service \
        systemd-logind.service \
        systemd-remount-fs.service

COPY dist/reticulumpi-*.whl /tmp/wheels/
RUN set -eu; \
    set -- /tmp/wheels/reticulumpi-*.whl; \
    test "$#" -eq 1; \
    python -m pip install "${1}[dev]"; \
    python -m pip check; \
    rm -rf /tmp/wheels

COPY admin-dist/reticulumpi-admin_*_linux-arm64-debian-bookworm-py311_arm64.deb /tmp/reticulumpi-admin.deb
RUN dpkg --install /tmp/reticulumpi-admin.deb && \
    /usr/sbin/reticulumpi-admin --help >/dev/null && \
    rm -f /tmp/reticulumpi-admin.deb

COPY . /workspace
WORKDIR /workspace

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
