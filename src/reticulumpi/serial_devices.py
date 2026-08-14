"""Canonical serial-device identity and process-local ownership claims.

This module deliberately does not open serial ports or reset USB devices.  It
provides the small safety primitive needed by those operations:

* resolve a configured path to its current character-device endpoint;
* capture the immutable USB-parent attributes exposed by Linux sysfs;
* prevent independent owners from claiming aliases or sibling interfaces of
  the same physical USB device; and
* revalidate a lease immediately before a destructive or recovery operation.

``SerialDeviceRegistry`` is thread-safe.  Claims are versioned, so releasing a
stale lease can never release a later claim (the usual ABA race).  Sharing is
possible only with a capability token issued by an active, non-external lease.
External reservations model devices owned by another process, such as an RNode
opened by ``rnsd``.
"""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

__all__ = [
    "InvalidSerialShareTokenError",
    "SerialDeviceBusyError",
    "SerialDeviceChangedError",
    "SerialDeviceIdentity",
    "SerialDeviceIdentityError",
    "SerialDeviceIntentLease",
    "SerialDeviceLease",
    "SerialDeviceRegistry",
    "SerialDeviceShareToken",
    "StaleSerialDeviceLeaseError",
    "USBDeviceIdentity",
    "resolve_serial_device",
    "serial_device_registry",
    "validate_stable_serial_path",
]


_INDEXED_USB_TTY_RE = re.compile(r"^/dev/tty(?:ACM|USB)\d+$")


def validate_stable_serial_path(value: object, field: str = "serial_port") -> str:
    """Return an explicit stable ``/dev`` path for a physical serial device.

    Kernel-assigned USB indexes change when devices re-enumerate.  Requiring a
    persistent by-id/by-path entry or a dedicated udev alias prevents one
    radio plugin from silently binding to another protocol's device.
    """

    if not isinstance(value, str):
        valid = False
    else:
        # Reject lexical aliases instead of silently normalizing them.  In
        # particular, normalization must never turn a traversal or doubled
        # slash spelling of /dev/ttyUSB<n> or /dev/ttyACM<n> into an accepted
        # stable-looking path.
        normalized = os.path.normpath(value)
        valid = (
            bool(value)
            and "\x00" not in value
            and value == value.strip()
            and value == normalized
            and value.casefold() != "auto"
            and value.startswith("/dev/")
            and _INDEXED_USB_TTY_RE.fullmatch(value) is None
        )
    if not valid:
        raise ValueError(
            f"{field} requires an explicit stable serial device path; "
            "use /dev/serial/by-id, /dev/serial/by-path, or a dedicated udev alias "
            "instead of auto or a kernel-assigned ttyUSB/ttyACM index"
        )
    return value


class SerialDeviceIdentityError(RuntimeError):
    """A configured path cannot be resolved to a usable serial identity."""


class SerialDeviceBusyError(RuntimeError):
    """A resolved device collides with one or more active claims."""

    def __init__(
        self,
        configured_path: str,
        owners: tuple[str, ...],
        identity: SerialDeviceIdentity | None,
        *,
        external: bool,
    ) -> None:
        owner_text = ", ".join(repr(owner) for owner in owners)
        qualifier = "external reservation" if external else "claim"
        super().__init__(
            f"Serial device {configured_path!r} conflicts with active {qualifier} "
            f"owned by {owner_text}"
        )
        self.identity = identity
        self.owners = owners
        self.external = external


class StaleSerialDeviceLeaseError(RuntimeError):
    """A lease is no longer the active version of its claim."""


class SerialDeviceChangedError(RuntimeError):
    """A configured path no longer resolves to the identity held by a lease."""

    def __init__(
        self,
        configured_path: str,
        expected: SerialDeviceIdentity,
        current: SerialDeviceIdentity,
    ) -> None:
        super().__init__(
            f"Serial device {configured_path!r} changed identity while its lease was active"
        )
        self.expected = expected
        self.current = current


class InvalidSerialShareTokenError(RuntimeError):
    """A share token is stale, foreign, external, or targets another device."""


