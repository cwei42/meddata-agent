#!/bin/bash
# Switch from the old persistent vault (fixed passphrase) to a new encrypted space per session.
# Run once, interactively:  ssh -t orin sudo bash ~/orin-agent/bin/setup-session-vault.sh
set -u
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
HERE=$(dirname "$(readlink -f "$0")")
OLD=/var/lib/medical-vault/vault.img
MNT=/data/medical

echo "== [1/4] install medical-vault (start / end / status / cleanup)"
install -m 0755 -o root -g root "$HERE/medical-vault" /usr/local/sbin/medical-vault
[ -f /etc/sudoers.d/91-medical-vault ] || {
  echo "meduser ALL=(root) NOPASSWD: /usr/local/sbin/medical-vault" > /etc/sudoers.d/91-medical-vault
  chmod 440 /etc/sudoers.d/91-medical-vault; }

echo "== [2/4] boot cleanup service (removes the file of a session that was not ended; its key is already gone)"
cat > /etc/systemd/system/medical-vault-cleanup.service <<'EOF'
[Unit]
Description=Remove leftover medical session space (its key was lost at shutdown)
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/medical-vault cleanup

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable -q medical-vault-cleanup.service

echo "== [3/4] remove the old persistent vault"
if [ -e "$OLD" ]; then
  if mountpoint -q "$MNT"; then
    echo "   it is open and contains: $(ls -A "$MNT" | grep -v lost+found | tr '\n' ' ')"
  fi
  read -rp "   Destroy the old vault and everything in it? Type YES: " a
  if [ "$a" = "YES" ]; then
    mountpoint -q "$MNT" && { sync; umount "$MNT" || { echo "   files still in use:"; fuser -vm "$MNT"; exit 1; }; }
    [ -e /dev/mapper/medical ] && cryptsetup close medical
    cryptsetup luksErase --batch-mode "$OLD" && rm -f "$OLD" && echo "   old vault erased (key slots destroyed) and deleted"
    logger -t medical-vault "old persistent vault erased and deleted by ${SUDO_USER:-root}"
  else
    echo "   kept - nothing removed; sessions cannot start while it is open (lock it first)"
  fi
else
  echo "   none"
fi

echo "== [4/4] test: start a session, write, end, check it is gone"
U=${SUDO_USER:-meduser}
if sudo -u "$U" sudo -n /usr/local/sbin/medical-vault start; then
  sudo -u "$U" sh -c "echo test > $MNT/probe.txt" && echo "   wrote $MNT/probe.txt"
  sudo -u "$U" sudo -n /usr/local/sbin/medical-vault end
  if [ ! -e "$MNT/probe.txt" ] && [ ! -e /var/lib/medical-vault/session.img ] && [ ! -e /dev/mapper/medical ]; then
    echo "== OK: session spaces work (nothing left after end)"
  else
    echo "== WARNING: something was left after end"; exit 1
  fi
else
  echo "== test FAILED"; exit 1
fi
