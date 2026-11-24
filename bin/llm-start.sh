#!/bin/bash
# Start the local LLM server (localhost only). Usage: llm-start.sh [4b|2b]
SIZE=${1:-4b}
case $SIZE in
  4b) M=~/models/qwen3.5-4b/Qwen3.5-4B-Q4_K_M.gguf; P=~/models/qwen3.5-4b/mmproj-Qwen3.5-4B-Q8_0.gguf ;;
  2b) M=~/models/qwen3.5-2b/Qwen3.5-2B-Q4_K_M.gguf; P=~/models/qwen3.5-2b/mmproj-F16.gguf ;;
  *) echo "usage: $0 [4b|2b]"; exit 1 ;;
esac
if pgrep -x llama-server >/dev/null; then echo "LLM server already running"; exit 0; fi
nohup ~/llama.cpp/build/bin/llama-server -m "$M" --mmproj "$P" \
  --host 127.0.0.1 --port 8080 -c 12288 -np 1 --cache-ram 512 > ~/llama-server.log 2>&1 &
# -np 1: one conversation at a time; --cache-ram 512: prompt cache max 512 MB
# (default 8192 MB = all RAM of the Orin -> llama-server was killed by the OOM killer in long agent runs)
echo -n "starting Qwen3.5-$SIZE "
for i in $(seq 1 60); do
  if curl -s 127.0.0.1:8080/health | grep -q '"ok"'; then echo " ready (http://127.0.0.1:8080)"; exit 0; fi
  echo -n "."; sleep 2
done
echo " failed - see ~/llama-server.log"; exit 1
