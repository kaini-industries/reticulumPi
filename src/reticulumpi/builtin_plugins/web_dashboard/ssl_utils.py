"""Optional self-signed certificate generation for HTTPS."""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket


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
) -> tuple[str, str]:
    """Generate a self-signed TLS certificate and private key.

    Returns (cert_path, key_path).
    Requires the `cryptography` package (transitive dependency of rns).

    The certificate's SubjectAlternativeName covers localhost, loopback IPs,
    the host's own name plus its ``.local`` mDNS alias, best-effort LAN IPs,
    and any ``extra_sans`` supplied via config — so browsers reaching the
    dashboard by any of those names don't trip a name-mismatch error.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    cert_dir = os.path.expanduser(cert_dir)
    os.makedirs(cert_dir, exist_ok=True)

    cert_path = os.path.join(cert_dir, "dashboard.crt")
    key_path = os.path.join(cert_dir, "dashboard.key")

    # If both files already exist, reuse them
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        if log:
            log.info("Reusing existing TLS certificate: %s", cert_path)
        return cert_path, key_path

    if log:
        log.info("Generating self-signed TLS certificate for '%s'", common_name)

    # Generate RSA key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Build certificate
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
    for value in _collect_san_strings(extra_sans):
        try:
            general: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(value))
            key_str = f"ip:{general.value}"
        except ValueError:
            general = x509.DNSName(value)
            key_str = f"dns:{value}"
        if key_str not in seen_san:
            seen_san.add(key_str)
            san_entries.append(general)

    now = datetime.datetime.now(datetime.timezone.utc)
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

    # Write private key — set restrictive permissions BEFORE writing
    # so the key material is never world-readable, even briefly.
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(
            fd,
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
    finally:
        os.close(fd)

    # Write certificate
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    if log:
        fingerprint = cert.fingerprint(hashes.SHA256()).hex(":")
        log.warning(
            "Self-signed certificate generated. SHA-256 fingerprint: %s",
            fingerprint,
        )

    return cert_path, key_path
