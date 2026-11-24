#!/bin/bash
# Stop the local LLM server and free its memory.
if pkill -x llama-server; then sleep 1; echo "LLM server stopped"; else echo "LLM server was not running"; fi
