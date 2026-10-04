#!/usr/bin/env bash
# Streamed over management SSH; all mutations run as root on the target.
RECOVERY_DIR=/run/cowrie-recovery
STARTUP_GRACE=180
REPAIR_INTERVAL=300

configuration_health() (

cfg_hash="$1"
userdb_hash="$2"
cowrie_unit_hash="$3"
delay_unit_hash="$4"
proxy_hash="$5"
authorized_keys_hash="$6"
state=/opt/cowrie/honeypot
student_home="$(getent passwd student-admin 2>/dev/null | awk -F: 'NR == 1 { print $6 }')" || true

unhealthy() {
    printf 'Cowrie unhealthy: %s\n' "$1" >&2
    exit 1
}
matches_hash() {
    [[ -f "$2" ]] && printf '%s  %s\n' "$1" "$2" | sha256sum -c --status
}
matches_attrs() {
    [[ -f "$2" ]] && [[ "$(stat -c '%U:%G:%a' "$2")" == "$1" ]]
}

[[ -n "$student_home" ]] || unhealthy 'student-admin account is missing'
matches_hash "$authorized_keys_hash" "$student_home/.ssh/authorized_keys" || {
    printf 'Cowrie unhealthy: authorized_keys does not match the group public key.\n' >&2
    exit 20
}
[[ "${EUID}" == 0 ]] || exit 7
getent passwd cowrie >/dev/null || unhealthy 'cowrie user is missing'
getent group cowrie >/dev/null || unhealthy 'cowrie group is missing'
[[ "$(stat -c '%U:%G:%a' /opt/cowrie 2>/dev/null)" == root:root:755 ]] || unhealthy '/opt/cowrie directory ownership or permissions differ'
[[ "$(stat -c '%U:%G:%a' "$state" 2>/dev/null)" == cowrie:cowrie:750 ]] || unhealthy 'Cowrie state directory ownership or permissions differ'
[[ ! -e "$state/cowrie.cfg" ]] || unhealthy 'flat cowrie.cfg overrides managed configuration'
[[ -x "$state/cowrie-env/bin/python" ]] || unhealthy 'Cowrie virtual environment is missing'
[[ -x "$state/cowrie-env/bin/cowrie" ]] || unhealthy 'Cowrie command is missing'
version="$("$state/cowrie-env/bin/python" -c 'from importlib.metadata import version; print(version("cowrie"))' 2>/dev/null)" || unhealthy 'Cowrie package is missing'
[[ "$version" == 3.0.15 ]] || unhealthy "Cowrie version is $version, expected 3.0.15"
matches_hash "$cfg_hash" "$state/etc/cowrie.cfg" || unhealthy 'cowrie.cfg differs'
matches_hash "$userdb_hash" "$state/etc/userdb.txt" || unhealthy 'userdb.txt differs'
matches_hash "$cowrie_unit_hash" /etc/systemd/system/cowrie.service || unhealthy 'cowrie.service differs'
matches_hash "$delay_unit_hash" /etc/systemd/system/cowrie-delay.service || unhealthy 'cowrie-delay.service differs'
matches_hash "$proxy_hash" /opt/cowrie/delay_proxy.py || unhealthy 'delay proxy differs'
matches_attrs cowrie:cowrie:600 "$state/etc/cowrie.cfg" || unhealthy 'cowrie.cfg ownership or permissions differ'
matches_attrs cowrie:cowrie:600 "$state/etc/userdb.txt" || unhealthy 'userdb.txt ownership or permissions differ'
matches_attrs root:root:644 /etc/systemd/system/cowrie.service || unhealthy 'cowrie.service ownership or permissions differ'
matches_attrs root:root:644 /etc/systemd/system/cowrie-delay.service || unhealthy 'cowrie-delay.service ownership or permissions differ'
matches_attrs root:root:644 /opt/cowrie/delay_proxy.py || unhealthy 'delay proxy ownership or permissions differ'
systemctl is-enabled --quiet cowrie.service || unhealthy 'cowrie.service is disabled'
systemctl is-enabled --quiet cowrie-delay.service || unhealthy 'cowrie-delay.service is disabled'

)

monotonic_seconds() {
    # Container VMs can virtualize /proc/uptime while systemd timestamps still
    # use CLOCK_MONOTONIC. Match systemd's clock when calculating service age.
    python3 -c 'import time; print(int(time.monotonic()))'
}

boot_age_seconds() {
    awk '{ print int($1) }' /proc/uptime
}

read_timestamp() {
    local value=-1
    if [[ -f "$RECOVERY_DIR/$1" && ! -L "$RECOVERY_DIR/$1" ]]; then
        read -r value < "$RECOVERY_DIR/$1" || return 7
        [[ "$value" =~ ^[0-9]+$ ]] || return 7
    fi
    printf '%s\n' "$value"
}

