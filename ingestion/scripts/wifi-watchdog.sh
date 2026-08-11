#!/usr/bin/env bash
#
# wifi-watchdog.sh — recover the Pi from a wifi drop without manual intervention.
#
# Run from cron every minute with flock to prevent overlap. Must run with sudo
# so it can write to /var/log (which survives reboots), e.g.:
#   * * * * * flock -n /tmp/wifi-watchdog.lock sudo -n /home/austinblanton/Desktop/deviated-septa/ingestion/scripts/wifi-watchdog.sh
#
# Behavior:
#   - Canary: ping the default gateway, then 8.8.8.8 as a fallback. Only count a
#     failure when BOTH are unreachable (so a dead router doesn't reboot the Pi).
#   - RECONNECT_THRESHOLD consecutive failures: force `nmcli connection up`.
#   - REBOOT_THRESHOLD consecutive failures: write a persistent marker, then
#     reboot via systemd.
#   - Failure count resets to 0 on the first successful check.
#   - The log lives in /var/log so it survives the reboot; the first successful
#     check after a watchdog-initiated reboot logs "link restored via reboot".
#
# Combine with `nmcli connection modify "Verizon_CK4G7P" connection.autoconnect-retries -1`
# so NetworkManager keeps trying on its own between watchdog runs.

set -u

CONN_NAME="Verizon_CK4G7P"
LOG_FILE="/var/log/wifi-watchdog.log"
STATE_FILE="/tmp/wifi-watchdog.failures"
MARKER_FILE="/var/log/wifi-watchdog.rebooted"
RECONNECT_THRESHOLD=3
REBOOT_THRESHOLD=6
PING_TIMEOUT=2

fail_count=0
if [ -f "$STATE_FILE" ]; then
    fail_count=$(cat "$STATE_FILE" 2>/dev/null)
    case "$fail_count" in
        ''|*[!0-9]*) fail_count=0 ;;
    esac
fi

is_online() {
    local gw
    gw=$(ip -4 route show default | awk '{print $3; exit}')
    if [ -n "$gw" ] && ping -q -c 1 -W "$PING_TIMEOUT" "$gw" >/dev/null 2>&1; then
        return 0
    fi
    ping -q -c 1 -W "$PING_TIMEOUT" 8.8.8.8 >/dev/null 2>&1
}

if is_online; then
    if [ -f "$MARKER_FILE" ]; then
        boot=$(uptime -s 2>/dev/null || echo unknown)
        echo "$(date '+%F %T') link restored via reboot (boot $boot)" >> "$LOG_FILE"
        rm -f "$MARKER_FILE"
    elif [ "$fail_count" -gt 0 ]; then
        echo "$(date '+%F %T') link restored after $fail_count failed checks" >> "$LOG_FILE"
    fi
    rm -f "$STATE_FILE"
    exit 0
fi

fail_count=$((fail_count + 1))
echo "$fail_count" > "$STATE_FILE"
echo "$(date '+%F %T') no connectivity (failure $fail_count)" >> "$LOG_FILE"

if [ "$fail_count" -eq "$RECONNECT_THRESHOLD" ]; then
    echo "$(date '+%F %T') attempting wifi reconnect ($CONN_NAME)" >> "$LOG_FILE"
    sudo -n nmcli connection up "$CONN_NAME" >> "$LOG_FILE" 2>&1
elif [ "$fail_count" -ge "$REBOOT_THRESHOLD" ]; then
    echo "$(date '+%F %T') rebooting after $fail_count failed checks" >> "$LOG_FILE"
    date '+%F %T' > "$MARKER_FILE"
    sudo -n systemctl reboot
fi
