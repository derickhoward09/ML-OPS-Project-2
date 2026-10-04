#!/usr/bin/env bash
# Reconcile the node 24 Cowrie decoy. All SSH authentication uses the group key.
# Run from the scheduler VM as an ordinary user; only the target uses sudo -n.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SOURCE="$SCRIPT_DIR/delay_proxy.py"
GROUP_KEY="$HOME/.ssh/mlops/id_ed25519_group_key"
GROUP_PUBLIC_KEY="$HOME/.ssh/mlops/id_ed25519_group_key.pub"
TARGET="student-admin@paffenroth-23.dyn.wpi.edu"
SSH_PORT=23001
SSH_JUMP="${RESUMELENS_SSH_JUMP:-${SSH_JUMP:-turing.wpi.edu}}"

usage() {
    cat <<'EOF'
Usage: cowrie/deploy.sh [--check|--check-only]

With no arguments, install or repair Cowrie 3.0.15 and the delay proxy on node
24 only when the desired state is missing or unhealthy. --check reports health
without modifying the target. Its exit status distinguishes an unhealthy
deployment (1), unavailable SSH access (2), and a mismatched authorized_keys
file (3), and unavailable local key material (4). This script never uses the
student-admin bootstrap key. Additional results: starting (5), runtime failure
with correct configuration (6), and inconclusive check (7).
EOF
}

MODE=deploy
case "${1:-}" in
    "") ;;
    --check|--check-only) MODE=check ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then
    usage >&2
    exit 2
fi

if [[ ! -s "$GROUP_KEY" || ! -r "$GROUP_KEY" ]]; then
    printf 'ERROR: group SSH key is missing or unreadable: %s\n' "$GROUP_KEY" >&2
    exit 4
fi
if [[ ! -s "$GROUP_PUBLIC_KEY" || ! -r "$GROUP_PUBLIC_KEY" ]]; then
    printf 'ERROR: group SSH public key is missing or unreadable: %s\n' "$GROUP_PUBLIC_KEY" >&2
    exit 4
fi
if [[ ! -s "$PROXY_SOURCE" || ! -r "$PROXY_SOURCE" ]]; then
    printf 'ERROR: delay proxy source is missing or unreadable: %s\n' "$PROXY_SOURCE" >&2
    exit 7
fi
if [[ ! -r "$SCRIPT_DIR/remote_recovery.sh" ]]; then
    echo 'ERROR: target recovery helper is unavailable.' >&2
    exit 7
fi

SSH_OPTIONS=(
    -T -F "$SCRIPT_DIR/../scripts/ssh_config" -p "$SSH_PORT" -i "$GROUP_KEY"
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o CertificateFile=none
    -o PreferredAuthentications=publickey
    -o PasswordAuthentication=no
    -o KbdInteractiveAuthentication=no
    -o GSSAPIAuthentication=no
    -o HostbasedAuthentication=no
    -o ConnectTimeout=8
    -o ConnectionAttempts=1
    -o ServerAliveInterval=5
    -o ServerAliveCountMax=2
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o GlobalKnownHostsFile=/dev/null
    -o ForwardAgent=no
    -o ClearAllForwardings=yes
    -o LogLevel=ERROR
)

