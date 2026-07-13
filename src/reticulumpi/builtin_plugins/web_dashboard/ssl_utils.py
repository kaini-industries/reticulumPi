"""Optional self-signed certificate generation for HTTPS."""

from __future__ import annotations

import datetime
import fcntl
import ipaddress
import logging
import os
import socket
import stat
import tempfile


def _collect_san_strings(extra_sans: list[str] | None) -> list[str]:
    """Build a de-duplicated, deterministically-ordered list of SAN strings.

    Covers loopback, the host's own name and ``.local`` alias, best-effort LAN
    IPs, and any caller-supplied ``extra_sans``. All discovery is wrapped in
    try/except so a flaky resolver never blocks cert generation. The UDP-connect
    trick sends no packets — ``connect()`` on a datagram socket only fixes the
    local source address used by the routing table.
    """
    sans: list[str] = ["localhost", "127.0.0.1", "::1"]

    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    if hostname:
        sans.append(hostname)
        sans.append(f"{hostname}.local")

    # Best-effort LAN IP via the default route (no packets actually sent).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            sans.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    # Best-effort: resolve all addresses the hostname maps to. Skip scoped
    # (zone-id) and link-local addresses — TLS clients can't match them, so
    # they'd only clutter the SAN list.
    if hostname:
        try:
            for info in socket.getaddrinfo(hostname, None):
                addr = info[4][0]
                if "%" in addr:
                    continue
                try:
                    if ipaddress.ip_address(addr).is_link_local:
                        continue
                except ValueError:
                    pass
                sans.append(addr)
        except OSError:
            pass

    for entry in extra_sans or []:
        if isinstance(entry, str) and entry:
            sans.append(entry)

    # De-dup while preserving first-seen order for deterministic certs.
    seen: set[str] = set()
    ordered: list[str] = []
    for s_val in sans:
        if s_val not in seen:
            seen.add(s_val)
            ordered.append(s_val)
    return ordered


def generate_self_signed_cert(
    cert_dir: str,
    common_name: str,
    log: logging.Logger | None = None,
    extra_sans: list[str] | None = None,
    *,
    now: datetime.datetime | None = None,
) -> tuple[str, str]:
    """Generate or renew an atomically published managed TLS bundle.

    Returns ``(bundle_path, bundle_path)``.  The mode-0600 PEM bundle contains
    both the certificate and private key so renewal has one atomic commit point
    instead of a crash window between two independent path replacements.
    Requires the `cryptography` package (transitive dependency of rns).

    The certificate's SubjectAlternativeName covers localhost, loopback IPs,
    the host's own name plus its ``.local`` mDNS alias, best-effort LAN IPs,
    and any ``extra_sans`` supplied via config — so browsers reaching the
    dashboard by any of those names don't trip a name-mismatch error.
    """
    cert_dir = os.path.expanduser(cert_dir)
    os.makedirs(cert_dir, exist_ok=True)

    bundle_path = os.path.join(cert_dir, "dashboard.pem")
    legacy_cert_path = os.path.join(cert_dir, "dashboard.crt")
    legacy_key_path = os.path.join(cert_dir, "dashboard.key")
    lock_path = os.path.join(cert_dir, ".dashboard-tls.lock")

    required_sans = _collect_san_strings(extra_sans)
    current_time = _normalise_now(now)

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Reuse only a matching bundle that remains valid for at least 30
        # days and covers the node's current names.
        if os.path.isfile(bundle_path):
            try:
                validate_cert_pair(
                    bundle_path,
                    bundle_path,
                    required_sans=required_sans,
                    min_valid_days=30,
                    now=current_time,
                )
            except ValueError as exc:
                if log:
                    log.warning("Renewing invalid or expiring TLS certificate: %s", exc)
            else:
                if log:
                    log.info("Reusing existing TLS certificate: %s", bundle_path)
                return bundle_path, bundle_path

        # One-time migration from the former two-file managed layout.  The
        # legacy files are preserved for recovery, but the listener uses only
        # the atomic bundle after this point.
        if os.path.isfile(legacy_cert_path) and os.path.isfile(legacy_key_path):
            try:
                validate_cert_pair(
                    legacy_cert_path,
                    legacy_key_path,
                    required_sans=required_sans,
                    min_valid_days=30,
                    now=current_time,
                )
            except ValueError as exc:
                if log:
                    log.warning("Replacing invalid or expiring legacy TLS certificate: %s", exc)
            else:
                with open(legacy_cert_path, "rb") as cert_file:
                    legacy_cert = cert_file.read()
                with open(legacy_key_path, "rb") as key_file:
                    legacy_key = key_file.read()
                _atomic_write(bundle_path, legacy_cert + legacy_key, 0o600)
                validate_cert_pair(
                    bundle_path,
                    bundle_path,
                    required_sans=required_sans,
                    min_valid_days=30,
                    now=current_time,
                )
                if log:
                    log.info("Migrated managed TLS material to atomic bundle: %s", bundle_path)
                return bundle_path, bundle_path

        if log:
            log.info("Generating self-signed TLS certificate for '%s'", common_name)

        cert_pem, key_pem, fingerprint = _build_self_signed_material(
            common_name,
            required_sans,
            current_time,
        )
        _atomic_write(bundle_path, cert_pem + key_pem, 0o600)
        validate_cert_pair(
            bundle_path,
            bundle_path,
            required_sans=required_sans,
            min_valid_days=30,
            now=current_time,
        )

        if log:
            log.warning(
                "Self-signed certificate generated. SHA-256 fingerprint: %s",
                fingerprint,
            )

        return bundle_path, bundle_path
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _normalise_now(now: datetime.datetime | None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _build_self_signed_material(
    common_name: str,
    required_sans: list[str],
    now: datetime.datetime,
) -> tuple[bytes, bytes, str]:
    """Return certificate PEM, key PEM, and SHA-256 fingerprint."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name or "ReticulumPi"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ReticulumPi"),
        ]
    )

    # Classify each SAN string as an IPAddress or DNSName by trying to parse
    # it as an IP; de-dup at the GeneralName level too in case discovery and
    # extra_sans overlap after IP normalisation.
    san_entries: list[x509.GeneralName] = []
    seen_san: set[str] = set()
    for value in required_sans:
        try:
            general: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(value))
            key_str = f"ip:{general.value}"
        except ValueError:
            general = x509.DNSName(value)
            key_str = f"dns:{value}"
        if key_str not in seen_san:
            seen_san.add(key_str)
            san_entries.append(general)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem, cert.fingerprint(hashes.SHA256()).hex(":")


def _read_tls_material(path: str, *, label: str, private: bool) -> bytes:
    """Read bounded TLS material from one safe, stable regular-file descriptor."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be loaded") from exc
    try:
        file_stat = os.fstat(descriptor)
        mode = stat.S_IMODE(file_stat.st_mode)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            raise ValueError(f"{label} must be a single-link regular file")
        if file_stat.st_uid not in {0, os.geteuid()}:
            raise ValueError(f"{label} has an unsafe owner")
        if mode & 0o022:
            raise ValueError(f"{label} may not be writable by group or other")
        if private and mode & 0o037:
            raise ValueError(
                "private key permissions may grant only owner access and optional group read"
            )

        chunks: list[bytes] = []
        remaining = 1024 * 1024 + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > 1024 * 1024:
            raise ValueError(f"{label} exceeds the 1 MiB safety limit")
        return payload
    finally:
        os.close(descriptor)