# Capture sockets and systemd state once; a failed observation is inconclusive.
runtime_health() {
    local sockets props now recent restart reconcile unit key value active started restarts age
    local pending=false failed=false
    FAILED_UNITS=()
    sockets="$(ss -H -ltn)" || return 7
    now="$(monotonic_seconds)" || return 7
    [[ "$now" =~ ^[0-9]+$ ]] || return 7
    restart="$(read_timestamp restart)" || return 7
    reconcile="$(read_timestamp reconcile)" || return 7
    recent=$restart
    (( reconcile > recent )) && recent=$reconcile
    if awk '$4 ~ /:2222$/ && $4 != "127.0.0.1:2222" { bad=1 } END { exit !bad }' <<< "$sockets"; then
        echo 'Cowrie unhealthy: Cowrie has an unexpected binding on port 2222' >&2
        return 1
    fi
    for unit in cowrie.service cowrie-delay.service; do
        active='' started='' restarts=''
        props="$(systemctl show "$unit" -p ActiveState -p ExecMainStartTimestampMonotonic -p NRestarts)" || return 7
        while IFS='=' read -r key value; do
            case "$key" in
                ActiveState) active=$value ;;
                ExecMainStartTimestampMonotonic) started=$value ;;
                NRestarts) restarts=$value ;;
            esac
        done <<< "$props"
        [[ "$started" =~ ^[0-9]+$ && "$restarts" =~ ^[0-9]+$ ]] || return 7
        (( started / 1000000 <= now )) || return 7
        case "$active" in active|activating|inactive|failed|deactivating) ;; *) return 7 ;; esac
        if [[ "$active" == active ]]; then
            if [[ "$unit" == cowrie.service ]]; then
                if awk '$4 == "127.0.0.1:2222" { found=1 } END { exit !found }' <<< "$sockets"; then
                    continue
                fi
                echo 'Cowrie not ready: port 2222 is not listening' >&2
            else
                if awk '$4 ~ /^(0[.]0[.]0[.]0|[*]):22001$/ { found=1 } END { exit !found }' <<< "$sockets"; then
                    continue
                fi
                echo 'Proxy not ready: public port 22001 is not listening' >&2
            fi
        else
            printf '%s is %s\n' "$unit" "$active" >&2
        fi
        FAILED_UNITS+=("$unit")
        age=$((now - started / 1000000))
        # Automatic restart loops cannot renew grace forever: cap their grace
        # at boot, or the last explicit recovery action on this boot.
        if (( restarts > 0 )); then
            age="$(boot_age_seconds)" || return 7
            [[ "$age" =~ ^[0-9]+$ ]] || return 7
            (( recent >= 0 )) && age=$((now - recent))
        fi
        if (( recent >= 0 && now >= recent && now - recent < STARTUP_GRACE )) ||
           { [[ "$active" == active || "$active" == activating ]] && (( started > 0 && age >= 0 && age < STARTUP_GRACE )); }; then
            pending=true
        else
            failed=true
        fi
    done
    if "$pending"; then
        echo 'Cowrie/proxy starting; allowing 180 seconds before repair' >&2
        return 5
    fi
    if "$failed"; then return 6; fi
    return 0
}

remote_health() {
    local status=0
    configuration_health "$@" || status=$?
    (( status == 0 )) || return "$status"
    runtime_health || status=$?
    if (( status == 7 )); then
        echo 'Cowrie health inconclusive: socket, clock, or systemd readiness data is unavailable' >&2
    fi
    return "$status"
}

prepare_recovery_directory() {
    [[ "$EUID" == 0 && ! -L "$RECOVERY_DIR" ]] || return 7
    mkdir -p "$RECOVERY_DIR" || return 7
    [[ "$(stat -c '%U:%G' "$RECOVERY_DIR")" == root:root ]] || return 7
    chmod 700 "$RECOVERY_DIR" || return 7
}

recovery_lock() {
    prepare_recovery_directory || return $?
    [[ ! -L "$RECOVERY_DIR/lock" ]] || return 7
    exec 8>"$RECOVERY_DIR/lock" || return 7
    if ! flock -n 8; then
        echo 'Another target recovery holds the lock; deferring repair' >&2
        return 5
    fi
}

write_timestamp() {
    local tmp
    tmp="$(mktemp "$RECOVERY_DIR/.timestamp.XXXXXX")" || return 7
    printf '%s\n' "$2" > "$tmp" || return 7
    mv -f -- "$tmp" "$RECOVERY_DIR/$1" || return 7
}

reserve_reconciliation() {
    local now last
    now="$(monotonic_seconds)" || return 7
    last="$(read_timestamp reconcile)" || return 7
    if (( last >= 0 && now >= last && now - last < REPAIR_INTERVAL )); then
        echo 'Full reconciliation already attempted within five minutes; deferring' >&2
        return 6
    fi
    write_timestamp reconcile "$now"
}

recover_runtime() {
    local status=0 now restart reconcile
    recovery_lock || return $?
    remote_health "$@" || status=$?
    (( status == 6 )) || return "$status"
    now="$(monotonic_seconds)" || return 7
    restart="$(read_timestamp restart)" || return 7
    reconcile="$(read_timestamp reconcile)" || return 7
    if (( restart >= 0 && now >= restart && now - restart < STARTUP_GRACE )); then
        return 5
    fi
    if (( reconcile >= 0 && now >= reconcile && now - reconcile < REPAIR_INTERVAL )); then
        echo 'Service recovery follows a recent reconciliation; deferring further mutations' >&2
        return 6
    fi
    if (( restart >= 0 && restart > reconcile )); then
        echo 'Targeted restart did not restore readiness; requesting full reconciliation' >&2
        return 1
    fi
    if (( restart >= 0 && now >= restart && now - restart < REPAIR_INTERVAL )); then
        return 6
    fi
    write_timestamp restart "$now" || return $?
    if [[ " ${FAILED_UNITS[*]} " == *' cowrie.service '* ]]; then
        echo 'Restarting Cowrie and its dependent proxy' >&2
        systemctl restart cowrie.service cowrie-delay.service || echo 'Targeted Cowrie restart failed; allowing grace before reconciliation' >&2
    else
        echo 'Restarting only the delay proxy' >&2
        systemctl restart cowrie-delay.service || echo 'Targeted proxy restart failed; allowing grace before reconciliation' >&2
    fi
    status=0
    remote_health "$@" || status=$?
    case "$status" in
        0) return 0 ;;
        6) return 5 ;;
        *) return "$status" ;;
    esac
}
