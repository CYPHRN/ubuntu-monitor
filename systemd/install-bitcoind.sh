#!/usr/bin/env bash
#
# Install (or update) the headless Bitcoin Core system service, take over any
# manually-started bitcoind, and enable it at boot.
#
# Run as your normal user (NOT root) -- it calls sudo only for the privileged
# steps and runs bitcoin-cli as you:
#
#     ./systemd/install-bitcoind.sh            # install + enable + start now
#     ./systemd/install-bitcoind.sh --no-start # install + enable, don't touch
#                                              # a running node (takes over next boot)
#     ./systemd/install-bitcoind.sh --disabled # install but leave OFF (the
#                                              # "have it off" option)
#
# Portability (other devices): edit the unit's User/Group, the -conf/-datadir
# paths, RequiresMountsFor, and the bitcoind path, OR override the vars below.
set -euo pipefail

SERVICE_USER="${SERVICE_USER:-cypher}"
CONF="${CONF:-/home/${SERVICE_USER}/.bitcoin/bitcoin.conf}"
BITCOIN_CLI="${BITCOIN_CLI:-/usr/local/bin/bitcoin-cli}"
UNIT_SRC="$(cd "$(dirname "$0")" && pwd)/bitcoind.service"
UNIT_DST="/etc/systemd/system/bitcoind.service"

MODE="start"
case "${1:-}" in
  --no-start) MODE="no-start" ;;
  --disabled) MODE="disabled" ;;
  "" )        MODE="start" ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

echo ">> installing $UNIT_DST"
sudo install -m 0644 -o root -g root "$UNIT_SRC" "$UNIT_DST"
sudo systemctl daemon-reload

# The GUI autostart and this daemon fight over the same datadir lock. Disable it.
if systemctl --user is-enabled bitcoin-qt.service >/dev/null 2>&1; then
  echo ">> disabling conflicting bitcoin-qt.service (GUI) user autostart"
  systemctl --user disable --now bitcoin-qt.service || true
fi

if [ "$MODE" = "disabled" ]; then
  echo ">> leaving service DISABLED (turn on later with: sudo systemctl enable --now bitcoind)"
  exit 0
fi

echo ">> enabling at boot"
sudo systemctl enable bitcoind.service

if [ "$MODE" = "no-start" ]; then
  echo ">> not starting now; it takes over cleanly on next reboot"
  exit 0
fi

# Take over a manually-started node: stop it cleanly and wait for the datadir
# lock to be released before systemd starts its own copy.
if pgrep -x bitcoind >/dev/null 2>&1; then
  echo ">> stopping the manually-started bitcoind (clean shutdown, may take a bit)"
  "$BITCOIN_CLI" -conf="$CONF" stop || true
  for _ in $(seq 1 150); do
    pgrep -x bitcoind >/dev/null 2>&1 || break
    sleep 2
  done
  if pgrep -x bitcoind >/dev/null 2>&1; then
    echo "!! bitcoind still running after wait; aborting so we never double-lock the datadir" >&2
    exit 1
  fi
fi

echo ">> starting bitcoind under systemd"
sudo systemctl start bitcoind.service
sleep 3
sudo systemctl --no-pager --full status bitcoind.service | head -n 12 || true
echo ">> node check:"
"$BITCOIN_CLI" -conf="$CONF" -getinfo 2>&1 | head -6 || echo "(RPC not ready yet; give it a few seconds)"
