#!/usr/bin/env bash
# Muse hook (optional): survive VM replacement.
# Poll every 30 seconds. Restores missing systemd units from the copies in
# ~/muse-bridge/systemd/, starts inactive services, and wakes an agent if a
# service stays down or the tunnel URL changed. Silent when healthy.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

UNITS_DIR="$HOME/muse-bridge/systemd"
STATE_FILE="$HOME/hooks/state/muse-bridge-watchdog-url.txt"
SERVICES=(9router muse-bridge muse-tunnel)
repaired=0
still_down=()

for svc in "${SERVICES[@]}"; do
  if [[ ! -f "/etc/systemd/system/${svc}.service" ]]; then
    if [[ -f "${UNITS_DIR}/${svc}.service" ]]; then
      cp "${UNITS_DIR}/${svc}.service" "/etc/systemd/system/${svc}.service"
      systemctl daemon-reload || true
      repaired=1
    fi
  fi
  if ! systemctl is-active --quiet "${svc}" 2>/dev/null; then
    systemctl start "${svc}" 2>/dev/null || true
    repaired=1
  fi
done

sleep 3
for svc in "${SERVICES[@]}"; do
  if ! systemctl is-active --quiet "${svc}" 2>/dev/null; then
    still_down+=("${svc}")
  fi
done

current_url="$(grep -oE 'https://[a-z0-9.-]+\.lhr\.life' "$HOME/muse-bridge/tunnel.log" 2>/dev/null | tail -1 || true)"
last_url=""
[[ -f "${STATE_FILE}" ]] && last_url="$(cat "${STATE_FILE}" 2>/dev/null || true)"
url_changed=0
if [[ -n "${current_url}" && "${current_url}" != "${last_url}" ]]; then
  url_changed=1
  if [[ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]]; then
    mkdir -p "$(dirname "${STATE_FILE}")"
    printf '%s' "${current_url}" > "${STATE_FILE}"
  fi
fi

log "watchdog check" "{\"repaired\":${repaired},\"down\":\"${still_down[*]:-}\",\"url_changed\":${url_changed}}"

if [[ ${#still_down[@]} -gt 0 ]]; then
  wake "muse-bridge services still down after repair attempt" "{\"down\":\"${still_down[*]}\",\"url\":\"${current_url}\"}"
elif [[ "${url_changed}" == "1" ]]; then
  wake "muse-bridge tunnel URL changed" "{\"url\":\"${current_url}\",\"repaired\":${repaired}}"
else
  silent "all muse-bridge services active, tunnel URL unchanged" "{\"url\":\"${current_url}\"}"
fi