@dataclass(frozen=True)
class USBDeviceIdentity:
    """Stable attributes of the physical USB parent of a tty endpoint.

    ``sysfs_path`` identifies the physical port in the current device tree.
    ``serial_number`` may be absent on inexpensive adapters; callers that need
    device-level (rather than physical-port-level) assurance can require it in
    their configuration policy.
    """

    sysfs_path: str
    vendor_id: str
    product_id: str
    serial_number: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "vendor_id", self.vendor_id.strip().lower())
        object.__setattr__(self, "product_id", self.product_id.strip().lower())
        serial_number = self.serial_number.strip() if self.serial_number else None
        object.__setattr__(self, "serial_number", serial_number or None)


_ClaimKey: TypeAlias = tuple[str, object]


@dataclass(frozen=True)
class SerialDeviceIdentity:
    """One immutable resolution of a configured serial path."""

    configured_path: str
    canonical_path: str
    major: int
    minor: int
    usb: USBDeviceIdentity | None = None

    @property
    def endpoint(self) -> tuple[str, int, int]:
        """Return the canonical endpoint binding used during revalidation."""

        return (self.canonical_path, self.major, self.minor)

    @property
    def claim_keys(self) -> frozenset[_ClaimKey]:
        """Return every identity key that must be exclusive.

        Both path and major/minor are retained because either one can expose an
        alias collision the other misses.  The USB-parent key conservatively
        prevents separate tty interfaces from independently resetting the same
        composite USB device.
        """

        keys: set[_ClaimKey] = {
            ("path", self.canonical_path),
            ("device", (self.major, self.minor)),
        }
        if self.usb is not None:
            keys.add(("usb_parent", self.usb.sysfs_path))
            if self.usb.serial_number is not None:
                keys.add(
                    (
                        "usb_device",
                        (
                            self.usb.vendor_id,
                            self.usb.product_id,
                            self.usb.serial_number,
                        ),
                    )
                )
        return frozenset(keys)

    @property
    def binding(self) -> tuple[str, int, int, USBDeviceIdentity | None]:
        """Return all captured facts that must match during revalidation."""

        return (self.canonical_path, self.major, self.minor, self.usb)

    def is_same_endpoint(self, other: SerialDeviceIdentity) -> bool:
        """Return whether *other* is the exact endpoint represented here."""

        return self.binding == other.binding


def _normalize_configured_path(configured_path: str | os.PathLike[str]) -> str:
    """Return a stable lexical key without requiring the path to exist."""

    configured = os.fspath(configured_path)
    if not configured or "\x00" in configured:
        raise ValueError("configured_path must be a non-empty filesystem path")
    expanded = os.path.expanduser(configured)
    return os.path.normcase(os.path.abspath(os.path.normpath(expanded)))


def _read_sysfs_attribute(path: Path, name: str, *, required: bool) -> str | None:
    attribute = path / name
    try:
        value = attribute.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        if not required:
            return None
        raise SerialDeviceIdentityError(
            f"USB sysfs identity is incomplete at {path}: missing {name}"
        ) from None
    except (OSError, UnicodeError) as exc:
        raise SerialDeviceIdentityError(
            f"Cannot read USB sysfs identity attribute {name} at {path}: {exc}"
        ) from exc
    if required and not value:
        raise SerialDeviceIdentityError(f"USB sysfs identity is incomplete at {path}: empty {name}")
    return value or None


def _sysfs_attribute_exists(path: Path) -> bool:
    """Check attribute presence without turning permission errors into absence."""

    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SerialDeviceIdentityError(
            f"Cannot inspect USB sysfs identity at {path}: {exc}"
        ) from exc
    return True


