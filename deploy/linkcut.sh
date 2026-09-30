#!/bin/sh
# Simulate losing the vessel uplink during the demo, without touching the Mac's own connection.
#
#   sudo ./deploy/linkcut.sh on      # node loses the internet; Mac <-> node LAN stays up
#   sudo ./deploy/linkcut.sh off     # restore
#   sudo ./deploy/linkcut.sh status
#
# The node sits behind macOS Internet Sharing (bridge100, 192.168.2.0/24). Turning the Mac's
# Wi-Fi off makes macOS tear the bridge down, which also takes the demo UI away. Instead this
# loads a pf rule into an anchor that the stock /etc/pf.conf already evaluates (com.apple/*)
# and rejects everything the node sends beyond the local subnet.
set -e
NODE=${NODE:-192.168.2.2}
LAN=${LAN:-192.168.2.0/24}
ANCHOR=com.apple/250.SMAGIC26LinkCut

case "$1" in
  on)
    echo "block return in quick on bridge100 inet from $NODE to ! $LAN" | pfctl -q -a "$ANCHOR" -f -
    pfctl -q -E 2>/dev/null || true
    # Established connections bypass new rules; drop the node's existing states so it notices now
    pfctl -q -k "$NODE" 2>/dev/null || true
    echo "link CUT: $NODE can reach $LAN only"
    ;;
  off)
    pfctl -q -a "$ANCHOR" -F rules
    echo "link RESTORED for $NODE"
    ;;
  status)
    if pfctl -a "$ANCHOR" -s rules 2>/dev/null | grep -q block; then echo "link CUT"; else echo "link up"; fi
    ;;
  *)
    echo "usage: sudo $0 on|off|status" >&2
    exit 2
    ;;
esac
