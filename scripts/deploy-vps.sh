#!/bin/bash
# Root-owned, fixed SSH deployment entry. No arbitrary shell or image arguments.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
APP_DIR=/opt/ticket-watcher
STATE_DIR=/var/lib/ticket-watcher-deploy
IMAGE_REPOSITORY=ghcr.io/jwander0820/ticket-watcher
CONTAINER=ticket-watcher-ticket-watcher-ui-1
VOLUME=ticket-watcher-ui-data
PREVIOUS_IMAGE=
BACKUP=
ROLLBACK_REQUIRED=0
EXPECTED_SCHEMA=
PREVIOUS_SCHEMA=

compose() {
    TICKET_WATCHER_IMAGE="$1" docker compose --project-name ticket-watcher \
        --env-file "$APP_DIR/.env" -f "$APP_DIR/compose.vps.yaml" "${@:2}"
}

helper() {
    local image=$1 operation=$2
    shift 2
    docker run --rm --network none --read-only --tmpfs /tmp --user 0 \
        --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true \
        --memory 256m --pids-limit 64 \
        -v "$VOLUME:/app/data" -v "$BACKUP:/backup" \
        -v "$APP_DIR/deploy_state.py:/deploy_state.py:ro" \
        --entrypoint python "$image" /deploy_state.py "$operation" "$@"
}

healthy() {
    compose "$1" up -d --no-deps --no-build --wait --wait-timeout 120 ticket-watcher-ui
    curl --fail --silent --max-time 10 --output /dev/null http://127.0.0.1:8787/
}

finish() {
    local result=$?
    trap - EXIT HUP INT TERM
    if [[ $ROLLBACK_REQUIRED == 1 ]]; then
        echo 'Deployment failed; checking whether the previous image can resume.' >&2
        compose "$IMAGE" stop ticket-watcher-ui || true
        if [[ -n "$PREVIOUS_IMAGE" ]] && \
            helper "$PREVIOUS_IMAGE" verify --expected "$PREVIOUS_SCHEMA" && \
            { [[ ! -f "$BACKUP/metadata.json" ]] || helper "$PREVIOUS_IMAGE" resume; } && \
            healthy "$PREVIOUS_IMAGE"; then
            echo 'Previous image restored. Deployment reported as failed.' >&2
        else
            echo 'UI left stopped. Inspect the private backup and recover manually.' >&2
        fi
        result=1
    fi
    exit "$result"
}

