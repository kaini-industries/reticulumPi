# syntax=docker/dockerfile:1.7
ARG UBUNTU_NOBLE_IMAGE=ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90

FROM ${UBUNTU_NOBLE_IMAGE}

ENV container=docker \
    DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/reticulumpi-ci-venv \
    PATH=/opt/reticulumpi-ci-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        dbus \
        iproute2 \
        minisign \
        procps \
        python3 \
        python3-dev \
        python3-venv \
        sudo \
        systemd \
        systemd-sysv && \
    rm -rf /var/lib/apt/lists/* && \
    systemctl mask \
        console-getty.service \
        getty@.service \
        systemd-logind.service \
        systemd-remount-fs.service && \
    python3 -m venv "${VIRTUAL_ENV}"

COPY dist/reticulumpi-*.whl /tmp/wheels/
RUN set -eu; \
    set -- /tmp/wheels/reticulumpi-*.whl; \
    test "$#" -eq 1; \
    python -m pip install "${1}[dev]"; \
    python -m pip check; \
    rm -rf /tmp/wheels

COPY admin-dist/reticulumpi-admin_*_linux-arm64-ubuntu-noble-py312_arm64.deb /tmp/reticulumpi-admin.deb
RUN dpkg --install /tmp/reticulumpi-admin.deb && \
    /usr/sbin/reticulumpi-admin --help >/dev/null && \
    rm -f /tmp/reticulumpi-admin.deb

COPY . /workspace
WORKDIR /workspace

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
