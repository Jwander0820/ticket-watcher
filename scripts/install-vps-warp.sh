#!/bin/bash
# Connect an existing Ticket Watcher bridge to an already configured local WARP proxy.
# Installs the relay and Compose/env settings; does not restart the application.
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
APP_DIR=/opt/ticket-watcher
NETWORK=ticket-watcher_default
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $(id -u) == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
[[ -f "$APP_DIR/.env" && -f "$APP_DIR/compose.vps.yaml" ]] || {
    echo 'Install the VPS deployment files first.' >&2; exit 1;
}
warp-cli --accept-tos settings list | grep -Fq 'WarpProxy on port 40000' || {
    echo 'Configure WARP in local proxy mode on port 40000 first.' >&2; exit 1;
}
systemctl is-active --quiet warp-svc.service
ss -lntH '( sport = :40000 )' | grep -q '127.0.0.1:40000' || {
    echo 'WARP must listen on 127.0.0.1:40000.' >&2; exit 1;
}
# Validate Docker-derived values before using them in service or firewall settings.
network_data=$(docker network inspect bridge "$NETWORK")
mapfile -t addresses < <(printf '%s' "$network_data" | python3 -c '
import ipaddress,json,sys
networks=json.load(sys.stdin)
def ipv4(network):
    if network["Driver"] != "bridge":
        raise ValueError("Expected a Docker bridge")
    values=[x for x in network["IPAM"]["Config"] if ":" not in x["Subnet"]]
    if len(values) != 1:
        raise ValueError("Expected one IPv4 subnet")
    subnet=ipaddress.ip_network(values[0]["Subnet"])
    gateway=ipaddress.ip_address(values[0]["Gateway"])
    private=[ipaddress.ip_network(x) for x in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16")]
    if (not any(subnet.subnet_of(x) for x in private) or gateway not in subnet
            or gateway in (subnet.network_address,subnet.broadcast_address)
            or gateway.is_unspecified or gateway.is_loopback or gateway.is_multicast):
        raise ValueError("Expected a private Docker gateway")
    return subnet,gateway
_,bind=ipv4(networks[0])
source,_=ipv4(networks[1])
interface=networks[1].get("Options",{}).get("com.docker.network.bridge.name") or "br-"+networks[1]["Id"][:12]
if not interface.replace("-","").replace("_","").isalnum() or len(interface)>15:
    raise ValueError("Unexpected bridge interface")
print(bind);print(source);print(interface)
')
[[ ${#addresses[@]} == 3 ]] || { echo 'Invalid Docker network configuration.' >&2; exit 1; }
relay_bind=${addresses[0]}
relay_source=${addresses[1]}
relay_interface=${addresses[2]}
if ! command -v socat >/dev/null; then
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y \
        --no-install-recommends --no-upgrade socat
fi
backup="$APP_DIR/backups/warp-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 700 "$backup"
cp -a "$APP_DIR/.env" "$APP_DIR/compose.vps.yaml" "$backup/"
printf 'WARP_RELAY_BIND=%s\nWARP_RELAY_SOURCE=%s\n' "$relay_bind" "$relay_source" \
    > /etc/ticket-watcher-warp-relay.env
chmod 600 /etc/ticket-watcher-warp-relay.env
install -o root -g root -m 644 "$source_dir/scripts/ticket-watcher-warp-relay.service" \
    /etc/systemd/system/ticket-watcher-warp-relay.service
install -o root -g root -m 644 "$source_dir/compose.vps.yaml" "$APP_DIR/compose.vps.yaml"
# Preserve all existing private env values without displaying or shell-evaluating them.
python3 - "$APP_DIR/.env" "$relay_bind" <<'PY'
import os,stat,sys,tempfile
from pathlib import Path
path=Path(sys.argv[1])
values={"TICKET_WATCHER_TICKETPLUS_PROXY":"http://host.docker.internal:40001",
        "TICKET_WATCHER_WARP_HOST":sys.argv[2]}
lines=[line for line in path.read_text().splitlines()
       if line.partition("=")[0].strip().removeprefix("export ") not in values]
lines.extend(f"{key}={value}" for key,value in values.items())
previous=path.stat()
fd,name=tempfile.mkstemp(dir=path.parent,prefix=".warp-env-")
try:
    with os.fdopen(fd,"w") as stream:
        stream.write("\n".join(lines)+"\n")
        stream.flush();os.fsync(stream.fileno())
    os.chmod(name,stat.S_IMODE(previous.st_mode))
    os.chown(name,previous.st_uid,previous.st_gid)
    os.replace(name,path)
finally:
    if os.path.exists(name): os.unlink(name)
PY
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    ufw allow in on "$relay_interface" from "$relay_source" to "$relay_bind" \
        port 40001 proto tcp comment 'Ticket Watcher WARP relay'
fi
systemctl daemon-reload
systemctl enable ticket-watcher-warp-relay.service
systemctl restart ticket-watcher-warp-relay.service
relay_ready=0
for attempt in {1..20}; do
    if systemctl is-active --quiet ticket-watcher-warp-relay.service && \
        ss -lntH '( sport = :40001 )' | awk '{print $4}' | grep -Fxq "$relay_bind:40001"; then
        relay_ready=1
        break
    fi
    sleep 0.2
done
[[ $relay_ready == 1 ]] || { echo 'Relay did not bind to the expected private gateway.' >&2; exit 1; }
printf 'Private relay installed on %s:40001 for %s.\n' "$relay_bind" "$relay_source"
printf 'Compose/env backup: %s\n' "$backup"
echo 'Recreate only ticket-watcher-ui with the tested proxy-capable image to activate.'
