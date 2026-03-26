"""Persistent Reticulum identity management."""

import logging
import os

import RNS

log = logging.getLogger(__name__)


def load_or_create(identity_path: str) -> RNS.Identity:
    """Load an existing identity from disk, or create and save a new one.

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
        log.warning("Failed to load identity from %s, creating new one", identity_path)

    identity = RNS.Identity()
    identity.to_file(identity_path)
    log.info("Created new identity and saved to %s", identity_path)
    return identity
