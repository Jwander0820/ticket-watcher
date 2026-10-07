"""Exercise deployment with the built image and the VPS helper's actual restrictions."""

import os
import subprocess
from pathlib import Path

import pytest

SMOKE = r"""
import os, runpy, sqlite3, tempfile
from pathlib import Path
from ticket_watcher import storage

helper = runpy.run_path('/deploy_state.py')
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    data, backup = root / 'data', root / 'backup'
    data.mkdir()
    (data / 'ui-config.yaml').write_text('app:\n  database_path: watcher.db\n  ui_paused: false\n')
    with sqlite3.connect(data / 'watcher.db') as db:
        db.executescript(storage.SCHEMA.replace(storage.OUTBOX_SCHEMA, helper['LEGACY_OUTBOX']))
        db.execute('PRAGMA user_version=2')
        db.execute("INSERT INTO events VALUES ('e',NULL,'RELEASE',1,'{}')")
        db.execute("INSERT INTO outbox VALUES ('e','SENT',1,1,100,NULL,'sent-id',NULL,'[]')")
    # Match the live volume: the helper is root with no FOWNER capability, while
    # the application owns its private directory, configuration and database.
    os.chmod(data, 0o700)
    os.chown(data, 10001, 10001)
    for path in (data / 'ui-config.yaml', data / 'watcher.db'):
        os.chmod(path, 0o600)
        os.chown(path, 10001, 10001)
    original = (data / 'watcher.db').read_bytes()
    expected = helper['supported_schema']()
    assert helper['ensure_schema'](data, expected, allow_migration=True)
    assert (data / 'watcher.db').read_bytes() == original
    helper['prepare'](data, backup, expected, allow_migration=True)
    assert helper['schema_signature'](data / 'watcher.db') == expected
    assert helper['read_document'](data)[1].ui_paused
    assert (data / 'ui-config.yaml').stat().st_uid == 10001
    assert (backup / 'data/watcher.db').read_bytes() == original
    helper['rollback_migration'](data, backup)
    assert (data / 'watcher.db').read_bytes() == original
    assert (data / 'watcher.db').stat().st_uid == 10001
    assert (data / 'watcher.db').stat().st_mode & 0o777 == 0o600
    assert not list(data.glob('.watcher-restore-*'))
    helper['prepare'](data, root / 'second-backup', expected, allow_migration=True)
    helper['resume'](data, root / 'second-backup')
    assert not helper['read_document'](data)[1].ui_paused
    try:
        helper['rollback_migration'](data, root / 'second-backup')
    except helper['DeploymentError'] as error:
        assert 'MIGRATION_ROLLBACK_BLOCKED' in str(error)
    else:
        raise AssertionError('Unsafe database rollback was allowed')
    with sqlite3.connect(data / 'watcher.db') as db:
        assert db.execute('SELECT channel_id,status,message_id FROM outbox').fetchall() == [('default','SENT','sent-id')]
print('Container migration, ownership, backup and rollback smoke passed.')
"""


def test_built_image_migration_under_vps_restrictions():
    image = os.environ.get("TICKET_WATCHER_DEPLOY_TEST_IMAGE")
    if not image:
        pytest.skip("Set TICKET_WATCHER_DEPLOY_TEST_IMAGE to the tested Linux image")
    helper = Path(__file__).resolve().parents[1] / "scripts/deploy_state.py"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--user",
            "0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "256m",
            "--pids-limit",
            "64",
            "--mount",
            f"type=bind,source={helper},target=/deploy_state.py,readonly",
            "--entrypoint",
            "python",
            image,
            "-",
        ],
        input=SMOKE,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Container migration, ownership, backup and rollback smoke passed." in result.stdout
