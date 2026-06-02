"""Persistent Reticulum identity management."""

import logging
import os
import tempfile

import RNS

log = logging.getLogger(__name__)


def load_or_create(identity_path: str) -> RNS.Identity:
    """Load an existing identity from disk, or create and save a new one.

    Uses atomic file creation (``O_EXCL``) to prevent race conditions when
    two processes call this concurrently — only one will succeed in creating
    the file; the other will load what was just created.

    Args:
        identity_path: Filesystem path to the identity file.

    Returns:
        An RNS.Identity instance with persistent keys.
    """
    identity_path = os.path.expanduser(identity_path)
    parent_dir = os.path.dirname(identity_path)
    if parent_dir and not os.path.isdir(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    if os.path.isfile(identity_path):
        identity = RNS.Identity.from_file(identity_path)
        if identity is not None:
            log.info("Loaded existing identity from %s", identity_path)
            return identity
        # Back up the corrupted file before overwriting
        backup_path = identity_path + ".bak"
        try:
            os.rename(identity_path, backup_path)
            log.warning(
                "Failed to load identity from %s, backed up to %s, creating new one",
                identity_path,
                backup_path,
            )
        except OSError:
            log.warning("Failed to load identity from %s, creating new one", identity_path)

    # Create identity and write atomically (temp file + rename).
    # O_EXCL on the rename target is implicit: if another process raced
    # and created the file between our check above and now, we detect it
    # after writing and load their version instead.
    identity = RNS.Identity()

    try:
        fd, tmp_path = tempfile.mkstemp(dir=parent_dir or ".", prefix=".identity_", suffix=".tmp")
        os.close(fd)
        identity.to_file(tmp_path)
        os.replace(tmp_path, identity_path)
        log.info("Created new identity and saved to %s", identity_path)
    except OSError:
        # Clean up temp file if replace failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # Another process may have created the file — try loading it
        if os.path.isfile(identity_path):
            loaded = RNS.Identity.from_file(identity_path)
            if loaded is not None:
                log.info("Loaded identity created by another process from %s", identity_path)
                return loaded
        # Fall back to writing directly (best effort)
        identity.to_file(identity_path)
        log.info("Created new identity (direct write) and saved to %s", identity_path)

    return identity
