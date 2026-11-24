#!/bin/bash
# Deploy the COMMITTED code to the Orin and run the regression tests there (run on the PC, in Git Bash).
# Refuses uncommitted changes, so what runs on the Orin is always a known commit; the commit id is written to
# ~/orin-agent/COMMIT and appears in every result (DeidentificationMethod, reports, JSON sidecars).
#   bash bin/deploy.sh [host]        (default host: orin)
# Root-installed helpers (medical-vault, orin-offline, orin-usb) still need their setup scripts with sudo.
set -e
HOST=${1:-orin}
cd "$(dirname "$0")/.."
if ! git diff --quiet || ! git diff --cached --quiet; then echo "uncommitted changes - commit first"; exit 1; fi
c=$(git rev-parse HEAD)
git archive --format=tar HEAD | ssh "$HOST" 'mkdir -p ~/orin-agent && tar -x -C ~/orin-agent'
ssh "$HOST" "echo $c > ~/orin-agent/COMMIT && cd ~/orin-agent && ~/dicom-env/bin/python -W ignore tests/run_tests.py | tail -1"
echo "deployed $(git log --oneline -1)"
