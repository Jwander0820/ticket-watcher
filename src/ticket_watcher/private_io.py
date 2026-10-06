"""Local settings persistence; credentials never enter YAML, SQLite or responses."""

import json
import os
import tempfile
from pathlib import Path


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".watcher-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_webhooks(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError
        return value
    except (ValueError, OSError):
        raise ValueError("無法讀取 Discord 私密設定檔，請檢查檔案格式與權限") from None
