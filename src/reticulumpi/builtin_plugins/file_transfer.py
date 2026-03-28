"""File Transfer plugin — send and receive files between nodes over Reticulum."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import RNS

from reticulumpi import events
from reticulumpi.plugin_base import PluginBase


class FileTransferPlugin(PluginBase):
    """Enables file transfer between ReticulumPi nodes over the mesh.

    Uses RNS.Resource for large file transfers with automatic chunking,
    compression, and integrity checking.
    """

    plugin_name = "file_transfer"
    plugin_version = "1.0.0"
    plugin_description = "File transfer between nodes over Reticulum"

    def validate_config(self) -> None:
        max_size = self.config.get("max_file_size_mb", 50)
        if not isinstance(max_size, (int, float)) or max_size < 1:
            raise ValueError("max_file_size_mb must be >= 1")

        allowed = self.config.get("allowed_identities", [])
        if not isinstance(allowed, list):
            raise ValueError("allowed_identities must be a list")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._transfers_completed = 0
        self._transfers_failed = 0
        self._current_transfers: dict[str, dict[str, Any]] = {}

        self._shared_dir = os.path.expanduser(
            self.config.get(
                "shared_dir", "~/.local/share/reticulumpi/shared_files"
            )
        )
        os.makedirs(self._shared_dir, exist_ok=True)

        self._max_size = self.config.get("max_file_size_mb", 50) * 1024 * 1024

        # Parse allowed identities
        self._allowed_hashes: set[bytes] | None = None
        allowed = self.config.get("allowed_identities", [])
        if allowed:
            self._allowed_hashes = set()
            for hex_hash in allowed:
                try:
                    self._allowed_hashes.add(
                        bytes.fromhex(hex_hash.replace("<", "").replace(">", ""))
                    )
                except ValueError:
                    self.log.warning("Invalid identity hash: %s", hex_hash)

        # Create file transfer destination
        self.destination = RNS.Destination(
            self.identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            "reticulumpi",
            "node",
            "filetransfer",
        )

        self.destination.set_link_established_callback(self._link_established)

        # Register request handlers for file listing
        self.destination.register_request_handler(
            "/list", self._handle_list, allow=RNS.Destination.ALLOW_ALL
        )
        self.destination.register_request_handler(
            "/info", self._handle_info, allow=RNS.Destination.ALLOW_ALL
        )

        self.log.info(
            "File transfer active at %s (shared: %s, max: %dMB)",
            RNS.prettyhexrep(self.destination.hash),
            self._shared_dir,
            self.config.get("max_file_size_mb", 50),
        )

    def stop(self) -> None:
        self._active = False
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": self._active,
                "shared_dir": getattr(self, "_shared_dir", None),
                "transfers_completed": self._transfers_completed,
                "transfers_failed": self._transfers_failed,
                "active_transfers": len(self._current_transfers),
                "shared_files": len(self._list_shared_files()),
            }

    def get_shared_files(self) -> list[dict[str, Any]]:
        """Return list of files in the shared directory."""
        return self._list_shared_files()

    # --- Link handling ---

    def _link_established(self, link: Any) -> None:
        """Accept incoming link for file transfer."""
        self.log.info("File transfer link from %s", link)

        # Configure resource acceptance
        link.set_resource_strategy(RNS.Link.ACCEPT_APP)
        link.set_resource_callback(self._resource_callback)
        link.set_resource_started_callback(self._resource_started)
        link.set_resource_concluded_callback(self._resource_concluded)

        # If we have allowed identities, authenticate
        if self._allowed_hashes is not None:
            link.set_remote_identified_callback(self._check_identity)

    def _check_identity(self, link: Any, identity: Any) -> None:
        if self._allowed_hashes and identity.hash not in self._allowed_hashes:
            self.log.warning(
                "Rejecting file transfer from unauthorized identity: %s",
                RNS.prettyhexrep(identity.hash),
            )
            link.teardown()

    def _resource_callback(self, resource: Any) -> bool:
        """Decide whether to accept an incoming resource."""
        # Check size
        if resource.size > self._max_size:
            self.log.warning(
                "Rejecting resource: size %d exceeds max %d",
                resource.size,
                self._max_size,
            )
            return False

        # Check disk space
        try:
            import shutil
            free = shutil.disk_usage(self._shared_dir).free
            if resource.size > free * 0.9:  # Leave 10% headroom
                self.log.warning("Rejecting resource: insufficient disk space")
                return False
        except Exception:
            pass

        auto_accept = self.config.get("auto_accept", True)
        if auto_accept:
            self.log.info("Accepting incoming file (%d bytes)", resource.size)
            return True

        self.log.info("Rejecting incoming file (auto_accept disabled)")
        return False

    def _resource_started(self, resource: Any) -> None:
        transfer_id = str(id(resource))
        with self._lock:
            self._current_transfers[transfer_id] = {
                "size": resource.size,
                "started": time.time(),
                "progress": 0,
            }
        self.log.info("File transfer started: %d bytes", resource.size)

    def _resource_concluded(self, resource: Any) -> None:
        transfer_id = str(id(resource))
        with self._lock:
            self._current_transfers.pop(transfer_id, None)

        if resource.status == RNS.Resource.COMPLETE:
            with self._lock:
                self._transfers_completed += 1

            # Save the received data
            try:
                data = resource.data.read() if hasattr(resource.data, "read") else resource.data
                if isinstance(data, bytes):
                    filename = self._safe_filename(resource)
                    filepath = os.path.join(self._shared_dir, filename)
                    with open(filepath, "wb") as f:
                        f.write(data)
                    self.log.info("File received: %s (%d bytes)", filename, len(data))

                    self.event_bus.publish(events.FILE_RECEIVED, {
                        "filename": filename,
                        "size": len(data),
                        "path": filepath,
                    })
            except Exception:
                self.log.exception("Error saving received file")
        else:
            with self._lock:
                self._transfers_failed += 1
            self.log.warning("File transfer failed (status: %s)", resource.status)

    # --- Request handlers ---

    def _handle_list(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        import RNS.vendor.umsgpack as umsgpack
        files = self._list_shared_files()
        return umsgpack.packb({"ok": True, "data": files})

    def _handle_info(self, path: str, data: Any, request_id: Any, link_id: Any, remote_identity: Any, requested_at: Any) -> Any:
        import RNS.vendor.umsgpack as umsgpack
        if not isinstance(data, bytes):
            return umsgpack.packb({"ok": False, "error": "filename required"})
        try:
            req = umsgpack.unpackb(data)
            filename = req.get("name") if isinstance(req, dict) else str(req)
        except Exception:
            return umsgpack.packb({"ok": False, "error": "invalid request"})

        # Prevent path traversal
        safe_name = os.path.basename(filename)
        filepath = os.path.join(self._shared_dir, safe_name)
        if not os.path.isfile(filepath):
            return umsgpack.packb({"ok": False, "error": "file not found"})

        stat = os.stat(filepath)
        return umsgpack.packb({"ok": True, "data": {
            "name": safe_name,
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }})

    # --- Helpers ---

    def _list_shared_files(self) -> list[dict[str, Any]]:
        files = []
        try:
            for entry in os.scandir(self._shared_dir):
                if entry.is_file():
                    stat = entry.stat()
                    files.append({
                        "name": entry.name,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })
        except Exception:
            self.log.debug("Error listing shared files", exc_info=True)
        return sorted(files, key=lambda f: f.get("modified", 0), reverse=True)

    def _safe_filename(self, resource: Any) -> str:
        """Generate a safe filename for a received resource."""
        # Try to get filename from resource metadata
        name = None
        if hasattr(resource, "data") and hasattr(resource.data, "name"):
            name = os.path.basename(resource.data.name)

        if not name:
            name = f"received_{int(time.time())}_{resource.size}b"

        # Ensure no path traversal
        name = os.path.basename(name)

        # Avoid overwriting existing files
        base_path = os.path.join(self._shared_dir, name)
        if os.path.exists(base_path):
            base, ext = os.path.splitext(name)
            counter = 1
            while os.path.exists(os.path.join(self._shared_dir, f"{base}_{counter}{ext}")):
                counter += 1
            name = f"{base}_{counter}{ext}"

        return name
