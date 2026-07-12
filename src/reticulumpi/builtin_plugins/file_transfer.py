"""File Transfer plugin — send and receive files between nodes over Reticulum."""

from __future__ import annotations

import errno
import os
import secrets
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

    _ACCESS_POLICIES = {"deny", "allowlist", "open"}

    def validate_config(self) -> None:
        max_size = self.config.get("max_file_size_mb", 50)
        if not isinstance(max_size, (int, float)) or max_size < 1:
            raise ValueError("max_file_size_mb must be >= 1")

        allowed = self.config.get("allowed_identities", [])
        if not isinstance(allowed, list):
            raise ValueError("allowed_identities must be a list")
        policy = self.config.get("access_policy")
        if policy is not None and policy not in self._ACCESS_POLICIES:
            raise ValueError("access_policy must be deny, allowlist, or open")
        for identity_hash in allowed:
            if not isinstance(identity_hash, str):
                raise ValueError("allowed_identities entries must be hex strings")
            try:
                decoded = bytes.fromhex(identity_hash.replace("<", "").replace(">", ""))
            except ValueError as exc:
                raise ValueError("allowed_identities entries must be valid hex") from exc
            if not decoded:
                raise ValueError("allowed_identities entries cannot be empty")

    def start(self) -> None:
        self._active = True
        self._lock = threading.Lock()
        self._transfers_completed = 0
        self._transfers_failed = 0
        self._current_transfers: dict[str, dict[str, Any]] = {}
        self._links: dict[Any, Any] = {}
        self._authorized_links: set[Any] = set()

        self._shared_dir = os.path.expanduser(
            self.config.get("shared_dir", "~/.local/share/reticulumpi/shared_files")
        )
        os.makedirs(self._shared_dir, exist_ok=True)

        self._max_size = self.config.get("max_file_size_mb", 50) * 1024 * 1024

        # Parse allowed identities
        self._allowed_hashes: set[bytes] = set()
        allowed = self.config.get("allowed_identities", [])
        for hex_hash in allowed:
            self._allowed_hashes.add(bytes.fromhex(hex_hash.replace("<", "").replace(">", "")))

        configured_policy = self.config.get("access_policy")
        self._legacy_access_policy = (
            configured_policy is None and "allowed_identities" in self.config
        )
        if configured_policy is not None:
            self._access_policy = configured_policy
        elif allowed:
            self._access_policy = "allowlist"
        elif "allowed_identities" in self.config:
            # Compatibility for legacy configurations whose empty list meant
            # unrestricted access.  Make the migration visible rather than
            # silently changing existing deployments.
            self._access_policy = "open"
            self.log.critical(
                "Legacy file_transfer allowed_identities is empty: access is OPEN. "
                "Set access_policy explicitly; new configurations default to deny."
            )
        else:
            self._access_policy = "deny"

        # Create file transfer destination
        self.destination = self.manage_destination(
            RNS.Destination(
                self.identity,
                RNS.Destination.IN,
                RNS.Destination.SINGLE,
                "reticulumpi",
                "node",
                "filetransfer",
            )
        )

        self.destination.set_link_established_callback(self._link_established)

        # Register request handlers for file listing
        self.destination.register_request_handler(
            "/list", self._handle_list, allow=RNS.Destination.ALLOW_ALL
        )
        self.manage_request_handler(self.destination, "/list")
        self.destination.register_request_handler(
            "/info", self._handle_info, allow=RNS.Destination.ALLOW_ALL
        )
        self.manage_request_handler(self.destination, "/info")

        self.log.info(
            "File transfer active at %s (shared: %s, max: %dMB)",
            RNS.prettyhexrep(self.destination.hash),
            self._shared_dir,
            self.config.get("max_file_size_mb", 50),
        )

    def stop(self) -> None:
        self._active = False
        with self._lock:
            self._links.clear()
            self._authorized_links.clear()
        # PluginBase cleanup owns links, request handlers, and destination in
        # reverse acquisition order. The attribute is cleared now so stopped
        # code cannot accidentally reuse the destination while cleanup runs.
        self.destination = None
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
                "access_policy": getattr(self, "_access_policy", "deny"),
            }

    def get_shared_files(self) -> list[dict[str, Any]]:
        """Return list of files in the shared directory."""
        return self._list_shared_files()

    # --- Link handling ---

    def _link_established(self, link: Any) -> None:
        """Accept incoming link for file transfer."""
        if not self._active:
            link.teardown()
            return
        self.log.info("File transfer link from %s", link)

        # Resource callbacks alone are not authentication.  Start closed and
        # open only after the link has proven an allowed identity.
        link.set_resource_strategy(RNS.Link.ACCEPT_NONE)
        link.set_resource_callback(self._resource_callback)
        link.set_resource_started_callback(self._resource_started)
        link.set_resource_concluded_callback(self._resource_concluded)

        if self._access_policy == "deny":
            self.log.warning("Rejecting file-transfer link: access policy is deny")
            link.teardown()
            return

        try:
            self.manage_link(link)
        except RuntimeError:
            link.teardown()
            return

        key = self._link_key(link)
        with self._lock:
            if not self._active:
                should_close = True
            else:
                should_close = False
                self._links[key] = link
        if should_close:
            link.teardown()
            return

        link.set_link_closed_callback(self._link_closed)
        if self._access_policy == "open":
            with self._lock:
                if self._active and self._links.get(key) is link:
                    self._authorized_links.add(key)
                    authorized = True
                else:
                    authorized = False
            if not authorized:
                link.teardown()
                return
            link.set_resource_strategy(RNS.Link.ACCEPT_APP)
        else:
            link.set_remote_identified_callback(self._check_identity)

    def _check_identity(self, link: Any, identity: Any) -> None:
        identity_hash = getattr(identity, "hash", None)
        if (
            not self._active
            or self._access_policy != "allowlist"
            or identity_hash not in self._allowed_hashes
        ):
            self.log.warning(
                "Rejecting file transfer from unauthorized identity: %s",
                RNS.prettyhexrep(identity_hash) if identity_hash is not None else "unknown",
            )
            link.teardown()
            return
        key = self._link_key(link)
        with self._lock:
            if self._active and self._links.get(key) is link:
                self._authorized_links.add(key)
                authorized = True
            else:
                authorized = False
        if not authorized:
            link.teardown()
            return
        link.set_resource_strategy(RNS.Link.ACCEPT_APP)
        self.log.info("Authorized file-transfer identity %s", RNS.prettyhexrep(identity_hash))

    def _link_closed(self, link: Any) -> None:
        key = self._link_key(link)
        with self._lock:
            self._links.pop(key, None)
            self._authorized_links.discard(key)

    @staticmethod
    def _link_key(link_or_id: Any) -> Any:
        link_id = getattr(link_or_id, "link_id", link_or_id)
        if isinstance(link_id, bytearray):
            return bytes(link_id)
        return link_id

    def _is_authorized_link(self, link_or_id: Any) -> bool:
        if not self._active or self._access_policy == "deny":
            return False
        key = self._link_key(link_or_id)
        with self._lock:
            return key in self._links and key in self._authorized_links

    def _is_authorized_request(self, link_id: Any, remote_identity: Any) -> bool:
        if not self._is_authorized_link(link_id):
            return False
        if self._access_policy == "allowlist":
            return (
                remote_identity is not None
                and getattr(remote_identity, "hash", None) in self._allowed_hashes
            )
        return self._access_policy == "open"

    def _resource_callback(self, resource: Any) -> bool:
        """Decide whether to accept an incoming resource."""
        if not self._is_authorized_link(getattr(resource, "link", None)):
            self.log.warning("Rejecting resource from unauthorized link")
            return False
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
            self.log.debug("Disk space check failed", exc_info=True)

        auto_accept = self.config.get("auto_accept", True)
        if auto_accept:
            self.log.info("Accepting incoming file (%d bytes)", resource.size)
            return True

        self.log.info("Rejecting incoming file (auto_accept disabled)")
        return False

    def _resource_started(self, resource: Any) -> None:
        if not self._is_authorized_link(getattr(resource, "link", None)):
            self.log.warning("Ignoring resource start from unauthorized link")
            cancel = getattr(resource, "cancel", None)
            if callable(cancel):
                cancel()
            return
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

        if not self._is_authorized_link(getattr(resource, "link", None)):
            self.log.warning("Discarding resource conclusion from unauthorized link")
            return

        if resource.status == RNS.Resource.COMPLETE:
            try:
                data = resource.data.read() if hasattr(resource.data, "read") else resource.data
                if not isinstance(data, bytes):
                    raise TypeError("received resource did not contain bytes")
                if len(data) > self._max_size:
                    raise ValueError("received resource exceeds configured size limit")
                filename, filepath = self._store_received_file(resource, data)
            except Exception:
                self.log.exception("Error saving received file")
                with self._lock:
                    self._transfers_failed += 1
                return

            with self._lock:
                self._transfers_completed += 1
            self.log.info("File received: %s (%d bytes)", filename, len(data))
            self.event_bus.publish(
                events.FILE_RECEIVED,
                {
                    "filename": filename,
                    "size": len(data),
                    "path": filepath,
                },
            )
        else:
            with self._lock:
                self._transfers_failed += 1
            self.log.warning("File transfer failed (status: %s)", resource.status)

    # --- Request handlers ---

    def _handle_list(
        self,
        path: str,
        data: Any,
        request_id: Any,
        link_id: Any,
        remote_identity: Any,
        requested_at: Any,
    ) -> Any:
        import RNS.vendor.umsgpack as umsgpack

        if not self._is_authorized_request(link_id, remote_identity):
            return umsgpack.packb({"ok": False, "error": "unauthorized"})
        files = self._list_shared_files()
        return umsgpack.packb({"ok": True, "data": files})

    def _handle_info(
        self,
        path: str,
        data: Any,
        request_id: Any,
        link_id: Any,
        remote_identity: Any,
        requested_at: Any,
    ) -> Any:
        import RNS.vendor.umsgpack as umsgpack

        if not self._is_authorized_request(link_id, remote_identity):
            return umsgpack.packb({"ok": False, "error": "unauthorized"})
        if not isinstance(data, bytes):
            return umsgpack.packb({"ok": False, "error": "filename required"})
        try:
            req = umsgpack.unpackb(data)
            filename = req.get("name") if isinstance(req, dict) else None
        except Exception:
            return umsgpack.packb({"ok": False, "error": "invalid request"})
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            return umsgpack.packb({"ok": False, "error": "invalid filename"})

        # Prevent path traversal — basename + boundary check
        safe_name = os.path.basename(filename)
        filepath = os.path.join(self._shared_dir, safe_name)
        if not self._is_within_shared_dir(filepath):
            return umsgpack.packb({"ok": False, "error": "invalid filename"})
        if not os.path.isfile(filepath):
            return umsgpack.packb({"ok": False, "error": "file not found"})

        stat = os.lstat(filepath)  # lstat: don't follow symlinks
        return umsgpack.packb(
            {
                "ok": True,
                "data": {
                    "name": safe_name,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                },
            }
        )

    # --- Helpers ---

    def _is_within_shared_dir(self, filepath: str) -> bool:
        """Verify that the resolved path is within the shared directory.

        Resolves symlinks and normalises paths so that symlinks pointing
        outside the shared directory are rejected.
        """
        real_shared = os.path.realpath(self._shared_dir)
        real_file = os.path.realpath(filepath)
        # Ensure the resolved path is a child of the shared dir
        return real_file.startswith(real_shared + os.sep) or real_file == real_shared

    def _list_shared_files(self) -> list[dict[str, Any]]:
        files = []
        try:
            for entry in os.scandir(self._shared_dir):
                # Use lstat to avoid following symlinks; skip non-regular files
                if not entry.is_file(follow_symlinks=False):
                    continue
                # Reject symlinks pointing outside the shared directory
                full = os.path.join(self._shared_dir, entry.name)
                if entry.is_symlink() and not self._is_within_shared_dir(full):
                    self.log.warning("Skipping symlink outside shared dir: %s", entry.name)
                    continue
                stat = entry.stat(follow_symlinks=False)
                files.append(
                    {
                        "name": entry.name,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    }
                )
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
                if counter > 9999:
                    name = f"received_{int(time.time())}_{resource.size}b{ext}"
                    break
            else:
                name = f"{base}_{counter}{ext}"

        # Final boundary check after constructing the name
        final_path = os.path.join(self._shared_dir, name)
        if not self._is_within_shared_dir(final_path):
            self.log.warning("Filename resolved outside shared dir, using fallback")
            name = f"received_{int(time.time())}_{resource.size}b"

        return name

    def _store_received_file(self, resource: Any, data: bytes) -> tuple[str, str]:
        """Durably publish received bytes without following or replacing links."""

        preferred = self._safe_filename(resource)
        base, extension = os.path.splitext(preferred)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(self._shared_dir, directory_flags)
        temporary_name = f".reticulumpi-upload-{secrets.token_hex(16)}.tmp"
        temporary_fd: int | None = None
        published_name: str | None = None
        try:
            file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            file_flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_fd = os.open(
                temporary_name,
                file_flags,
                0o600,
                dir_fd=directory_fd,
            )
            view = memoryview(data)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise OSError("short write while persisting received file")
                view = view[written:]
            os.fsync(temporary_fd)
            os.close(temporary_fd)
            temporary_fd = None

            for counter in range(10_000):
                candidate = preferred if counter == 0 else f"{base}_{counter}{extension}"
                try:
                    os.link(
                        temporary_name,
                        candidate,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    continue
                published_name = candidate
                break
            if published_name is None:
                raise OSError(errno.EEXIST, "could not reserve a unique received filename")
            os.fsync(directory_fd)
            os.unlink(temporary_name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except BaseException:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(directory_fd)

        return published_name, os.path.join(self._shared_dir, published_name)
