#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
ENABLE_VNC="${ENABLE_VNC:-1}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

if [[ "${ENABLE_VNC}" == "1" || "${ENABLE_VNC,,}" == "true" ]]; then
  echo "[docker] starting Xvfb on ${DISPLAY}"
  Xvfb "${DISPLAY}" -screen 0 1366x900x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &

  echo "[docker] starting fluxbox window manager"
  fluxbox >/tmp/fluxbox.log 2>&1 &

  echo "[docker] starting x11vnc on :${VNC_PORT}"
  x11vnc \
    -display "${DISPLAY}" \
    -rfbport "${VNC_PORT}" \
    -forever \
    -shared \
    -nopw \
    >/tmp/x11vnc.log 2>&1 &

  NOVNC_WEB_ROOT="/usr/share/novnc"
  if [[ ! -d "${NOVNC_WEB_ROOT}" ]]; then
    NOVNC_WEB_ROOT="/usr/share/novnc/"
  fi
  echo "[docker] starting noVNC on :${NOVNC_PORT} (web=${NOVNC_WEB_ROOT})"
  websockify --web="${NOVNC_WEB_ROOT}" "${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" >/tmp/novnc.log 2>&1 &
fi

echo "[docker] starting webui..."
exec python webui.py

