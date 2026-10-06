"""Deployment decisions with fake Docker and no SSH or daemon access."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = (
    "C:/Software/Git/bin/bash.exe"
    if Path("C:/Software/Git/bin/bash.exe").exists()
    else shutil.which("bash")
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-vps.sh"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
HARNESS = r"""
if command -v cygpath >/dev/null 2>&1; then
    DEPLOY_SCRIPT=$(cygpath -u "$DEPLOY_SCRIPT")
    CASE_DIR=$(cygpath -u "$CASE_DIR")
fi
source "$DEPLOY_SCRIPT"
APP_DIR="$CASE_DIR/app"
STATE_DIR="$CASE_DIR/state"
mkdir -p "$APP_DIR" "$STATE_DIR"
touch "$APP_DIR/.env" "$APP_DIR/compose.vps.yaml" "$APP_DIR/deploy_state.py"
[[ "$FAILURE" != outdated ]] || printf '99\n' > "$STATE_DIR/run-number"
id() { printf '0\n'; }
install() { mkdir -p "${@: -1}"; }
flock() { [[ "$FAILURE" != locked ]]; }
curl() { return 0; }
docker() {
    printf '%s\n' "$*" >> "$CASE_DIR/calls"
    case "$1" in
        pull) [[ "$FAILURE" != pull ]] ;;
        inspect) printf 'old-image-id\n' ;;
        image)
            if [[ "$FAILURE" == revision ]]; then printf 'wrong\n'; else printf '%s\n' "$SHA"; fi ;;
        compose)
            if [[ " $* " == *' up '* ]]; then
                printf '%s\n' "$TICKET_WATCHER_IMAGE" >> "$CASE_DIR/images"
                [[ "$FAILURE" != health || "$TICKET_WATCHER_IMAGE" == old-image-id ]]
            fi ;;
        *) return 99 ;;
    esac
}
helper() {
    printf 'helper %s %s\n' "$1" "$2" >> "$CASE_DIR/calls"
    case "$2" in
        schema) printf '%064d\n' 0 ;;
        verify) [[ "$FAILURE" != schema ]] ;;
        prepare) touch "$BACKUP/metadata.json" ;;
        resume) return 0 ;;
    esac
}
main "$TEST_COMMAND"
"""


def run_case(tmp_path, command, failure=""):
    if not BASH:
        pytest.skip("Bash required")
    result = subprocess.run(
        [BASH, "-c", HARNESS],
        env=dict(
            os.environ,
            DEPLOY_SCRIPT=str(SCRIPT),
            CASE_DIR=str(tmp_path),
            TEST_COMMAND=command,
            FAILURE=failure,
            SHA=SHA,
        ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    calls = (tmp_path / "calls").read_text() if (tmp_path / "calls").exists() else ""
    images = (tmp_path / "images").read_text() if (tmp_path / "images").exists() else ""
    return result, calls, images


def test_command_injection_rejected(tmp_path):
    result, calls, _ = run_case(tmp_path, f"deploy 1 {SHA} {DIGEST}; id")
    assert result.returncode == 2
    assert calls == ""


def test_check_is_read_only_for_application(tmp_path):
    result, calls, images = run_case(tmp_path, "check")
    assert result.returncode == 0, result.stderr
    assert "pull " not in calls and " stop " not in calls
    assert not images


@pytest.mark.parametrize("failure", ["pull", "revision", "schema", "locked"])
def test_preflight_failure_keeps_service_running(tmp_path, failure):
    result, calls, images = run_case(tmp_path, f"deploy 1 {SHA} {DIGEST}", failure)
    assert result.returncode != 0
    assert " stop " not in calls and not images


def test_stale_run_is_skipped(tmp_path):
    result, calls, images = run_case(tmp_path, f"deploy 1 {SHA} {DIGEST}", "outdated")
    assert result.returncode == 0, result.stderr
    assert calls == "" and not images


def test_success_uses_digest_and_restores_pause(tmp_path):
    result, calls, images = run_case(tmp_path, f"deploy 1 {SHA} {DIGEST}")
    assert result.returncode == 0, result.stderr
    assert images.splitlines() == [f"ghcr.io/jwander0820/ticket-watcher@{DIGEST}"] * 2
    assert " prepare" in calls and " resume" in calls
    assert "--no-deps --no-build --wait" in calls
    assert (tmp_path / "state/run-number").read_text().strip() == "1"


def test_failed_health_rolls_back_without_restoring_database(tmp_path):
    result, calls, images = run_case(tmp_path, f"deploy 1 {SHA} {DIGEST}", "health")
    assert result.returncode != 0
    assert images.splitlines()[-1] == "old-image-id"
    assert "helper old-image-id verify" in calls
    assert "helper old-image-id resume" in calls
    assert "restore" not in calls
