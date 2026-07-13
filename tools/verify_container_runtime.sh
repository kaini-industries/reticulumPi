#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 IMAGE PLATFORM" >&2
    exit 2
fi

image=$1
platform=$2
if [[ ! $image =~ ^[a-zA-Z0-9][a-zA-Z0-9._/@:-]*$ ]]; then
    echo "invalid image reference: $image" >&2
    exit 2
fi
if [[ $platform != linux/amd64 && $platform != linux/arm64 ]]; then
    echo "unsupported verification platform: $platform" >&2
    exit 2
fi
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
suffix=${platform//[^a-zA-Z0-9]/-}
run_id="${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}-$$"
container="reticulumpi-smoke-${suffix}-${run_id}"
data_volume="reticulumpi-data-${suffix}-${run_id}"

cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    docker volume rm -f "$data_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "$data_volume" >/dev/null

start_container() {
    docker run --detach \
        --name "$container" \
        --platform "$platform" \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges=true \
        --pids-limit 256 \
        --network none \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
        --tmpfs /cache:rw,noexec,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=0700 \
        --tmpfs /run/reticulumpi:rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=0700 \
        --volume "$root/docker/config.ci.yaml:/config/config.yaml:ro" \
        --volume "$data_volume:/data" \
        "$image" >/dev/null
}

wait_healthy() {
    local started=$SECONDS
    local health
    while (( SECONDS - started < 120 )); do
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container")
        if [[ $health == healthy ]]; then
            return
        fi
        if [[ $(docker inspect --format '{{.State.Running}}' "$container") != true ]]; then
            docker logs "$container" >&2
            echo "container exited before readiness" >&2
            exit 1
        fi
        sleep 1
    done
    docker logs "$container" >&2
    echo "container did not become healthy within 120 seconds" >&2
    exit 1
}

probe_state() {
    docker run --rm \
        --platform "$platform" \
        --read-only \
        --cap-drop ALL \
        --security-opt no-new-privileges=true \
        --network none \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
        --volume "$data_volume:/data:ro" \
        --volume "$root/tools/container_state_probe.py:/probe.py:ro" \
        --entrypoint python \
        "$image" /probe.py
}

verify_dashboard() {
    docker exec "$container" python -c '
import urllib.error
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8080/login.html", timeout=5) as response:
    assert response.status == 200
    assert b"ReticulumPi" in response.read(262144)
with urllib.request.urlopen("http://127.0.0.1:8080/api/version", timeout=5) as response:
    assert response.status == 200
    assert b"version" in response.read(65536)
try:
    urllib.request.urlopen("http://127.0.0.1:8080/api/status", timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
else:
    raise SystemExit("anonymous loopback access to a protected API unexpectedly succeeded")
'
    docker exec "$container" /bin/sh -ec '
        test "$HOME" = /data
        test ! -w /config/config.yaml
        test -f /run/reticulumpi/ready
    '
}

stop_gracefully() {
    local started=$SECONDS
    docker stop --time 60 "$container" >/dev/null
    if (( SECONDS - started > 60 )); then
        echo "container exceeded the 60-second shutdown deadline" >&2
        exit 1
    fi
    if [[ $(docker inspect --format '{{.State.ExitCode}}' "$container") -ne 0 ]]; then
        docker logs "$container" >&2
        echo "container did not terminate cleanly" >&2
        exit 1
    fi
    docker rm "$container" >/dev/null
}

# The runtime image is intentionally wheel-only and unprivileged.
docker run --rm \
    --platform "$platform" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges=true \
    --network none \
    --entrypoint /bin/sh \
    "$image" -ec '
    test "$(id -u)" = 10001
    test "$(id -g)" = 10001
    ! command -v cc
    ! command -v gcc
    ! command -v make
    ! command -v pip
    ! command -v pip3
    ! command -v sudo
    ! command -v systemctl
    python -c '\''
import importlib.util
from hashlib import sha256
from pathlib import Path

for name in ("ensurepip", "pip", "setuptools", "wheel"):
    assert importlib.util.find_spec(name) is None, name

parser_path = Path("/usr/local/lib/python3.14/html/parser.py")
assert sha256(parser_path.read_bytes()).hexdigest() == (
    "951b46301862483dbcb3debbbd39b4cef3b85ebe488f86cc2ff667f834dfe523"
)
'\''
    test ! -e /src
    test ! -e /workspace
    test ! -e /run/reticulumpi-control.sock
    ! touch /runtime-root-must-be-read-only
'

start_container
wait_healthy
verify_dashboard
first_state=$(probe_state)
docker exec "$container" /bin/sh -ec 'touch /cache/recreation-sentinel'
stop_gracefully

# Recreate the process from the exact image while retaining only /data.
# Identity, SQLite schemas, NomadNet pages, and dashboard credentials must be
# unchanged, while the disposable cache must start empty.
start_container
wait_healthy
verify_dashboard
docker exec "$container" /bin/sh -ec 'test ! -e /cache/recreation-sentinel'
second_state=$(probe_state)
if [[ $first_state != "$second_state" ]]; then
    echo "durable state changed across container recreation" >&2
    exit 1
fi
stop_gracefully

# A stale application marker must never mask loss of the shared Reticulum
# daemon. The entrypoint invalidates readiness and terminates the main service
# so Compose's restart policy can recover both processes together.
start_container
wait_healthy
docker exec "$container" /bin/sh -ec '
    pid=$(cat /run/reticulumpi/rnsd.pid)
    kill -TERM "$pid"
'
dependency_deadline=$((SECONDS + 15))
while [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]]; do
    if (( SECONDS >= dependency_deadline )); then
        docker logs "$container" >&2
        echo "container remained live after rnsd exited" >&2
        exit 1
    fi
    sleep 1
done
docker rm "$container" >/dev/null

echo "Container runtime, readiness, shutdown, and recreation persistence verified"
