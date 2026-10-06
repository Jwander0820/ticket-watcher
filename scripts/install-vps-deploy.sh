#!/bin/bash
# Install fixed entry points only. Does not start or stop containers.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
public_key=/home/ubuntu/.ssh/github_ticket_watcher_deploy.pub
authorized_keys=/home/ubuntu/.ssh/authorized_keys
[[ -f "$public_key" ]] || { echo 'Deployment public key missing.' >&2; exit 1; }
ssh-keygen -lf "$public_key" >/dev/null
read -r key_type key_body _ < "$public_key"
[[ "$key_type" == ssh-ed25519 ]] || { echo 'Expected an Ed25519 key.' >&2; exit 1; }
forced='restrict,command="/usr/bin/sudo -n /usr/local/sbin/ticket-watcher-deploy \"$SSH_ORIGINAL_COMMAND\""'
entry="$forced $key_type $key_body github-actions-ticket-watcher"
if [[ -f "$authorized_keys" ]] && grep -Fq -- "$key_body" "$authorized_keys"; then
    grep -Fxq -- "$entry" "$authorized_keys" || {
        echo 'Key already exists with different permissions; inspect manually.' >&2; exit 1;
    }
fi
bash -n "$source_dir/scripts/deploy-vps.sh"
install -d -o root -g root -m 700 /opt/ticket-watcher /opt/ticket-watcher/backups
install -o root -g root -m 644 "$source_dir/compose.vps.yaml" /opt/ticket-watcher/compose.vps.yaml
install -o root -g root -m 644 "$source_dir/scripts/deploy_state.py" /opt/ticket-watcher/deploy_state.py
install -o root -g root -m 755 "$source_dir/scripts/deploy-vps.sh" /usr/local/sbin/ticket-watcher-deploy
if [[ ! -f /opt/ticket-watcher/.env ]]; then
    install -o root -g root -m 600 "$source_dir/.env.example" /opt/ticket-watcher/.env
fi
temporary=$(mktemp)
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' 'ubuntu ALL=(root) NOPASSWD: /usr/local/sbin/ticket-watcher-deploy *' > "$temporary"
visudo -cf "$temporary"
install -o root -g root -m 440 "$temporary" /etc/sudoers.d/ticket-watcher-deploy
install -d -o ubuntu -g ubuntu -m 700 /home/ubuntu/.ssh
touch "$authorized_keys"
if ! grep -Fxq -- "$entry" "$authorized_keys"; then
    printf '\n%s\n' "$entry" >> "$authorized_keys"
fi
chown ubuntu:ubuntu "$authorized_keys"
chmod 600 "$authorized_keys"
/usr/local/sbin/ticket-watcher-deploy check
echo 'Installed restricted entry. No containers were started or updated.'