# Persistent monitoring reuses an already authenticated master. A failing
# ProxyCommand prevents OpenSSH from silently opening a new TCP connection if
# the control socket disappears between the liveness check and this command.
if [[ -n "${COWRIE_SSH_CONTROL_PATH:-}" ]]; then
    if [[ "$COWRIE_SSH_CONTROL_PATH" != /* ]]; then
        printf 'ERROR: COWRIE_SSH_CONTROL_PATH must be absolute.\n' >&2
        exit 2
    fi
    SSH_OPTIONS+=(
        -S "$COWRIE_SSH_CONTROL_PATH"
        -o ControlMaster=no
        -o ProxyCommand=/bin/false
    )
else
    SSH_OPTIONS+=(-J "$SSH_JUMP")
fi

local_stage=""
remote_stage=""
cleanup() {
    if [[ -n "$remote_stage" ]]; then
        ssh "${SSH_OPTIONS[@]}" "$TARGET" "rm -rf -- '$remote_stage'" \
            >/dev/null 2>&1 || true
    fi
    if [[ -n "$local_stage" ]]; then
        rm -rf -- "$local_stage"
    fi
}
trap cleanup EXIT

local_stage="$(mktemp -d "${TMPDIR:-/tmp}/cowrie-deploy.XXXXXXXX")"

cat > "$local_stage/cowrie.cfg" <<'EOF'
# Managed by cowrie/deploy.sh. Authentication must never succeed.
[honeypot]
backend = shell
auth_class = UserDB
etc_path = etc

[ssh]
enabled = true
listen_endpoints = tcp:2222:interface=127.0.0.1
auth_publickey_allow_any = false
auth_none_enabled = false
auth_keyboard_interactive_enabled = false
sftp_enabled = false
forwarding = false
forward_redirect = false
forward_tunnel = false

[telnet]
enabled = false
EOF

# Cowrie UserDB uses a leading ! for a deny rule. This one wildcard rule
# rejects every password for every username, including empty passwords.
printf '*:x:!*\n' > "$local_stage/userdb.txt"

cat > "$local_stage/cowrie.service" <<'EOF'
[Unit]
Description=Cowrie SSH honeypot on loopback
After=network-online.target
Wants=network-online.target
Upholds=cowrie-delay.service

[Service]
Type=simple
User=cowrie
Group=cowrie
WorkingDirectory=/opt/cowrie/honeypot
Environment=COWRIE_STDOUT=yes
Environment=PATH=/opt/cowrie/honeypot/cowrie-env/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/opt/cowrie/honeypot/cowrie-env/bin/cowrie start
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=full
UMask=0077
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
EOF

cat > "$local_stage/cowrie-delay.service" <<'EOF'
[Unit]
Description=Delayed SSH front end for Cowrie on port 22001
After=network-online.target cowrie.service
Wants=network-online.target
BindsTo=cowrie.service

[Service]
Type=simple
User=cowrie
Group=cowrie
WorkingDirectory=/opt/cowrie
ExecStart=/usr/bin/python3 /opt/cowrie/delay_proxy.py
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=full
UMask=0077
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
EOF

cp -- "$PROXY_SOURCE" "$local_stage/delay_proxy.py"

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

CFG_HASH="$(sha256_file "$local_stage/cowrie.cfg")"
USERDB_HASH="$(sha256_file "$local_stage/userdb.txt")"
COWRIE_UNIT_HASH="$(sha256_file "$local_stage/cowrie.service")"
DELAY_UNIT_HASH="$(sha256_file "$local_stage/cowrie-delay.service")"
PROXY_HASH="$(sha256_file "$local_stage/delay_proxy.py")"
AUTHORIZED_KEYS_HASH="$(sha256_file "$GROUP_PUBLIC_KEY")"

remote_command() {
    # Append the caller's script after the shared target-side functions.
    { cat "$SCRIPT_DIR/remote_recovery.sh"; cat; } | \
        ssh "${SSH_OPTIONS[@]}" "$TARGET" \
        "sudo -n bash -s -- '$CFG_HASH' '$USERDB_HASH' '$COWRIE_UNIT_HASH' '$DELAY_UNIT_HASH' '$PROXY_HASH' '$AUTHORIZED_KEYS_HASH' ${1:-}"
}

verify_remote() {
    remote_command <<'REMOTE_CHECK'
remote_health "$@"
REMOTE_CHECK
}

verify_status=0
if verify_remote; then
    printf 'Cowrie healthy on node 24; no changes made.\n'
    exit 0
else
    verify_status=$?
fi
case "$verify_status" in
    255) printf 'Cowrie health unknown; management SSH unavailable.\n' >&2; exit 2 ;;
    20) exit 3 ;;
    0|1|5|6|7) ;;
    *) printf 'Cowrie health unknown; remote health check failed (status %s).\n' "$verify_status" >&2; exit 7 ;;
esac
if [[ "$MODE" == check || "$verify_status" == 5 || "$verify_status" == 7 ]]; then
    exit "$verify_status"
fi
if [[ "$verify_status" == 6 ]]; then
    recovery_status=0
    remote_command <<'REMOTE_RECOVER' || recovery_status=$?
recover_runtime "$@"
REMOTE_RECOVER
    case "$recovery_status" in
        1) ;; # Failed targeted restart: proceed to locked reconciliation.
        255) exit 2 ;;
        20) exit 3 ;;
        0|5|6|7) exit "$recovery_status" ;;
        *) exit 7 ;;
    esac
fi

printf 'Cowrie state is missing or unhealthy; reconciling node 24.\n'

# Check the exact sudo operation required by the installer before upload.
ssh "${SSH_OPTIONS[@]}" "$TARGET" 'sudo -n bash -c true' >/dev/null
remote_stage="$(ssh "${SSH_OPTIONS[@]}" "$TARGET" 'mktemp -d /tmp/cowrie-stage.XXXXXXXX')"
if [[ ! "$remote_stage" =~ ^/tmp/cowrie-stage\.[A-Za-z0-9]+$ ]]; then
    printf 'ERROR: unexpected remote staging path.\n' >&2
    remote_stage=""
    exit 1
fi
tar -C "$local_stage" -cf - . | \
    ssh "${SSH_OPTIONS[@]}" "$TARGET" "tar -xf - -C '$remote_stage'"

install_status=0
remote_command "'$remote_stage'" <<'REMOTE_INSTALL' || install_status=$?
set -Eeuo pipefail

stage="${7}"
state=/opt/cowrie/honeypot
venv="$state/cowrie-env"

[[ "$EUID" == 0 ]] || { echo 'ERROR: target sudo is required' >&2; exit 1; }
[[ "$stage" =~ ^/tmp/cowrie-stage\.[A-Za-z0-9]+$ ]] || { echo 'ERROR: invalid staging path' >&2; exit 1; }
for file in cowrie.cfg userdb.txt cowrie.service cowrie-delay.service delay_proxy.py; do
    [[ -f "$stage/$file" && ! -L "$stage/$file" ]] || { echo "ERROR: staging file $file is missing" >&2; exit 1; }
done
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 22.04 ]] || { echo 'ERROR: expected Ubuntu 22.04 on target' >&2; exit 1; }

# Recheck under the same target lock used by service-only recovery.
recovery_lock || exit $?
status=0
remote_health "${@:1:6}" || status=$?
case "$status" in
    0|5|7|20) exit "$status" ;;
    1|6) ;;
    *) exit 7 ;;
esac
if (( status == 6 )); then
    restart="$(read_timestamp restart)" || exit 7
    now="$(monotonic_seconds)" || exit 7
    (( restart >= 0 && now >= restart && now - restart >= STARTUP_GRACE )) || exit 6
fi
reserve_reconciliation || exit $?

# Stop the managed services before changing their package or effective config.
for service in cowrie-delay.service cowrie.service; do
    if systemctl cat "$service" >/dev/null 2>&1; then
        systemctl stop "$service"
    fi
done

packages=(python3-pip python3-venv libssl-dev libffi-dev build-essential libpython3-dev)
missing=()
for package in "${packages[@]}"; do
    if [[ "$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
        missing+=("$package")
    fi
done
if (( ${#missing[@]} )); then
    echo 'Installing missing Cowrie system dependencies.'
    apt-get -qq update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
fi

if ! getent group cowrie >/dev/null; then
    groupadd --system cowrie
fi
if ! getent passwd cowrie >/dev/null; then
    useradd --system --gid cowrie --create-home --home-dir /opt/cowrie --shell /usr/sbin/nologin cowrie
fi
install -d -o root -g root -m 0755 /opt/cowrie
install -d -o cowrie -g cowrie -m 0750 "$state"

if [[ ! -x "$venv/bin/python" ]]; then
    runuser -u cowrie -- python3 -m venv "$venv"
fi
version="$("$venv/bin/python" -c 'from importlib.metadata import version; print(version("cowrie"))' 2>/dev/null || true)"
if [[ "$version" != 3.0.15 || ! -x "$venv/bin/cowrie" ]]; then
    echo 'Installing Cowrie 3.0.15 into its virtual environment.'
    runuser -u cowrie -- env HOME=/opt/cowrie "$venv/bin/python" -m pip install \
        --disable-pip-version-check --no-input --no-cache-dir --force-reinstall 'cowrie==3.0.15'
fi

if [[ ! -f "$state/etc/cowrie.cfg" ]]; then
    echo 'Initializing Cowrie state directory.'
    runuser -u cowrie -- sh -c 'cd /opt/cowrie/honeypot && ./cowrie-env/bin/cowrie init'
fi
if [[ -e "$state/cowrie.cfg" ]]; then
    echo 'ERROR: flat cowrie.cfg would override managed etc/cowrie.cfg; remove it first.' >&2
    exit 1
fi

# Install every config and unit from staging before starting either service.
install_managed() {
    src="$1"
    dest="$2"
    owner="$3"
    group="$4"
    mode="$5"
    if [[ -f "$dest" ]] && cmp -s "$src" "$dest" && \
       [[ "$(stat -c '%U:%G:%a' "$dest")" == "$owner:$group:${mode#0}" ]]; then
        return
    fi
    tmp="${dest}.new.$$"
    install -o "$owner" -g "$group" -m "$mode" "$src" "$tmp"
    mv -f -- "$tmp" "$dest"
}
install -d -o cowrie -g cowrie -m 0750 "$state/etc"
install_managed "$stage/cowrie.cfg" "$state/etc/cowrie.cfg" cowrie cowrie 0600
install_managed "$stage/userdb.txt" "$state/etc/userdb.txt" cowrie cowrie 0600
install_managed "$stage/delay_proxy.py" /opt/cowrie/delay_proxy.py root root 0644
install_managed "$stage/cowrie.service" /etc/systemd/system/cowrie.service root root 0644
install_managed "$stage/cowrie-delay.service" /etc/systemd/system/cowrie-delay.service root root 0644

systemctl daemon-reload
systemctl enable cowrie.service cowrie-delay.service >/dev/null
systemctl start cowrie.service
systemctl start cowrie-delay.service
REMOTE_INSTALL
case "$install_status" in
    0) ;;
    255) exit 2 ;;
    20) exit 3 ;;
    1|5|6|7) exit "$install_status" ;;
    *) exit 7 ;;
esac

# A successful install can still be starting. Leave the grace period to the
# next minute's read-only check instead of repeatedly polling or reinstalling.
status=0
verify_remote || status=$?
case "$status" in
    0) printf 'Cowrie 3.0.15 and delay proxy are healthy on node 24.\n' ;;
    5) printf 'Cowrie/proxy startup is pending; the next minute check will verify readiness.\n' ;;
    255) exit 2 ;;
    20) exit 3 ;;
    1|6|7) exit "$status" ;;
    *) exit 7 ;;
esac
exit "$status"
