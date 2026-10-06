"""Offline volume preparation for the root-owned VPS deployment entry.

Run inside an application image with network disabled. Never print settings or secrets.
Backups contain credentials and must be kept private. No database restoration is automatic.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import yaml

from ticket_watcher.config import parse_config
from ticket_watcher.private_io import atomic_text
from ticket_watcher.storage import Store


def schema_signature(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        version = db.execute("PRAGMA user_version").fetchone()[0]
    return hashlib.sha256(json.dumps([version, rows]).encode()).hexdigest()


def supported_schema():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "schema.db"
        store = Store(path)
        store.close()
        return schema_signature(path)


def read_document(data):
    path = data / "ui-config.yaml"
    document = (
        yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if path.exists()
        else {"app": {"database_path": "watcher.db", "ui_paused": True}, "targets": []}
    )
    config = parse_config(document, data)
    if not config.database_path.resolve().is_relative_to(data.resolve()):
        raise ValueError("Database must stay inside the UI volume")
    return document, config


def ensure_schema(data, expected):
    _, config = read_document(data)
    if config.database_path.exists() and schema_signature(config.database_path) != expected:
        raise ValueError("Schema change requires a manual migration; deployment stopped")


def write_paused(data, document, value):
    document.setdefault("app", {})["ui_paused"] = value
    path = data / "ui-config.yaml"
    atomic_text(path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    if os.geteuid() == 0:
        os.chown(path, 10001, 10001)


def prepare(data, backup, expected):
    # Reject links before copying private files out of the volume.
    if any(path.is_symlink() for path in data.rglob("*")):
        raise ValueError("Unexpected symlink in UI data")
    ensure_schema(data, expected)
    document, config = read_document(data)
    shutil.copytree(data, backup / "data")
    metadata = {"paused": config.ui_paused, "schema": expected}
    atomic_text(backup / "metadata.json", json.dumps(metadata))
    write_paused(data, document, True)


def resume(data, backup):
    metadata = json.loads((backup / "metadata.json").read_text())
    ensure_schema(data, metadata["schema"])
    document, _ = read_document(data)
    write_paused(data, document, metadata["paused"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["schema", "prepare", "resume", "verify"])
    parser.add_argument("--data", type=Path, default=Path("/app/data"))
    parser.add_argument("--backup", type=Path, default=Path("/backup"))
    parser.add_argument("--expected")
    args = parser.parse_args()
    if args.operation == "schema":
        print(supported_schema())
    elif args.operation == "prepare":
        prepare(args.data, args.backup, args.expected)
    elif args.operation == "resume":
        resume(args.data, args.backup)
    else:
        ensure_schema(args.data, args.expected)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Offline deployment state check failed; inspect configuration/schema.")
