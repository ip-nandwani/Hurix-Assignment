#!/bin/bash
# Verifier entry point: bring up the gateway, run the tests, write a 0/1 reward.

# run from the workspace root (WORKDIR is /app in the container)
if [ ! -f package.json ] && [ -d /app ]; then
    cd /app || exit 1
fi
if [ "$PWD" = "/" ]; then
    echo "Error: no working directory set (need a WORKDIR)."
    exit 1
fi

mkdir -p /logs/verifier

# clean state so grading only reflects this run
rm -f releases.duckdb releases.duckdb.wal
rm -f distribution-gateway/data/gateway.json

# start the gateway in the background and wait for it
node distribution-gateway/server.js > /logs/verifier/gateway.log 2>&1 &
GW_PID=$!

python3 - <<'PY'
import time, urllib.request
for _ in range(60):
    try:
        if urllib.request.urlopen("http://127.0.0.1:7070/healthz", timeout=1).status == 200:
            print("gateway ready"); break
    except Exception:
        time.sleep(0.5)
else:
    print("gateway NOT ready")
PY

python3 -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
code=$?

kill "$GW_PID" 2>/dev/null
wait "$GW_PID" 2>/dev/null

echo "pytest exit code: ${code}"
if [ "$code" -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
