"""Offline verification of all three Linux image trust stores; no app startup."""

import importlib.metadata
import json
import re
import ssl
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.x509.oid import NameOID
from pip._vendor import certifi as pip_certifi


def check_bundle(path: str) -> int:
    blocks = re.findall(
        b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        Path(path).read_bytes(),
        re.DOTALL,
    )
    if not blocks:
        raise ValueError(f"No certificates found: {path}")
    for block in blocks:
        certificate = x509.load_pem_x509_certificate(block)
        names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if any(name.value == "emSign ECC Root CA - G3" for name in names):
            raise ValueError(f"Distrusted emSign ECC Root CA - G3 present: {path}")
    # Also require the bundle to be usable by the TLS implementation.
    ssl.create_default_context(cafile=path)
    return len(blocks)


def main() -> None:
    versions = {name: importlib.metadata.version(name) for name in ("pip", "certifi")}
    for name, minimum in (("pip", (26, 2, 2)), ("certifi", (2026, 10, 19))):
        if tuple(map(int, versions[name].split("."))) < minimum:
            raise ValueError(f"Unpatched {name}: {versions[name]}")
    paths = {
        "system": "/etc/ssl/certs/ca-certificates.crt",
        "certifi": certifi.where(),
        "pip_vendored": pip_certifi.where(),
    }
    counts = {name: check_bundle(path) for name, path in paths.items()}
    directory = Path("/etc/ssl/certs")
    files = list(directory.glob("*.pem"))
    if not files:
        raise ValueError("System CA directory is empty")
    counts["system_directory"] = sum(check_bundle(str(path)) for path in files)
    print(json.dumps({"versions": versions, "paths": paths, "certificates": counts}))


if __name__ == "__main__":
    main()
