#!/bin/bash
# setup-sandbox.sh - let bubblewrap create its sandbox on Ubuntu 24.04 (run once: sudo bash setup-sandbox.sh)
#
# Ubuntu 24.04 blocks unprivileged user namespaces unless an AppArmor profile allows them
# (kernel.apparmor_restrict_unprivileged_userns=1). This adds such a profile for /usr/bin/bwrap only -
# the same way Ubuntu does it for browsers. The restriction stays on for every other program.
set -e
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
[ -x /usr/bin/bwrap ] || apt-get install -y bubblewrap
cat > /etc/apparmor.d/bwrap <<'EOF'
# allow bubblewrap (used by orin-agent run_python sandbox) to create user namespaces
abi <abi/4.0>,
include <tunables/global>

profile bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
  include if exists <local/bwrap>
}
EOF
apparmor_parser -r /etc/apparmor.d/bwrap
U=${SUDO_USER:-meduser}
if sudo -u "$U" bwrap --unshare-all --die-with-parent --ro-bind /usr /usr --symlink usr/lib /lib \
     --symlink usr/bin /bin --proc /proc --dev /dev /bin/sh -c 'tail -n +3 /proc/net/dev' | grep -c : | grep -qx 1; then
  echo "sandbox OK (only loopback network inside)"
else
  echo "sandbox test FAILED"; exit 1
fi
