#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 RELEASE_ASSETS GHCR_IMAGE VERSION" >&2
    exit 2
fi

assets=$1
image=${2,,}
version=$3
if [[ ! $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
    echo "invalid release version: $version" >&2
    exit 2
fi
if [[ ! $image =~ ^ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._/-]*$ ]]; then
    echo "invalid GHCR image name: $image" >&2
    exit 2
fi
if [[ ! -d $assets || -L $assets ]]; then
    echo "release asset directory is missing or unsafe: $assets" >&2
    exit 2
fi
manifest="$assets/SHA256SUMS"
signature="$assets/SHA256SUMS.minisig"
if [[ ! -f $manifest || -L $manifest || ! -f $signature || -L $signature ]]; then
    echo "signed global release manifest is missing or unsafe" >&2
    exit 1
fi
(cd "$assets" && sha256sum --check --strict SHA256SUMS >/dev/null)

assert_tag_absent() {
    local tag=$1
    local output
    local status
    set +e
    output=$(docker manifest inspect "$image:$tag" 2>&1)
    status=$?
    set -e
    if (( status == 0 )); then
        echo "refusing to overwrite existing GHCR tag: $image:$tag" >&2
        exit 1
    fi
    if [[ $output != *"manifest unknown"* && $output != *"no such manifest"* ]]; then
        echo "could not prove GHCR tag is absent: $image:$tag" >&2
        echo "$output" >&2
        exit 1
    fi
}

for tag in "$version" "$version-amd64" "$version-arm64"; do
    assert_tag_absent "$tag"
done

declare -A digests
for architecture in amd64 arm64; do
    archive="$assets/reticulumpi-container-${version}-${architecture}.tar.gz"
    if [[ ! -f $archive || -L $archive ]]; then
        echo "validated image archive is missing or unsafe: $archive" >&2
        exit 1
    fi
    gzip --decompress --stdout "$archive" | docker load >/dev/null
    local_image="reticulumpi:${architecture}"
    actual_architecture=$(docker image inspect --format '{{.Architecture}}' "$local_image")
    actual_os=$(docker image inspect --format '{{.Os}}' "$local_image")
    if [[ $actual_architecture != "$architecture" || $actual_os != linux ]]; then
        echo "loaded image platform is $actual_os/$actual_architecture, expected linux/$architecture" >&2
        exit 1
    fi
    target="$image:$version-$architecture"
    docker tag "$local_image" "$target"
    push_output=$(docker push "$target")
    digest=$(sed -nE 's/^.*digest: (sha256:[0-9a-f]{64}).*$/\1/p' <<<"$push_output" | tail -n 1)
    if [[ ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "registry did not return a digest for $target" >&2
        exit 1
    fi
    digests[$architecture]=$digest
done

multiarch="$image:$version"
docker manifest create \
    "$multiarch" \
    "$image:$version-amd64" \
    "$image:$version-arm64" >/dev/null
docker manifest annotate "$multiarch" "$image:$version-amd64" --os linux --arch amd64
docker manifest annotate "$multiarch" "$image:$version-arm64" --os linux --arch arm64
manifest_output=$(docker manifest push --purge "$multiarch")
manifest_digest=$(sed -nE 's/^.*(sha256:[0-9a-f]{64}).*$/\1/p' <<<"$manifest_output" | tail -n 1)
if [[ ! $manifest_digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "registry did not return a multi-architecture manifest digest" >&2
    exit 1
fi

if [[ -n ${GITHUB_OUTPUT:-} ]]; then
    {
        echo "image=$image"
        echo "digest=$manifest_digest"
        echo "amd64-digest=${digests[amd64]}"
        echo "arm64-digest=${digests[arm64]}"
    } >>"$GITHUB_OUTPUT"
else
    printf '%s@%s\n' "$image" "$manifest_digest"
fi
