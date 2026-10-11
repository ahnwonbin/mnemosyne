#!/usr/bin/env bash
set -e
export PATH="$HOME/.bun/bin:$PATH"
export CLAUDE_MEM_DATA_DIR="$HOME/.claude-mem-trial/data"
export CLAUDE_MEM_ENV_FILE="$HOME/.claude-mem-trial/data/.env"
export CLAUDE_MEM_WORKER_PORT=37777
export CLAUDE_MEM_WORKER_HOST=127.0.0.1
export CLAUDE_MEM_CHROMA_ENABLED=false
export CLAUDE_MEM_MAX_CONCURRENT_AGENTS=1

pkill -f "worker-service.cjs" || true
sleep 1

bun "$HOME/.claude-mem-trial/scripts/worker-service.cjs" > /tmp/diag_worker.log 2>&1 &
WPID=$!
echo "Started PID: $WPID"

for i in $(seq 1 40); do
    STATUS_CODE=$(curl -s -o /tmp/readiness.json -w "%{http_code}" "http://127.0.0.1:37777/api/readiness" 2>/dev/null || echo "000")
    BODY=$(cat /tmp/readiness.json 2>/dev/null || echo "")
    echo "Sec $i: HTTP $STATUS_CODE -> $BODY"
    if [ "$STATUS_CODE" -eq 200 ]; then
        echo "SUCCESS: Worker ready at Sec $i!"
        break
    fi
    sleep 1
done

kill -TERM $WPID 2>/dev/null || true
echo "--- LOG TAIL ---"
tail -n 30 /tmp/diag_worker.log
