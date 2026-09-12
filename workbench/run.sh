#!/usr/bin/env bash
set -euo pipefail
cd /root/YuE/workbench
export PYTHONPATH="/root/YuE/workbench/vendor:${PYTHONPATH:-}"
export HF_HOME=/root/autodl-tmp/huggingface
export HF_HUB_OFFLINE=1
export STUDIO_DATA=/root/autodl-tmp/yue2-studio
export OMP_NUM_THREADS=8
export STUDIO_PRIVATE_USER=creator
exec /root/YuE/.venv/bin/python -m uvicorn server:app --host 0.0.0.0 --port "${STUDIO_PORT:-6006}" --workers 1 --no-access-log --no-server-header
