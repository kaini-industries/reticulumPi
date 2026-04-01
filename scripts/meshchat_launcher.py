#!/usr/bin/env python3
"""Wrapper that launches MeshChat with patched NomadNet download timeouts
and enhanced logging for routing/connection diagnostics.

MeshChat's NomadnetDownloader has minimal logging — no destination hashes,
hop counts, timing, or failure details.  This launcher patches the class
to add structured diagnostic output before handing off to MeshChat.

Configuration is read from environment variables set by the meshchat_server
plugin:

    MESHCHAT_DIR                  – path to MeshChat install directory
    MESHCHAT_LINK_TIMEOUT         – link establishment timeout in seconds
    MESHCHAT_PATH_LOOKUP_TIMEOUT  – path discovery timeout in seconds

If any patch cannot be applied (e.g. MeshChat refactors the classes), the
launcher prints a warning and starts MeshChat with its built-in defaults.
"""

import os
import sys
import time


_TAG = "[meshchat_launcher]"

_LINK_STATUS_NAMES = {
    0: "PENDING",
    1: "HANDSHAKE",
    2: "ACTIVE",
    3: "STALE",
    4: "CLOSED",
}

_TEARDOWN_REASON_NAMES = {
    1: "TIMEOUT",
    2: "INITIATOR_CLOSED",
    3: "DESTINATION_CLOSED",
}


def _hex(destination_hash: bytes) -> str:
    """Safely convert a destination hash to a hex string."""
    if isinstance(destination_hash, bytes):
        return destination_hash.hex()
    return str(destination_hash)


