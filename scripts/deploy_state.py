"""Offline volume preparation for the root-owned VPS deployment entry.

Run inside an application image with network disabled. Never print settings or secrets.
Backups contain credentials and must be kept private. A migration backup can be restored
only while the candidate has not resumed monitoring or notification delivery.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import yaml

from ticket_watcher import storage
from ticket_watcher.config import parse_config
from ticket_watcher.private_io import atomic_text
from ticket_watcher.storage import Store


class DeploymentError(ValueError):
    """An actionable deployment error containing no private settings or rows."""


LEGACY_OUTBOX = """
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, expires_at REAL NOT NULL,
 lease_until REAL, message_id TEXT, last_error TEXT,
 excluded_items TEXT NOT NULL DEFAULT '[]'
);
"""


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


def schema_version(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def legacy_schema():
    # Match the complete released v2 structure, not only its user_version.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "legacy.db"
        with closing(sqlite3.connect(path)) as db:
            db.executescript(storage.SCHEMA.replace(storage.OUTBOX_SCHEMA, LEGACY_OUTBOX))
            db.execute("PRAGMA user_version=2")
        return schema_signature(path)


def check_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise DeploymentError("DATABASE_INVALID: SQLite integrity check failed.")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise DeploymentError("DATABASE_INVALID: SQLite references are inconsistent.")


def contents(path, *, legacy=False):
    """Hash each logical table; normalize only the approved outbox transformation."""
    result = {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({quoted})")]
            if legacy and table == "outbox":
                columns.insert(1, "channel_id")
                query = (
                    "SELECT o.event_id,coalesce(json_extract(e.payload,'$.channel_id'),'default'),"
                    "o.status,o.attempts,o.next_attempt,o.expires_at,o.lease_until,"
                    "o.message_id,o.last_error,o.excluded_items "
                    "FROM outbox o JOIN events e ON e.id=o.event_id"
                )
            else:
                query = f"SELECT * FROM {quoted}"
            query += " ORDER BY " + ",".join(str(i + 1) for i in range(len(columns)))
            digest = hashlib.sha256()
            for row in db.execute(query):
                digest.update(json.dumps(row, ensure_ascii=True).encode() + b"\n")
            result[table] = digest.hexdigest()
    return result


def migrate_database(path, expected):
    check_database(path)
    before = contents(path, legacy=True)
    store = Store(path)
    store.close()
    if schema_signature(path) != expected:
        raise DeploymentError("MIGRATION_FAILED: Migrated structure differs from the tested image.")
    check_database(path)
    if contents(path) != before:
        raise DeploymentError(
            "MIGRATION_FAILED: Migration did not preserve all delivery/state rows."
        )


def rehearse_migration(path, expected):
    current = schema_version(path)
    target = getattr(storage, "SCHEMA_VERSION", 2)
    if (
        (current, target) != (2, 3)
        or expected != supported_schema()
        or schema_signature(path) != legacy_schema()
    ):
        raise DeploymentError(
            f"SCHEMA_MIGRATION_UNSUPPORTED: {current} -> {target}; "
            "unknown structure requires manual migration. Live service was not changed."
        )
    # SQLite backup includes committed WAL data and never writes to the live source.
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "rehearsal.db"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(copy)) as destination:
                source.backup(destination)
        migrate_database(copy, expected)


def read_document(data, *, config_base=None):
    path = data / "ui-config.yaml"
    document = (
        yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if path.exists()
        else {"app": {"database_path": "watcher.db", "ui_paused": True}, "targets": []}
    )
    config_base = data if config_base is None else config_base
    config = parse_config(document, config_base)
    if not config.database_path.resolve().is_relative_to(config_base.resolve()):
        raise ValueError("Database must stay inside the UI volume")
    return document, config


def ensure_schema(data, expected, *, allow_migration=False):
    _, config = read_document(data)
    if config.database_path.exists() and schema_signature(config.database_path) != expected:
        if allow_migration:
            rehearse_migration(config.database_path, expected)
            return True
        current = schema_version(config.database_path)
        target = getattr(storage, "SCHEMA_VERSION", 2)
        raise DeploymentError(
            f"SCHEMA_MIGRATION_REQUIRED: {current} -> {target}. "
            "Schema change requires a manual migration or the approved --allow-migration flow; "
            "deployment stopped. See docs/vps-migration.md."
        )
    return False


def write_paused(data, document, value):
    document.setdefault("app", {})["ui_paused"] = value
    path = data / "ui-config.yaml"
    atomic_text(path, yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    if os.geteuid() == 0:
        os.chown(path, 10001, 10001)


def prepare(data, backup, expected, *, allow_migration=False):
    # Reject links before copying private files out of the volume.
    if any(path.is_symlink() for path in data.rglob("*")):
        raise ValueError("Unexpected symlink in UI data")
    migrated = ensure_schema(data, expected, allow_migration=allow_migration)
    document, config = read_document(data)
    original_schema = (
        schema_signature(config.database_path) if config.database_path.exists() else expected
    )
    shutil.copytree(data, backup / "data")
    metadata = {
        "paused": config.ui_paused,
        "schema": expected,
        "original_schema": original_schema,
        "migrated": migrated,
        "resumed": False,
    }
    atomic_text(backup / "metadata.json", json.dumps(metadata))
    write_paused(data, document, True)
    if migrated:
        migrate_database(config.database_path, expected)
        print("MIGRATION_COMPLETE: 2 -> 3; backup saved and all delivery/state rows preserved.")


def resume(data, backup):
    metadata = json.loads((backup / "metadata.json").read_text())
    ensure_schema(data, metadata["schema"])
    document, _ = read_document(data)
    # Once work may resume, restoring old delivery rows could repeat notifications.
    metadata["resumed"] = True
    atomic_text(backup / "metadata.json", json.dumps(metadata))
    write_paused(data, document, metadata["paused"])


def restore_file(source, destination):
    # copy2 applies timestamps/mode to its destination. Keep that file root-owned
    # until metadata is complete so the restricted helper needs no FOWNER capability.
    descriptor, name = tempfile.mkstemp(prefix=".watcher-restore-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copy2(source, temporary)
        if os.geteuid() == 0:
            os.chown(temporary, 10001, 10001)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def rollback_migration(data, backup):
    path = backup / "metadata.json"
    if not path.exists():
        return
    metadata = json.loads(path.read_text())
    if not metadata.get("migrated"):
        return
    if metadata.get("resumed"):
        raise DeploymentError(
            "MIGRATION_ROLLBACK_BLOCKED: Work may have resumed; do not restore old delivery rows. "
            "Keep the service stopped and recover with the new schema."
        )
    document, original = read_document(backup / "data", config_base=data)
    relative = original.database_path.relative_to(data)
    database = data / relative
    saved_database = backup / "data" / relative
    if schema_signature(saved_database) != metadata["original_schema"]:
        raise DeploymentError("MIGRATION_ROLLBACK_BLOCKED: Backup structure verification failed.")
    check_database(saved_database)
    # Service is stopped; restore only the DB files and config from before migration.
    for suffix in ("", "-wal", "-shm", "-journal"):
        source = Path(str(saved_database) + suffix)
        destination = Path(str(database) + suffix)
        if source.exists():
            restore_file(source, destination)
        else:
            destination.unlink(missing_ok=True)
    write_paused(data, document, metadata["paused"])
    ensure_schema(data, metadata["original_schema"])
    metadata.update(schema=metadata["original_schema"], migrated=False)
    atomic_text(path, json.dumps(metadata))
    print("MIGRATION_ROLLED_BACK: Pre-migration database restored before work resumed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", choices=["schema", "prepare", "resume", "verify", "rollback-migration"]
    )
    parser.add_argument("--data", type=Path, default=Path("/app/data"))
    parser.add_argument("--backup", type=Path, default=Path("/backup"))
    parser.add_argument("--expected")
    parser.add_argument("--allow-migration", action="store_true")
    args = parser.parse_args()
    if args.operation == "schema":
        print(supported_schema())
    elif args.operation == "prepare":
        prepare(args.data, args.backup, args.expected, allow_migration=args.allow_migration)
    elif args.operation == "resume":
        resume(args.data, args.backup)
    elif args.operation == "rollback-migration":
        rollback_migration(args.data, args.backup)
    else:
        if ensure_schema(args.data, args.expected, allow_migration=args.allow_migration):
            print("MIGRATION_READY: 2 -> 3 rehearsal passed; live data was not modified.")


if __name__ == "__main__":
    try:
        main()
    except DeploymentError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        print(
            f"STATE_CHECK_FAILED: {type(error).__name__}; inspect private configuration/schema.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