def _resolve_usb_parent(
    major: int,
    minor: int,
    *,
    sysfs_root: str | os.PathLike[str],
) -> USBDeviceIdentity | None:
    char_link = Path(sysfs_root) / "dev" / "char" / f"{major}:{minor}"
    try:
        endpoint = char_link.resolve(strict=True)
    except FileNotFoundError:
        # Non-Linux platforms and non-USB ttys legitimately have no sysfs
        # mapping.  The canonical endpoint and major/minor remain useful.
        return None
    except OSError as exc:
        raise SerialDeviceIdentityError(
            f"Cannot resolve sysfs identity for character device {major}:{minor}: {exc}"
        ) from exc

    for candidate in (endpoint, *endpoint.parents):
        vendor_path = candidate / "idVendor"
        product_path = candidate / "idProduct"
        if not _sysfs_attribute_exists(vendor_path) and not _sysfs_attribute_exists(product_path):
            continue
        vendor_id = _read_sysfs_attribute(candidate, "idVendor", required=True)
        product_id = _read_sysfs_attribute(candidate, "idProduct", required=True)
        serial_number = _read_sysfs_attribute(candidate, "serial", required=False)
        assert vendor_id is not None and product_id is not None
        return USBDeviceIdentity(
            sysfs_path=str(candidate),
            vendor_id=vendor_id,
            product_id=product_id,
            serial_number=serial_number,
        )
    return None


def resolve_serial_device(
    configured_path: str | os.PathLike[str],
    *,
    sysfs_root: str | os.PathLike[str] = "/sys",
) -> SerialDeviceIdentity:
    """Resolve an existing serial path without opening it.

    The path must currently identify a character device.  Linux USB metadata is
    discovered by walking from ``/sys/dev/char/<major>:<minor>`` to the nearest
    ancestor exposing ``idVendor`` and ``idProduct``.  No bus/device numbers are
    captured because they are transient and unsafe as physical identity.
    """

    configured = os.fspath(configured_path)
    if not configured or "\x00" in configured:
        raise ValueError("configured_path must be a non-empty filesystem path")
    try:
        canonical = os.path.realpath(configured, strict=True)
        device_stat = os.stat(canonical)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise SerialDeviceIdentityError(f"Serial device {configured!r} does not exist") from exc
    except OSError as exc:
        raise SerialDeviceIdentityError(
            f"Cannot inspect serial device {configured!r}: {exc}"
        ) from exc

    if not stat.S_ISCHR(device_stat.st_mode):
        raise SerialDeviceIdentityError(f"Serial device {configured!r} is not a character device")

    major = os.major(device_stat.st_rdev)
    minor = os.minor(device_stat.st_rdev)
    usb = _resolve_usb_parent(major, minor, sysfs_root=sysfs_root)
    return SerialDeviceIdentity(configured, canonical, major, minor, usb)


@dataclass(frozen=True, repr=False)
class SerialDeviceShareToken:
    """Opaque capability authorizing an intentional claim of one endpoint."""

    _registry_nonce: object = field(repr=False)
    _group_id: int = field(repr=False)
    _group_nonce: object = field(repr=False)

    def __repr__(self) -> str:
        return "<SerialDeviceShareToken>"


@dataclass(frozen=True)
class _ClaimRecord:
    claim_id: int
    group_id: int
    owner: str
    identity: SerialDeviceIdentity
    external: bool


@dataclass
class _ShareGroup:
    identity: SerialDeviceIdentity
    claim_ids: set[int]
    share_nonce: object
    external: bool


@dataclass(frozen=True)
class _IntentRecord:
    intent_id: int
    owner: str
    configured_path: str
    normalized_path: str
    identity: SerialDeviceIdentity | None = None
    claim_keys: frozenset[_ClaimKey] = frozenset()


@dataclass(frozen=True)
class SerialDeviceLease:
    """Versioned ownership token for one serial-device claim."""

    identity: SerialDeviceIdentity
    owner: str
    external: bool = False
    _registry: SerialDeviceRegistry | None = field(repr=False, compare=False, default=None)
    _claim_id: int = field(repr=False, compare=False, default=0)

    def release(self) -> None:
        """Release exactly this lease version; repeated or stale release is safe."""

        if self._registry is not None and self._claim_id:
            self._registry._release(self._claim_id)

    def revalidate(self) -> SerialDeviceIdentity:
        """Resolve the configured path again and fail closed if it changed."""

        if self._registry is None or not self._claim_id:
            raise StaleSerialDeviceLeaseError("Serial-device lease is not registry-backed")
        return self._registry.revalidate(self)

    def issue_share_token(self) -> SerialDeviceShareToken:
        """Issue a capability permitting an intentional claim of this endpoint."""

        if self._registry is None or not self._claim_id:
            raise StaleSerialDeviceLeaseError("Serial-device lease is not registry-backed")
        return self._registry.issue_share_token(self)


