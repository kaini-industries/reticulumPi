# syntax=docker/dockerfile:1.7
ARG PYTHON_BOOKWORM_IMAGE=python:3.11-slim-bookworm@sha256:f5cf0344c9886ff24d34797578d5d7dd6e8911ae0fe5962bb55d0f89603ec361

FROM scratch AS wheel-artifact
COPY dist/reticulumpi-*.whl /wheels/

FROM ${PYTHON_BOOKWORM_IMAGE} AS test

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
# keeps repository-level tests such as tools/check_docs.py honest inside Bookworm.
COPY --chown=reticulumpi-test:reticulumpi-test . .
USER reticulumpi-test
RUN python -c "from importlib.resources import files; p = files('reticulumpi.builtin_plugins.web_dashboard').joinpath('static/index.html'); assert p.is_file(), p" && \
    pytest tests/ -q --tb=short

FROM ${PYTHON_BOOKWORM_IMAGE} AS runtime

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
    rm -rf /tmp/wheels /tmp/constraints
COPY --chown=root:root docker/entrypoint.sh /usr/local/bin/reticulumpi-entrypoint
RUN chmod 0755 /usr/local/bin/reticulumpi-entrypoint

USER reticulumpi
WORKDIR /data
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -m reticulumpi.container_healthcheck

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/reticulumpi-entrypoint"]
CMD ["--config", "/config/config.yaml"]
