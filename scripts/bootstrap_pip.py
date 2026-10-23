"""Bootstrap pip from the PSF-announced wheel without invoking the old pip."""

import hashlib
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# https://blog.python.org/2026/10/python-3151-3149-31317/
WHEEL = "pip-26.2.2-py3-none-any.whl"
URL = (
    "https://files.pythonhosted.org/packages/3f/06/"
    "3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/" + WHEEL
)
SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"


def verify(data: bytes) -> None:
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("PSF pip wheel SHA256 mismatch; refusing installation")


def main() -> None:
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    verify(data)
    with tempfile.TemporaryDirectory(prefix="trusted-pip-") as directory:
        wheel = Path(directory) / WHEEL
        wheel.write_bytes(data)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); "
                "runpy.run_module('pip',run_name='__main__')",
                str(wheel),
                "install",
                "--no-index",
                "--no-deps",
                "--no-cache-dir",
                "--force-reinstall",
                str(wheel),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
