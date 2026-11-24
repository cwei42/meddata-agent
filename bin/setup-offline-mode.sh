#!/bin/bash
# One-time: install orin-offline + orin-usb helpers, sudo rules and the orin-console command.
#   ssh -t orin sudo bash ~/orin-agent/bin/setup-offline-mode.sh
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
OWNER=${SUDO_USER:-meduser}
HOME_DIR=$(getent passwd "$OWNER" | cut -d: -f6)

echo "== install helpers"
install -m 0755 -o root -g root "$DIR/orin-offline" /usr/local/sbin/orin-offline
install -m 0755 -o root -g root "$DIR/orin-usb" /usr/local/sbin/orin-usb
touch /var/log/orin-offline.log && chmod 644 /var/log/orin-offline.log

echo "== sudo rules (only these two commands, no password)"
TMP=$(mktemp)
cat > "$TMP" <<EOF
$OWNER ALL=(root) NOPASSWD: /usr/local/sbin/orin-offline, /usr/local/sbin/orin-usb
EOF
visudo -cf "$TMP" >/dev/null && install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/92-orin-offline
rm -f "$TMP"

echo "== orin-console command"
cat > /usr/local/bin/orin-console <<EOF
#!/bin/bash
exec $HOME_DIR/dicom-env/bin/python $HOME_DIR/orin-agent/console.py "\$@"
EOF
chmod 755 /usr/local/bin/orin-console

echo "== login hint on the local screen (tty1 only)"
PROFILE="$HOME_DIR/.bash_profile"
if ! grep -q orin-console "$PROFILE" 2>/dev/null; then
  cat >> "$PROFILE" <<'EOF'
[ -f ~/.bashrc ] && . ~/.bashrc
if [ "$(tty)" = /dev/tty1 ]; then echo; echo "  Orin offline console: type  orin-console"; echo; fi
EOF
  chown "$OWNER:$OWNER" "$PROFILE"
fi
echo "== OFFLINE MODE SETUP DONE"
