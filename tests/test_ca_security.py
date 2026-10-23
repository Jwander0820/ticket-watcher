"""Fail closed on altered bootstrap bytes and distrusted certificates."""

import datetime
import io
import runpy
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_rejects_tampering_before_install(monkeypatch):
    script = runpy.run_path(str(ROOT / "scripts/bootstrap_pip.py"))
    monkeypatch.setattr(script["urllib"].request, "urlopen", lambda *a, **kw: io.BytesIO(b"bad"))

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified wheel must never execute")

    monkeypatch.setattr(script["subprocess"], "run", forbidden)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        script["main"]()


def certificate(common_name):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def test_bundle_checks_certificate_subject_not_comments(tmp_path):
    check = runpy.run_path(str(ROOT / "scripts/verify_ca.py"))["check_bundle"]
    bundle = tmp_path / "ca.pem"
    good = certificate("Test unaffected root")
    bundle.write_bytes(b"# emSign ECC Root CA - G3\n" + good)
    assert check(str(bundle)) == 1
    bundle.write_bytes(good + certificate("emSign ECC Root CA - G3"))
    with pytest.raises(ValueError, match="Distrusted"):
        check(str(bundle))


@pytest.mark.parametrize("data", [b"", b"not a CA bundle"])
def test_empty_or_invalid_bundle_cannot_pass(tmp_path, data):
    check = runpy.run_path(str(ROOT / "scripts/verify_ca.py"))["check_bundle"]
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(data)
    with pytest.raises(ValueError, match="No certificates"):
        check(str(bundle))
