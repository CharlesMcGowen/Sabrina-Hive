#!/bin/bash
# Operator launcher -- (re)start Sabrina's two GPU workers with the CURRENT code, in one shot.
# All the quoting lives HERE so the command you run is trivial and can't mis-parse.
#
#   ssh ego@192.168.2.69 'bash /home/ego/bot/Sabrina-windows-sync/tools/start_workers.sh'
#
# Worker 1  -> pod s3nty7d3wka0mm (ablation2): general self-improvement busy loop.
#             LIVE-CONTROLLED via data/busy_control.json -> the web panel buttons
#             (http://192.168.2.69:8091) flip it improve<->all / pause WITHOUT relaunch.
# Worker 2  -> pod oc28k8qtvpojnf (paper-migration): scope-gate hardening loop
#             (goal in logs/scopegate_goal.txt).
# Both are POD-STRICT (the pod's 72B or nothing -- no weak-local fallback).
set -u
cd /home/ego/bot/Sabrina-windows-sync || exit 1
PY=/mnt/webapps-nvme/venv_merged/bin/python3

# Stop any previous workers first (single-token patterns -> no quoting pitfalls).
pkill -f run_engagement 2>/dev/null
pkill -f sabrina_gemini_loop 2>/dev/null
sleep 3

# Worker 1: self-improvement on the default pod (ablation2), button-controlled.
setsid bash -c "$PY -u tools/run_engagement.py busy --mode improve --minutes 20 > logs/busy.log 2>&1" </dev/null &

# Worker 2: scope-gate hardening pinned to oc28k8qtvpojnf's 72B.
setsid env SABRINA_RUNPOD_OLLAMA_URLS=https://oc28k8qtvpojnf-11434.proxy.runpod.net \
  SABRINA_FORCE_LOCAL_MODEL=qwen2.5:72b SABRINA_SMART_TIER_STRICT=1 SABRINA_UNATTENDED=1 \
  bash -c "$PY -u sabrina_gemini_loop.py --goal \"\$(cat logs/scopegate_goal.txt)\" --max-seconds 5400 --mode yolo --oracle-preference local > logs/scopegate.log 2>&1" </dev/null &

sleep 1
echo "[start_workers] launched:"
echo "  worker1  self-improvement (ablation2)   -> logs/busy.log      (panel: http://192.168.2.69:8091)"
echo "  worker2  scope-gate hardening (oc28k8..) -> logs/scopegate.log"