@dataclass(frozen=True)
class SerialDeviceIntentLease:
    """External path reservation that remains valid while its tty is absent."""

    configured_path: str
    normalized_path: str
    owner: str
    external: bool = True
    _registry: SerialDeviceRegistry | None = field(repr=False, compare=False, default=None)
    _intent_id: int = field(repr=False, compare=False, default=0)

    @property
    def identity(self) -> SerialDeviceIdentity | None:
        """Return the last resolved identity, or ``None`` while pending."""

        if self._registry is None or not self._intent_id:
            raise StaleSerialDeviceLeaseError("Serial-device intent lease is not registry-backed")
        return self._registry.intent_identity(self)

    @property
    def pending(self) -> bool:
        return self.identity is None

    def revalidate(self) -> SerialDeviceIdentity | None:
        """Refresh the intent's identity without giving up path ownership."""

        if self._registry is None or not self._intent_id:
            raise StaleSerialDeviceLeaseError("Serial-device intent lease is not registry-backed")
        return self._registry.revalidate_intent(self)

    def release(self) -> None:
        if self._registry is not None and self._intent_id:
            self._registry._release_intent(self._intent_id)

    def issue_share_token(self) -> SerialDeviceShareToken:
        raise InvalidSerialShareTokenError("External serial-device reservations cannot be shared")


