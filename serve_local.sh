#!/usr/bin/env bash
# Start the local "image-capable Jev" (llama.cpp / Metal).
#
#   ./serve_local.sh
#
# Swap the model:
#   MODEL_DIR=~/models/Qwen3-VL-4B-Instruct-GGUF PORT=8081 ./serve_local.sh
#
# Requires: llama-server (Homebrew llama.cpp) and a downloaded GGUF + mmproj.
#   huggingface-cli download Qwen/Qwen3-VL-4B-Instruct-GGUF \
#     --include "Qwen3VL-4B-Instruct-Q4_K_M.gguf" "mmproj-Qwen3VL-4B-Instruct-F16.gguf" \
#     --local-dir ~/models/Qwen3-VL-4B-Instruct-GGUF
set -euo pipefail

MODEL_DIR="${MODEL_DIR:-$HOME/models/Qwen3-VL-4B-Instruct-GGUF}"
MODEL_FILE="${MODEL_FILE:-$MODEL_DIR/Qwen3VL-4B-Instruct-Q4_K_M.gguf}"
MMPROJ_FILE="${MMPROJ_FILE:-$MODEL_DIR/mmproj-Qwen3VL-4B-Instruct-F16.gguf}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8081}"
CTX="${CTX:-8192}"

[ -f "$MODEL_FILE" ] || { echo "missing model file: $MODEL_FILE" >&2; exit 1; }
[ -f "$MMPROJ_FILE" ] || { echo "missing mmproj: $MMPROJ_FILE" >&2; exit 1; }

echo ">>> llama-server  $MODEL_FILE  (:${PORT})"
exec llama-server \
  -m "$MODEL_FILE" \
  --mmproj "$MMPROJ_FILE" \
  --host "$HOST" --port "$PORT" \
  -c "$CTX" \
  --n-gpu-layers 99 \
  --image-min-tokens 1024 \
  --jinja \
  --no-warmup
