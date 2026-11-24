#!/bin/bash
# Stop the web interface.
if pkill -f "python web/server.py"; then echo "UI stopped"; else echo "UI was not running"; fi