def validate_cert_pair(
    cert_path: str,
    key_path: str,
    *,
    required_sans: list[str] | None = None,
    min_valid_days: int = 1,
    now: datetime.datetime | None = None,
) -> datetime.datetime:
    """Validate TLS material and return its UTC expiry timestamp."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        cert = x509.load_pem_x509_certificate(
            _read_tls_material(cert_path, label="certificate", private=False)
        )
        key = serialization.load_pem_private_key(
            _read_tls_material(key_path, label="private key", private=True),
            password=None,
        )
    except (ValueError, TypeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(("certificate ", "private key ")):
            raise
        raise ValueError("certificate or key cannot be loaded") from exc

    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_pub != key_pub:
        raise ValueError("certificate and private key do not match")

    current_time = _normalise_now(now)
    not_before = getattr(cert, "not_valid_before_utc", None)
    if not_before is None:
        not_before = cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)
    if current_time < not_before:
        raise ValueError("certificate is not yet valid")

    expiry = getattr(cert, "not_valid_after_utc", None)
    if expiry is None:
        expiry = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    minimum_expiry = current_time + datetime.timedelta(days=min_valid_days)
    if expiry <= minimum_expiry:
        raise ValueError(f"certificate expires within {min_valid_days} days")

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as exc:
        raise ValueError("certificate has no SubjectAlternativeName") from exc
    if required_sans:
        actual = {str(value) for value in san.get_values_for_type(x509.DNSName)}
        actual.update(str(value) for value in san.get_values_for_type(x509.IPAddress))
        missing = set(required_sans).difference(actual)
        if missing:
            raise ValueError(f"certificate is missing SANs: {', '.join(sorted(missing))}")
    return expiry


def _atomic_write(path: str, data: bytes, mode: int) -> None:
    """Write and fsync a same-directory temporary file before replacement."""
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".tls-", dir=directory)
    try:
        os.fchmod(fd, mode)
        remaining = memoryview(data)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short write while publishing TLS material")
            remaining = remaining[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_path, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