class SerialDeviceRegistry:
    """Thread-safe registry of canonical serial-device claims."""

    def __init__(self, *, sysfs_root: str | os.PathLike[str] = "/sys") -> None:
        self._sysfs_root = os.fspath(sysfs_root)
        self._lock = threading.RLock()
        self._registry_nonce = object()
        self._next_claim_id = 0
        self._next_group_id = 0
        self._next_intent_id = 0
        self._claims: dict[int, _ClaimRecord] = {}
        self._claims_by_key: dict[_ClaimKey, set[int]] = {}
        self._groups: dict[int, _ShareGroup] = {}
        self._intents: dict[int, _IntentRecord] = {}
        self._intents_by_path: dict[str, set[int]] = {}
        self._intents_by_key: dict[_ClaimKey, set[int]] = {}

    def _resolve(self, configured_path: str | os.PathLike[str]) -> SerialDeviceIdentity:
        return resolve_serial_device(configured_path, sysfs_root=self._sysfs_root)

    def claim(
        self,
        configured_path: str | os.PathLike[str],
        owner: str,
        *,
        share_token: SerialDeviceShareToken | None = None,
    ) -> SerialDeviceLease:
        """Resolve and atomically claim one device for *owner*.

        A claim conflicts when any canonical path, character-device number, or
        physical USB parent is already claimed.  A valid share token permits an
        additional claim only for the exact endpoint that issued the token.
        """

        return self._claim(configured_path, owner, external=False, share_token=share_token)

    def reserve_external(
        self,
        configured_path: str | os.PathLike[str],
        owner: str,
    ) -> SerialDeviceLease:
        """Reserve a device already owned by another process.

        External reservations are exclusive and cannot issue sharing tokens.
        The returned lease must remain alive for as long as the external owner
        may access the device.
        """

        return self._claim(configured_path, owner, external=True, share_token=None)

    def reserve_external_intent(
        self,
        configured_path: str | os.PathLike[str],
        owner: str,
    ) -> SerialDeviceIntentLease:
        """Reserve an external owner's configured path before it exists.

        The normalized configured path is exclusive immediately.  If the path
        resolves now, its endpoint and USB-parent keys are exclusive too.  A
        pending reservation is refreshed before every later claim, making a
        hot-plugged device exclusive before that claim can be granted.
        """

        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")
        owner = owner.strip()
        configured = os.fspath(configured_path)
        normalized = _normalize_configured_path(configured)
        try:
            identity = self._resolve(configured)
        except SerialDeviceIdentityError:
            identity = None

        with self._lock:
            self._refresh_intents_locked()
            path_collisions = self._intent_records_for_path_locked(normalized)
            if path_collisions:
                self._raise_intent_busy(configured, identity, path_collisions)

            if identity is not None:
                claim_collisions = self._claim_records_for_identity_locked(identity)
                if claim_collisions:
                    self._raise_busy(identity, claim_collisions)
                intent_collisions = self._intent_records_for_identity_locked(identity)
                if intent_collisions:
                    self._raise_intent_busy(configured, identity, intent_collisions)

            self._next_intent_id += 1
            intent_id = self._next_intent_id
            record = _IntentRecord(
                intent_id,
                owner,
                configured,
                normalized,
                identity,
                identity.claim_keys if identity is not None else frozenset(),
            )
            self._intents[intent_id] = record
            self._intents_by_path.setdefault(normalized, set()).add(intent_id)
            if identity is not None:
                self._index_intent_identity_locked(intent_id, identity)

        return SerialDeviceIntentLease(configured, normalized, owner, True, self, intent_id)

    def _claim(
        self,
        configured_path: str | os.PathLike[str],
        owner: str,
        *,
        external: bool,
        share_token: SerialDeviceShareToken | None,
    ) -> SerialDeviceLease:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")
        owner = owner.strip()
        configured = os.fspath(configured_path)
        normalized = _normalize_configured_path(configured)

        # A configured-path intent must win even while the path is absent.  In
        # particular, callers must see a busy error rather than mistaking an
        # externally owned hot-plug endpoint for an unowned missing device.
        with self._lock:
            path_collisions = self._intent_records_for_path_locked(normalized)
            if path_collisions:
                self._raise_intent_busy(configured, None, path_collisions)

        identity = self._resolve(configured)

        with self._lock:
            # Close the race with an intent created after the pre-resolution
            # path check, and promote hot-plugged intents before deciding that
            # an alias or sibling USB interface is available.
            self._refresh_intents_locked()
            path_collisions = self._intent_records_for_path_locked(normalized)
            if path_collisions:
                self._raise_intent_busy(configured, identity, path_collisions)
            intent_collisions = self._intent_records_for_identity_locked(identity)
            if intent_collisions:
                self._raise_intent_busy(configured, identity, intent_collisions)

            colliding = self._claim_records_for_identity_locked(identity)

            if share_token is None:
                if colliding:
                    self._raise_busy(identity, colliding)
                group_id = self._new_group(identity, external=external)
            else:
                if external:
                    raise InvalidSerialShareTokenError(
                        "External serial-device reservations cannot be shared"
                    )
                group_id = self._validate_share_token(share_token, identity, colliding)

            self._next_claim_id += 1
            claim_id = self._next_claim_id
            record = _ClaimRecord(claim_id, group_id, owner, identity, external)
            self._claims[claim_id] = record
            self._groups[group_id].claim_ids.add(claim_id)
            for key in identity.claim_keys:
                self._claims_by_key.setdefault(key, set()).add(claim_id)

        return SerialDeviceLease(identity, owner, external, self, claim_id)

    def _claim_records_for_identity_locked(
        self,
        identity: SerialDeviceIdentity,
    ) -> list[_ClaimRecord]:
        colliding_ids: set[int] = set()
        for key in identity.claim_keys:
            colliding_ids.update(self._claims_by_key.get(key, ()))
        return [self._claims[claim_id] for claim_id in sorted(colliding_ids)]

    def _intent_records_for_path_locked(self, normalized_path: str) -> list[_IntentRecord]:
        return [
            self._intents[intent_id]
            for intent_id in sorted(self._intents_by_path.get(normalized_path, ()))
        ]

    def _intent_records_for_identity_locked(
        self,
        identity: SerialDeviceIdentity,
    ) -> list[_IntentRecord]:
        colliding_ids: set[int] = set()
        for key in identity.claim_keys:
            colliding_ids.update(self._intents_by_key.get(key, ()))
        return [self._intents[intent_id] for intent_id in sorted(colliding_ids)]

    def _index_intent_identity_locked(
        self,
        intent_id: int,
        identity: SerialDeviceIdentity,
    ) -> None:
        for key in identity.claim_keys:
            self._intents_by_key.setdefault(key, set()).add(intent_id)

    def _unindex_intent_keys_locked(
        self,
        intent_id: int,
        claim_keys: frozenset[_ClaimKey],
    ) -> None:
        for key in claim_keys:
            intent_ids = self._intents_by_key.get(key)
            if intent_ids is None:
                continue
            intent_ids.discard(intent_id)
            if not intent_ids:
                self._intents_by_key.pop(key, None)

    def _refresh_intents_locked(self) -> None:
        """Refresh every live intent while claim admission is serialized.

        A failed resolution retains all known identity keys.  That is the
        fail-closed choice for a transiently missing symlink: disappearance of
        one configured alias must not expose the same physical radio through a
        different alias.  Successful identity changes accumulate keys because
        the external owner may still hold the old endpoint's file descriptor.
        """

        for intent_id in tuple(sorted(self._intents)):
            record = self._intents.get(intent_id)
            if record is None:
                continue
            try:
                current = self._resolve(record.configured_path)
            except SerialDeviceIdentityError:
                continue
            if record.identity is not None and record.identity.binding == current.binding:
                continue
            claim_keys = record.claim_keys | current.claim_keys
            updated = _IntentRecord(
                record.intent_id,
                record.owner,
                record.configured_path,
                record.normalized_path,
                current,
                claim_keys,
            )
            self._intents[intent_id] = updated
            self._index_intent_identity_locked(intent_id, current)

    def _new_group(self, identity: SerialDeviceIdentity, *, external: bool) -> int:
        self._next_group_id += 1
        group_id = self._next_group_id
        self._groups[group_id] = _ShareGroup(identity, set(), object(), external)
        return group_id

    def _validate_share_token(
        self,
        token: SerialDeviceShareToken,
        identity: SerialDeviceIdentity,
        colliding: list[_ClaimRecord],
    ) -> int:
        if not isinstance(token, SerialDeviceShareToken):
            raise InvalidSerialShareTokenError("Invalid serial-device share token")
        group = self._groups.get(token._group_id)
        if (
            token._registry_nonce is not self._registry_nonce
            or group is None
            or token._group_nonce is not group.share_nonce
            or not group.claim_ids
            or group.external
            or not identity.is_same_endpoint(group.identity)
            or not colliding
            or any(record.group_id != token._group_id for record in colliding)
        ):
            raise InvalidSerialShareTokenError(
                "Serial-device share token is stale, foreign, or targets another endpoint"
            )
        return token._group_id

    @staticmethod
    def _raise_busy(identity: SerialDeviceIdentity, colliding: list[_ClaimRecord]) -> None:
        owners = tuple(dict.fromkeys(record.owner for record in colliding))
        raise SerialDeviceBusyError(
            identity.configured_path,
            owners,
            identity,
            external=any(record.external for record in colliding),
        )

    @staticmethod
    def _raise_intent_busy(
        configured_path: str,
        identity: SerialDeviceIdentity | None,
        colliding: list[_IntentRecord],
    ) -> None:
        owners = tuple(dict.fromkeys(record.owner for record in colliding))
        raise SerialDeviceBusyError(
            configured_path,
            owners,
            identity,
            external=True,
        )

    def issue_share_token(self, lease: SerialDeviceLease) -> SerialDeviceShareToken:
        """Return an opaque share capability for an active local lease."""

        with self._lock:
            record = self._active_record_for(lease)
            group = self._groups[record.group_id]
            if record.external or group.external:
                raise InvalidSerialShareTokenError(
                    "External serial-device reservations cannot be shared"
                )
            return SerialDeviceShareToken(
                self._registry_nonce,
                record.group_id,
                group.share_nonce,
            )

    def revalidate(self, lease: SerialDeviceLease) -> SerialDeviceIdentity:
        """Verify that an active lease still names the exact captured endpoint."""

        with self._lock:
            record = self._active_record_for(lease)
            configured_path = record.identity.configured_path
            expected = record.identity

        # Resolution performs filesystem I/O, so do it outside the registry
        # lock.  The second active-record check closes the release/reclaim race.
        current = self._resolve(configured_path)
        with self._lock:
            record = self._active_record_for(lease)
            if record.identity.binding != expected.binding:
                raise StaleSerialDeviceLeaseError("Serial-device lease changed while validating")
            if not expected.is_same_endpoint(current):
                raise SerialDeviceChangedError(configured_path, expected, current)
        return current

    def intent_identity(self, lease: SerialDeviceIntentLease) -> SerialDeviceIdentity | None:
        """Return the last identity captured for an active intent lease."""

        with self._lock:
            return self._active_intent_for(lease).identity

    def revalidate_intent(
        self,
        lease: SerialDeviceIntentLease,
    ) -> SerialDeviceIdentity | None:
        """Refresh an intent while preserving its configured-path ownership."""

        with self._lock:
            record = self._active_intent_for(lease)
            configured_path = record.configured_path
        try:
            current = self._resolve(configured_path)
        except SerialDeviceIdentityError:
            # Preserve any last-known physical keys so a transiently missing
            # alias cannot weaken an external reservation.
            with self._lock:
                return self._active_intent_for(lease).identity

        with self._lock:
            record = self._active_intent_for(lease)
            if record.identity is None or record.identity.binding != current.binding:
                claim_keys = record.claim_keys | current.claim_keys
                updated = _IntentRecord(
                    record.intent_id,
                    record.owner,
                    record.configured_path,
                    record.normalized_path,
                    current,
                    claim_keys,
                )
                self._intents[record.intent_id] = updated
                self._index_intent_identity_locked(record.intent_id, current)
            return current

    def _active_record_for(self, lease: SerialDeviceLease) -> _ClaimRecord:
        if lease._registry is not self:
            raise StaleSerialDeviceLeaseError("Serial-device lease belongs to another registry")
        record = self._claims.get(lease._claim_id)
        if record is None or record.owner != lease.owner or record.identity != lease.identity:
            raise StaleSerialDeviceLeaseError("Serial-device lease is stale")
        return record

    def _active_intent_for(self, lease: SerialDeviceIntentLease) -> _IntentRecord:
        if lease._registry is not self:
            raise StaleSerialDeviceLeaseError(
                "Serial-device intent lease belongs to another registry"
            )
        record = self._intents.get(lease._intent_id)
        if (
            record is None
            or record.owner != lease.owner
            or record.configured_path != lease.configured_path
            or record.normalized_path != lease.normalized_path
        ):
            raise StaleSerialDeviceLeaseError("Serial-device intent lease is stale")
        return record

    def _release(self, claim_id: int) -> None:
        """Release one exact claim id.  Unknown ids are intentionally harmless."""

        with self._lock:
            record = self._claims.pop(claim_id, None)
            if record is None:
                return
            for key in record.identity.claim_keys:
                claim_ids = self._claims_by_key.get(key)
                if claim_ids is None:
                    continue
                claim_ids.discard(claim_id)
                if not claim_ids:
                    self._claims_by_key.pop(key, None)
            group = self._groups.get(record.group_id)
            if group is not None:
                group.claim_ids.discard(claim_id)
                if not group.claim_ids:
                    self._groups.pop(record.group_id, None)

    def _release_intent(self, intent_id: int) -> None:
        """Release one exact intent id; repeated releases are harmless."""

        with self._lock:
            record = self._intents.pop(intent_id, None)
            if record is None:
                return
            intent_ids = self._intents_by_path.get(record.normalized_path)
            if intent_ids is not None:
                intent_ids.discard(intent_id)
                if not intent_ids:
                    self._intents_by_path.pop(record.normalized_path, None)
            if record.claim_keys:
                self._unindex_intent_keys_locked(intent_id, record.claim_keys)

    def claim_count(self) -> int:
        """Return the number of active claims without exposing device identity."""

        with self._lock:
            return len(self._claims) + len(self._intents)


# Process-global registry used by future application/plugin integration.  Tests
# and isolated consumers can instantiate their own registry.
serial_device_registry = SerialDeviceRegistry()
