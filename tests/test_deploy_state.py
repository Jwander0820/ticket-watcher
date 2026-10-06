import json
from pathlib import Path

import pytest
import yaml

from scripts import deploy_state
from ticket_watcher.storage import Store


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
