#!/bin/bash
# Start the web interface (127.0.0.1:8090, reachable only through an SSH tunnel).
cd "$(dirname "$0")/.."
if curl -s -o /dev/null 127.0.0.1:8090/api/model; then echo "UI already running"; exit 0; fi
setsid nohup ~/dicom-env/bin/python web/server.py > ~/orin-agent-ui.log 2>&1 < /dev/null &
for i in $(seq 1 20); do
  curl -s -o /dev/null 127.0.0.1:8090/api/model && { echo "UI ready on 127.0.0.1:8090"; exit 0; }
  sleep 0.5
done
echo "UI failed to start - see ~/orin-agent-ui.log"; exit 1
