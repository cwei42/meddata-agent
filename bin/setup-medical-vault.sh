#!/bin/bash
# One-time setup of the encrypted medical volume. Run interactively:  ssh -t orin sudo bash setup-medical-vault.sh
set -e
SIZE=400G
DIR=/var/lib/medical-vault
IMG=$DIR/vault.img
NAME=medical
MNT=/data/medical
OWNER=meduser
SRC="$(dirname "$0")/medical-vault"

[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
if [ -e "$IMG" ]; then echo "$IMG already exists - nothing to do (use medical-vault unlock)"; exit 1; fi

echo "== [1/5] install medical-vault tool + sudo rule"
install -m 0755 -o root -g root "$SRC" /usr/local/sbin/medical-vault
TMP=$(mktemp)
echo "$OWNER ALL=(root) NOPASSWD: /usr/local/sbin/medical-vault" > "$TMP"
visudo -cf "$TMP" >/dev/null && install -m 0440 -o root -g root "$TMP" /etc/sudoers.d/91-medical-vault
rm -f "$TMP"

echo "== [2/5] create $SIZE container file"
mkdir -p "$DIR" && chmod 700 "$DIR"
fallocate -l "$SIZE" "$IMG"
chmod 600 "$IMG"

echo
echo "== [3/5] encrypt (LUKS2)"
echo "   Choose a strong passphrase for the medical volume and WRITE IT DOWN somewhere safe."
echo "   If it is lost, the data in the volume can NOT be recovered."
echo
cryptsetup luksFormat --type luks2 --batch-mode --verify-passphrase --label medical-vault "$IMG"

echo
echo "== [4/5] open (enter the same passphrase once more) and create filesystem"
cryptsetup open --type luks2 "$IMG" "$NAME"
mkfs.ext4 -q -L medical -m 0 "/dev/mapper/$NAME"

echo "== [5/5] mount at $MNT"
mkdir -p "$MNT"
mount -o nodev,nosuid,noexec "/dev/mapper/$NAME" "$MNT"
chown "$OWNER:$OWNER" "$MNT" && chmod 700 "$MNT"
# cases are created on import: $MNT/<case>/original, $MNT/<case>/<step>
chown -R "$OWNER:$OWNER" "$MNT"
df -h "$MNT" | tail -1
echo
echo "== MEDICAL VAULT READY: $MNT (locked again after reboot; unlock with: sudo medical-vault unlock)"
