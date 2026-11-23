#!/usr/bin/env bash
# Stable localhost.run tunnel to 9Router (port 20128) with SSH key auth.
# Register ~/.ssh/id_ed25519.pub at https://admin.localhost.run first;
# the subdomain then stays the same across reconnects.
#
# If the VM only reaches the internet through an HTTP CONNECT proxy,
# keep the ProxyCommand option and export https_proxy (see proxy.env).
# If direct egress works, delete the ProxyCommand option.
set -u
cd "$HOME/muse-bridge"
exec ssh -i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -o "ProxyCommand=python3 $HOME/muse-bridge/connect_proxy.py %h %p" \
  -R 80:localhost:20128 localhost.run < /dev/null 2>&1 | tee -a "$HOME/muse-bridge/tunnel.log"
