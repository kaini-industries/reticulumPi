# syntax=docker/dockerfile:1.7
ARG PYTHON_TRIXIE_IMAGE=python:3.14-slim-trixie@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

FROM scratch AS wheel-artifact
COPY dist/reticulumpi-*.whl /wheels/

FROM ${PYTHON_TRIXIE_IMAGE} AS python-patched

# Python 3.14.6 predates the upstream fix for CVE-2026-15308. Apply the
# verified CPython 3.14 backport fail-closed until a patched stable image is
# available, then record that exact remediation in the release VEX document.
COPY --chown=root:root docker/security/patch_cpython_html_parser.py /tmp/
RUN python /tmp/patch_cpython_html_parser.py /usr/local/lib/python3.14/html/parser.py && \
    rm -f /tmp/patch_cpython_html_parser.py

FROM python-patched AS test

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 reticulumpi-test && \
    useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp reticulumpi-test && \
    install -d -o reticulumpi-test -g reticulumpi-test /test
WORKDIR /test
COPY --from=wheel-artifact /wheels/*.whl /tmp/wheels/
COPY constraints/production-universal-dashboard-nomadnet.txt /tmp/constraints/runtime.txt
RUN set -eu; \
    python -m pip install --require-hashes --only-binary :all: \
        --requirement /tmp/constraints/runtime.txt; \
    set -- /tmp/wheels/reticulumpi-*.whl; \
    test "$#" -eq 1; \
    python -m pip install "${1}[dev]"; \
    python -m pip check
# The test target intentionally carries the repository fixtures and documentation.
# Runtime remains wheel-only below.  Copying the complete (dockerignored) context
# keeps repository-level tests such as tools/check_docs.py honest inside Trixie.
COPY --chown=reticulumpi-test:reticulumpi-test . .
USER reticulumpi-test
ARG PYTEST_WORKERS=2
ARG PYTEST_TIMEOUT=60
RUN python -c "from importlib.resources import files; p = files('reticulumpi.builtin_plugins.web_dashboard').joinpath('static/index.html'); assert p.is_file(), p" && \
    pytest tests/ -q --tb=short -n "$PYTEST_WORKERS" --timeout="$PYTEST_TIMEOUT"

FROM python-patched AS runtime

ARG RETICULUMPI_UID=10001
ARG RETICULUMPI_GID=10001
ENV HOME=/data \
    RETICULUMPI_STATE_DIR=/data \
    RETICULUMPI_RNSD_PID_FILE=/run/reticulumpi/rnsd.pid \
    XDG_CONFIG_HOME=/data/.config \
    XDG_DATA_HOME=/data/.local/share \
    XDG_STATE_HOME=/data/.local/state \
    XDG_CACHE_HOME=/cache \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends tini ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid "$RETICULUMPI_GID" reticulumpi && \
    useradd --uid "$RETICULUMPI_UID" --gid "$RETICULUMPI_GID" --no-create-home --home-dir /data --shell /usr/sbin/nologin reticulumpi && \
    install -d -o reticulumpi -g reticulumpi /data /cache /run/reticulumpi

COPY --from=wheel-artifact /wheels/*.whl /tmp/wheels/
COPY constraints/production-universal-dashboard-nomadnet.txt /tmp/constraints/runtime.txt
RUN set -eu; \
    python -m pip install --require-hashes --only-binary :all: \
        --requirement /tmp/constraints/runtime.txt; \
    set -- /tmp/wheels/reticulumpi-*.whl; \
    test "$#" -eq 1; \
    python -m pip install --no-deps "${1}"; \
    python -m pip check; \
    python -m pip uninstall --yes setuptools wheel pip; \
    rm -rf \
        /tmp/wheels \
        /tmp/constraints \
        /usr/local/lib/python3.14/ensurepip
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/reticulumpi-entrypoint
RUN chmod 0755 /usr/local/bin/reticulumpi-entrypoint

USER reticulumpi
WORKDIR /data
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -m reticulumpi.container_healthcheck

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/reticulumpi-entrypoint"]
CMD ["--config", "/config/config.yaml"]
