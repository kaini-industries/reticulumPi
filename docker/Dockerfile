FROM python:3.11-slim-bookworm

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home reticulumpi
WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY plugins/ plugins/

RUN pip install --no-cache-dir .

USER reticulumpi

VOLUME ["/config", "/data"]

ENTRYPOINT ["reticulumpi"]
CMD ["--config", "/config/config.yaml"]
