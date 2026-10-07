import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from ticket_watcher.storage import OUTBOX_SCHEMA, SCHEMA, Store

spec = importlib.util.spec_from_file_location(
    "deploy_state", Path(__file__).resolve().parents[1] / "scripts/deploy_state.py"
)
deploy_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_state)


@pytest.fixture
def volume(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_state.os, "geteuid", lambda: 10001, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    return data, tmp_path / "backup"


def config(data, paused=False):
    (data / "ui-config.yaml").write_text(
        yaml.safe_dump({"app": {"database_path": "watcher.db", "ui_paused": paused}})
    )
    store = Store(data / "watcher.db")
    store.close()


def test_prepare_preserves_private_data_and_resume_preserves_new_settings(volume):
    data, backup = volume
    config(data)
    secret = data / "discord-webhooks.json"
    secret.write_text('{"default":"private-test-webhook"}')
    original_db = (data / "watcher.db").read_bytes()
    deploy_state.prepare(data, backup, deploy_state.supported_schema())
    assert yaml.safe_load((data / "ui-config.yaml").read_text())["app"]["ui_paused"]
    assert (backup / "data/discord-webhooks.json").read_text() == secret.read_text()
    assert (data / "watcher.db").read_bytes() == original_db
    document, _ = deploy_state.read_document(data)
    document["app"]["retention_days"] = 21
    deploy_state.write_paused(data, document, True)
    deploy_state.resume(data, backup)
    document, _ = deploy_state.read_document(data)
    assert not document["app"]["ui_paused"]
    assert document["app"]["retention_days"] == 21


def test_blank_volume_starts_paused_without_creating_database(volume):
    data, backup = volume
    deploy_state.prepare(data, backup, deploy_state.supported_schema())
    deploy_state.resume(data, backup)
    assert deploy_state.read_document(data)[1].ui_paused
    assert not (data / "watcher.db").exists()


def test_existing_pause_is_preserved(volume):
    data, backup = volume
    config(data, paused=True)
    deploy_state.prepare(data, backup, deploy_state.supported_schema())
    deploy_state.resume(data, backup)
    assert deploy_state.read_document(data)[1].ui_paused


def test_migration_is_rejected_before_backup_or_pause(volume):
    data, backup = volume
    config(data)
    before = (data / "ui-config.yaml").read_bytes()
    with pytest.raises(ValueError, match="Schema change"):
        deploy_state.prepare(data, backup, "a" * 64)
    assert not backup.exists()
    assert (data / "ui-config.yaml").read_bytes() == before


def test_resume_refuses_changed_schema(volume):
    data, backup = volume
    config(data)
    deploy_state.prepare(data, backup, deploy_state.supported_schema())
    metadata = json.loads((backup / "metadata.json").read_text())
    metadata["schema"] = "a" * 64
    (backup / "metadata.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="Schema change"):
        deploy_state.resume(data, backup)
    assert deploy_state.read_document(data)[1].ui_paused


def test_database_cannot_escape_volume(volume):
    data, backup = volume
    (data / "ui-config.yaml").write_text("app:\n  database_path: ../external.db\n")
    with pytest.raises(ValueError, match="inside"):
        deploy_state.prepare(data, backup, deploy_state.supported_schema())
    assert not backup.exists()


def test_backup_failure_does_not_pause_live_config(volume, monkeypatch):
    data, backup = volume
    config(data)
    before = (data / "ui-config.yaml").read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(deploy_state.shutil, "copytree", fail)
    with pytest.raises(OSError):
        deploy_state.prepare(data, backup, deploy_state.supported_schema())
    assert (data / "ui-config.yaml").read_bytes() == before


def test_schema_signature_does_not_create_missing_database(tmp_path):
    path = Path(tmp_path) / "missing.db"
    with pytest.raises(deploy_state.sqlite3.OperationalError):
        deploy_state.schema_signature(path)
    assert not path.exists()


LEGACY_OUTBOX = """
CREATE TABLE IF NOT EXISTS outbox (
 event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL, expires_at REAL NOT NULL,
 lease_until REAL, message_id TEXT, last_error TEXT,
 excluded_items TEXT NOT NULL DEFAULT '[]'
);
"""


def legacy_config(data):
    (data / "ui-config.yaml").write_text(
        yaml.safe_dump({"app": {"database_path": "watcher.db", "ui_paused": False}})
    )
    with sqlite3.connect(data / "watcher.db") as db:
        db.executescript(SCHEMA.replace(OUTBOX_SCHEMA, LEGACY_OUTBOX))
        db.execute("PRAGMA user_version=2")
        db.execute(
            "INSERT INTO events VALUES ('sent', NULL, 'RELEASE', 10, ?) ",
            (json.dumps({"channel_id": "private-channel"}),),
        )
        db.execute("INSERT INTO events VALUES ('pending', NULL, 'RELEASE', 20, '{}')")
        db.execute("INSERT INTO outbox VALUES ('sent', 'SENT', 2, 15, 100, NULL, 'm', NULL, '[]')")
        db.execute(
            "INSERT INTO outbox VALUES ('pending', 'PENDING', 1, 30, 200, 40, NULL, 'HTTP', '[]')"
        )
        db.execute("INSERT INTO runtime VALUES ('baseline', 'private-baseline')")


def test_known_migration_is_rehearsed_and_backed_up_before_live_change(volume):
    data, backup = volume
    legacy_config(data)
    before = (data / "watcher.db").read_bytes()
    expected = deploy_state.supported_schema()
    deploy_state.ensure_schema(data, expected, allow_migration=True)
    assert (data / "watcher.db").read_bytes() == before
    deploy_state.prepare(data, backup, expected, allow_migration=True)
    assert (backup / "data/watcher.db").read_bytes() == before
    assert deploy_state.schema_signature(data / "watcher.db") == expected
    assert deploy_state.read_document(data)[1].ui_paused
    with sqlite3.connect(data / "watcher.db") as db:
        assert db.execute(
            "SELECT event_id,channel_id,status,attempts FROM outbox ORDER BY event_id"
        ).fetchall() == [
            ("pending", "default", "PENDING", 1),
            ("sent", "private-channel", "SENT", 2),
        ]
        assert (
            db.execute("SELECT value FROM runtime WHERE key='baseline'").fetchone()[0]
            == "private-baseline"
        )
    deploy_state.resume(data, backup)
    assert not deploy_state.read_document(data)[1].ui_paused


def test_cli_reports_schema_versions_without_exposing_settings(volume):
    data, _ = volume
    legacy_config(data)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(deploy_state.__file__)),
            "verify",
            "--data",
            str(data),
            "--expected",
            deploy_state.supported_schema(),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "SCHEMA_MIGRATION_REQUIRED" in result.stderr
    assert "2 -> 3" in result.stderr
    assert "private" not in result.stderr


@pytest.mark.parametrize("damage", ["unknown", "orphan", "future"])
def test_unsafe_migration_is_rejected_before_backup_or_live_change(volume, damage):
    data, backup = volume
    legacy_config(data)
    with sqlite3.connect(data / "watcher.db") as db:
        if damage == "unknown":
            db.execute("ALTER TABLE runtime ADD COLUMN unexpected TEXT")
        elif damage == "orphan":
            db.execute("DELETE FROM events WHERE id='pending'")
        else:
            db.execute("PRAGMA user_version=4")
    before = (data / "watcher.db").read_bytes()
    settings = (data / "ui-config.yaml").read_bytes()
    with pytest.raises(deploy_state.DeploymentError):
        deploy_state.prepare(data, backup, deploy_state.supported_schema(), allow_migration=True)
    assert not backup.exists()
    assert (data / "watcher.db").read_bytes() == before
    assert (data / "ui-config.yaml").read_bytes() == settings


def test_migration_failure_restores_original_schema_and_pause_before_resume(volume):
    data, backup = volume
    legacy_config(data)
    before = (data / "watcher.db").read_bytes()
    original_schema = deploy_state.schema_signature(data / "watcher.db")
    deploy_state.prepare(data, backup, deploy_state.supported_schema(), allow_migration=True)
    deploy_state.rollback_migration(data, backup)
    assert (data / "watcher.db").read_bytes() == before
    assert deploy_state.schema_signature(data / "watcher.db") == original_schema
    assert not deploy_state.read_document(data)[1].ui_paused
    # The same metadata can now be used to resume the previous image.
    deploy_state.resume(data, backup)
    deploy_state.rollback_migration(data, backup)


def test_post_resume_failure_never_restores_old_delivery_history(volume):
    data, backup = volume
    legacy_config(data)
    expected = deploy_state.supported_schema()
    deploy_state.prepare(data, backup, expected, allow_migration=True)
    deploy_state.resume(data, backup)
    with sqlite3.connect(data / "watcher.db") as db:
        db.execute("UPDATE outbox SET status='SENT' WHERE event_id='pending'")
    before = (data / "watcher.db").read_bytes()
    with pytest.raises(deploy_state.DeploymentError, match="MIGRATION_ROLLBACK_BLOCKED"):
        deploy_state.rollback_migration(data, backup)
    assert (data / "watcher.db").read_bytes() == before
    assert deploy_state.schema_signature(data / "watcher.db") == expected


def test_rehearsal_includes_committed_wal_without_mutating_source(volume):
    data, _ = volume
    legacy_config(data)
    with sqlite3.connect(data / "watcher.db") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO runtime VALUES ('wal-only', 'preserved')")
        db.commit()
        files = {
            p.name: p.read_bytes() for p in data.glob("watcher.db*") if not p.name.endswith("-shm")
        }
        deploy_state.ensure_schema(data, deploy_state.supported_schema(), allow_migration=True)
        assert {
            p.name: p.read_bytes() for p in data.glob("watcher.db*") if not p.name.endswith("-shm")
        } == files


def test_failed_migration_after_backup_can_recover(volume, monkeypatch):
    data, backup = volume
    legacy_config(data)
    before = (data / "watcher.db").read_bytes()
    original = deploy_state.migrate_database

    def fail_live(path, expected):
        original(path, expected)
        if path == data / "watcher.db":
            raise OSError("private failure")

    monkeypatch.setattr(deploy_state, "migrate_database", fail_live)
    with pytest.raises(OSError):
        deploy_state.prepare(data, backup, deploy_state.supported_schema(), allow_migration=True)
    deploy_state.rollback_migration(data, backup)
    assert (data / "watcher.db").read_bytes() == before
    assert not deploy_state.read_document(data)[1].ui_paused


def test_migration_rollback_supports_absolute_path_inside_volume(volume):
    data, backup = volume
    legacy_config(data)
    (data / "ui-config.yaml").write_text(
        yaml.safe_dump({"app": {"database_path": str(data / "watcher.db"), "ui_paused": True}})
    )
    before = (data / "watcher.db").read_bytes()
    deploy_state.prepare(data, backup, deploy_state.supported_schema(), allow_migration=True)
    deploy_state.rollback_migration(data, backup)
    assert (data / "watcher.db").read_bytes() == before
    assert deploy_state.read_document(data)[1].ui_paused


def test_invalid_backup_never_overwrites_migrated_database(volume):
    data, backup = volume
    legacy_config(data)
    deploy_state.prepare(data, backup, deploy_state.supported_schema(), allow_migration=True)
    with sqlite3.connect(backup / "data/watcher.db") as db:
        db.execute("ALTER TABLE runtime ADD COLUMN unexpected TEXT")
    before = (data / "watcher.db").read_bytes()
    with pytest.raises(deploy_state.DeploymentError, match="Backup structure"):
        deploy_state.rollback_migration(data, backup)
    assert (data / "watcher.db").read_bytes() == before