main() {
    [[ $# == 1 ]] || { echo 'Expected check or deploy <run number> <SHA> <digest>.' >&2; return 2; }
    [[ "$1" == check || "$1" =~ ^deploy\ ([1-9][0-9]{0,9})\ ([0-9a-f]{40})\ (sha256:[0-9a-f]{64})$ ]] || {
        echo 'Command rejected.' >&2; return 2;
    }
    [[ $(id -u) == 0 ]] || { echo 'Use the configured sudo entry.' >&2; return 1; }
    [[ -f "$APP_DIR/.env" && -f "$APP_DIR/compose.vps.yaml" && -f "$APP_DIR/deploy_state.py" ]] || {
        echo 'VPS deployment files missing.' >&2; return 1;
    }
    install -d -o root -g root -m 700 "$STATE_DIR"
    exec 9>"$STATE_DIR/deploy.lock"
    flock -w 300 9 || { echo 'Deployment lock timeout.' >&2; return 1; }
    if [[ "$1" == check ]]; then
        compose "$IMAGE_REPOSITORY:check" config --quiet
        echo 'Deployment entry ready. No deployment performed.'
        return 0
    fi
    local number revision digest previous_number=0 expected_revision
    [[ "$1" =~ ^deploy\ ([1-9][0-9]{0,9})\ ([0-9a-f]{40})\ (sha256:[0-9a-f]{64})$ ]]
    number=${BASH_REMATCH[1]}; revision=${BASH_REMATCH[2]}; digest=${BASH_REMATCH[3]}
    IMAGE="$IMAGE_REPOSITORY@$digest"
    [[ ! -f "$STATE_DIR/run-number" ]] || read -r previous_number < "$STATE_DIR/run-number"
    [[ "$previous_number" =~ ^[0-9]+$ ]] || return 1
    if (( number < previous_number )); then
        echo "Skipped outdated run $number."
        return 0
    fi
    # Pull and verify before stopping an existing application.
    docker pull "$IMAGE"
    expected_revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")
    [[ "$expected_revision" == "$revision" ]] || { echo 'Image revision mismatch.' >&2; return 1; }
    compose "$IMAGE" config --quiet
    PREVIOUS_IMAGE=$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
    if [[ -z "$PREVIOUS_IMAGE" ]] && docker volume inspect "$VOLUME" >/dev/null 2>&1; then
        echo 'Existing volume without a known container; inspect manually.' >&2
        return 1
    fi
    BACKUP="$APP_DIR/backups/$(date -u +%Y%m%dT%H%M%SZ)-$number-${revision:0:12}-$$"
    install -d -m 700 "$BACKUP"
    EXPECTED_SCHEMA=$(helper "$IMAGE" schema)
    [[ "$EXPECTED_SCHEMA" =~ ^[0-9a-f]{64}$ ]] || return 1
    if [[ -n "$PREVIOUS_IMAGE" ]]; then
        PREVIOUS_SCHEMA=$(helper "$PREVIOUS_IMAGE" schema)
        helper "$IMAGE" verify --expected "$EXPECTED_SCHEMA"
    fi
    trap finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    ROLLBACK_REQUIRED=1
    printf '%s\n' "$number" > "$STATE_DIR/run-number.tmp"
    mv -f "$STATE_DIR/run-number.tmp" "$STATE_DIR/run-number"
    if [[ -n "$PREVIOUS_IMAGE" ]]; then
        compose "$IMAGE" stop ticket-watcher-ui
    fi
    # Snapshot all private data while stopped; refuse automatic schema changes.
    helper "$IMAGE" prepare --expected "$EXPECTED_SCHEMA"
    healthy "$IMAGE"
    compose "$IMAGE" stop ticket-watcher-ui
    helper "$IMAGE" resume
    healthy "$IMAGE"
    # Write final state atomically. The live DB and previous image are retained.
    printf '%s\n' "$number" > "$STATE_DIR/run-number.tmp"
    mv -f "$STATE_DIR/run-number.tmp" "$STATE_DIR/run-number"
    printf '%s\n' "$IMAGE" > "$APP_DIR/image.env.tmp"
    mv -f "$APP_DIR/image.env.tmp" "$APP_DIR/image.env"
    ROLLBACK_REQUIRED=0
    printf 'Deployed %s (%s). Local HTTP and heartbeat passed.\n' "$revision" "$digest"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    if [[ $# == 2 && "$1" == --worker ]]; then
        main "$2"
    elif [[ $# == 1 && "$1" == check ]]; then
        main check
    elif [[ $# == 1 && "$1" =~ ^deploy\ ([1-9][0-9]{0,9})\ ([0-9a-f]{40})\ (sha256:[0-9a-f]{64})$ ]]; then
        [[ $(id -u) == 0 ]] || exit 1
        unit="ticket-watcher-deploy-${BASH_REMATCH[1]}-$$"
        result=0
        # The VPS owns the process lifetime; an SSH disconnect does not cancel it.
        systemd-run --quiet --wait --collect --unit "$unit" \
            --property RuntimeMaxSec=15min --property TimeoutStopSec=180s \
            /usr/local/sbin/ticket-watcher-deploy --worker "$1" || result=$?
        journalctl -u "$unit" --no-pager -n 80
        exit "$result"
    else
        echo 'Command rejected.' >&2
        exit 2
    fi
fi