def _apply_patches(meshchat_module):
    """Apply all monkey-patches to the imported meshchat module.

    Returns True if all critical patches succeeded, False otherwise.
    Each patch is wrapped individually so one failure doesn't block the rest.
    """
    import asyncio  # noqa: F811
    import RNS

    link_timeout = int(os.environ.get("MESHCHAT_LINK_TIMEOUT", "75"))
    path_lookup_timeout = int(os.environ.get("MESHCHAT_PATH_LOOKUP_TIMEOUT", "15"))

    ok = True

    # --- Patch 1: Enhanced download() with timeouts and logging ---
    try:
        Downloader = meshchat_module.NomadnetDownloader
        _original_download = Downloader.download
        _nomadnet_cached_links = meshchat_module.nomadnet_cached_links

        async def _patched_download(
            self,
            path_lookup_timeout=path_lookup_timeout,
            link_establishment_timeout=link_timeout,
        ):
            dest_hex = _hex(self.destination_hash)[:12]
            t0 = time.time()

            # --- Check cached link ---
            if self.destination_hash in _nomadnet_cached_links:
                link = _nomadnet_cached_links[self.destination_hash]
                if link.status is RNS.Link.ACTIVE:
                    print(
                        f"{_TAG} Reusing cached link to <{dest_hex}> "
                        f"for {self.path}"
                    )
                    self.link_established(link)
                    return

            # --- Path lookup ---
            has_path = RNS.Transport.has_path(self.destination_hash)
            if not has_path:
                print(
                    f"{_TAG} No path to <{dest_hex}>, requesting "
                    f"(timeout: {path_lookup_timeout}s)..."
                )
                RNS.Transport.request_path(self.destination_hash)
                timeout_at = time.time() + path_lookup_timeout
                while not RNS.Transport.has_path(self.destination_hash) and time.time() < timeout_at:
                    await asyncio.sleep(0.1)
                has_path = RNS.Transport.has_path(self.destination_hash)

            if not has_path:
                elapsed = time.time() - t0
                print(
                    f"{_TAG} Path lookup FAILED for <{dest_hex}> "
                    f"after {elapsed:.1f}s"
                )
                self.on_download_failure(
                    f"Could not find path to <{dest_hex}> "
                    f"after {path_lookup_timeout}s."
                )
                return

            hops = RNS.Transport.hops_to(self.destination_hash)
            path_elapsed = time.time() - t0
            if path_elapsed > 0.5:
                print(
                    f"{_TAG} Path to <{dest_hex}> resolved in "
                    f"{path_elapsed:.1f}s ({hops} hops)"
                )

            # --- Identity recall ---
            identity = RNS.Identity.recall(self.destination_hash)
            if identity is None:
                print(
                    f"{_TAG} Identity recall FAILED for <{dest_hex}> — "
                    f"cannot create destination"
                )
                self.on_download_failure(
                    f"Could not recall identity for <{dest_hex}>."
                )
                return

            # --- Link establishment ---
            destination = RNS.Destination(
                identity,
                RNS.Destination.OUT,
                RNS.Destination.SINGLE,
                self.app_name,
                self.aspects,
            )

            rns_timeout = hops * RNS.Link.ESTABLISHMENT_TIMEOUT_PER_HOP
            effective_timeout = max(link_establishment_timeout, rns_timeout + 5)
            print(
                f"{_TAG} Establishing link to <{dest_hex}> "
                f"({hops} hops, timeout: {effective_timeout}s) "
                f"for {self.path}"
            )
            link = RNS.Link(destination, established_callback=self.link_established)

            timeout_at = time.time() + effective_timeout
            while link.status == RNS.Link.PENDING and time.time() < timeout_at:
                await asyncio.sleep(0.1)

            elapsed = time.time() - t0
            status_name = _LINK_STATUS_NAMES.get(link.status, f"UNKNOWN({link.status})")

            if link.status == RNS.Link.ACTIVE:
                print(
                    f"{_TAG} Link to <{dest_hex}> ACTIVE in {elapsed:.2f}s "
                    f"({hops} hops)"
                )
                # link_established callback already fired via RNS
                return

            # Link failed — gather diagnostics
            teardown = getattr(link, "teardown_reason", None)
            teardown_name = _TEARDOWN_REASON_NAMES.get(teardown, str(teardown))
            print(
                f"{_TAG} Link to <{dest_hex}> FAILED — "
                f"status={status_name}, teardown={teardown_name}, "
                f"elapsed={elapsed:.1f}s, hops={hops}"
            )
            self.on_download_failure(
                f"Link to <{dest_hex}> failed after {elapsed:.1f}s "
                f"(status={status_name}, reason={teardown_name}, "
                f"hops={hops})."
            )

        Downloader.download = _patched_download
        print(
            f"{_TAG} Patched download(): "
            f"path_lookup={path_lookup_timeout}s, "
            f"link_establishment={link_timeout}s"
        )
    except Exception as exc:
        print(f"{_TAG} Warning: could not patch download() ({exc})")
        ok = False

    # --- Patch 2: Enhanced on_failed with request details ---
    try:
        Downloader = meshchat_module.NomadnetDownloader

        _original_on_failed = Downloader.on_failed

        def _patched_on_failed(self, request_receipt=None):
            dest_hex = _hex(self.destination_hash)[:12]
            detail = ""
            if request_receipt is not None:
                status = getattr(request_receipt, "status", None)
                progress = getattr(request_receipt, "progress", None)
                detail = f" (status={status}, progress={progress})"
            print(
                f"{_TAG} Request FAILED for <{dest_hex}>{self.path}{detail}"
            )
            self.on_download_failure(
                f"Request to <{dest_hex}>{self.path} failed{detail}."
            )

        Downloader.on_failed = _patched_on_failed
        print(f"{_TAG} Patched on_failed() with enhanced diagnostics")
    except Exception as exc:
        print(f"{_TAG} Warning: could not patch on_failed() ({exc})")

    # --- Patch 3: Log successful page downloads ---
    try:
        PageDownloader = meshchat_module.NomadnetPageDownloader
        _original_page_success = PageDownloader.on_download_success

        def _patched_page_success(self, request_receipt):
            dest_hex = _hex(self.destination_hash)[:12]
            size = len(request_receipt.response) if request_receipt.response else 0
            print(
                f"{_TAG} Page downloaded from <{dest_hex}>{self.path} "
                f"({size} bytes)"
            )
            _original_page_success(self, request_receipt)

        PageDownloader.on_download_success = _patched_page_success
        print(f"{_TAG} Patched page download success logging")
    except Exception as exc:
        print(f"{_TAG} Warning: could not patch page success logging ({exc})")

    # --- Patch 4: Log page download failures ---
    try:
        PageDownloader = meshchat_module.NomadnetPageDownloader
        _original_page_failure = PageDownloader.on_download_failure

        def _patched_page_failure(self, failure_reason):
            dest_hex = _hex(self.destination_hash)[:12]
            print(
                f"{_TAG} Page download FAILED for <{dest_hex}>{self.path}: "
                f"{failure_reason}"
            )
            _original_page_failure(self, failure_reason)

        PageDownloader.on_download_failure = _patched_page_failure
        print(f"{_TAG} Patched page download failure logging")
    except Exception as exc:
        print(f"{_TAG} Warning: could not patch page failure logging ({exc})")

    return ok


def main():
    meshchat_dir = os.environ.get("MESHCHAT_DIR", "")
    if not meshchat_dir:
        print(f"{_TAG} MESHCHAT_DIR not set, cannot locate MeshChat")
        sys.exit(1)

    # MeshChat uses relative imports (from src.backend...) so we need both
    # sys.path and the working directory set to the MeshChat install dir.
    sys.path.insert(0, meshchat_dir)
    os.chdir(meshchat_dir)

    # Import MeshChat's module — this triggers all its top-level imports.
    import meshchat  # noqa: E402

    # Apply all patches (timeouts + logging).
    _apply_patches(meshchat)

    # Hand off to MeshChat's normal CLI entry point.
    meshchat.main()


if __name__ == "__main__":
    main()
