"""Optional self-signed certificate generation for HTTPS."""

from __future__ import annotations

import datetime
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def generate_self_signed_cert(
    cert_dir: str,
    common_name: str,
    log: logging.Logger | None = None,
) -> tuple[str, str]:
    """Generate a self-signed TLS certificate and private key.

    Returns (cert_path, key_path).
    Requires the `cryptography` package (transitive dependency of rns).
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
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name or "ReticulumPi"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ReticulumPi"),
    ])

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
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    # Write private key
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    os.chmod(key_path, 0o600)

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
