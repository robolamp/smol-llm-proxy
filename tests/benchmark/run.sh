#!/bin/bash
# Benchmark proxy overhead vs direct llama-server.
# Usage: ./run.sh [low|medium|high]

set -eo pipefail

MODE=${1:-medium}
case $MODE in
  low)    USERS=5;     DURATION=30 ;;
  medium) USERS=20;    DURATION=60 ;;
  high)   USERS=100;   DURATION=60 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LOCUSTFILE="$SCRIPT_DIR/locustfile.py"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

# Benchmark-specific settings
export PROXY_PORT=7077
export DB_PATH=/tmp/bench_proxy.db
export CONFIG_PATH="${SCRIPT_DIR}/config.yaml"

# Load ADMIN_KEY from project .env
if [ -f "$PROJECT_DIR/.env" ]; then
    ADMIN_KEY=$(grep '^ADMIN_KEY=' "$PROJECT_DIR/.env" | cut -d= -f2-)
fi
ADMIN_KEY=${ADMIN_KEY:?ADMIN_KEY is not set}

# Load first server from benchmark config.yaml for direct benchmark
BENCH_CONFIG="$SCRIPT_DIR/config.yaml"
if [ -f "$BENCH_CONFIG" ]; then
    eval "$("$VENV_PYTHON" -c "
import yaml, sys
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f)
srv = data['servers'][0]
print(f'DIRECT_URL={repr(srv[\"url\"])}')
print(f'UPSTREAM_KEY={repr(srv.get(\"api_key\", \"\"))}')
print(f'BENCH_MODEL={repr(srv[\"models\"][0])}')
" "$BENCH_CONFIG")"
fi

DIRECT_URL=${DIRECT_URL:-http://host:port}
UPSTREAM_KEY=${UPSTREAM_KEY:-}

export DIRECT_URL UPSTREAM_KEY

# Kill any existing process on the proxy port
stop_proxy() {
    local pids=$(ps aux | grep "uvicorn main:app.*--port $PROXY_PORT" | grep -v grep | awk '{print $2}')
    if [ -n "$pids" ]; then
        echo "Killing existing processes on port $PROXY_PORT: $pids"
        kill -9 $pids 2>/dev/null || true
        sleep 1
    fi
}

# Auto-start proxy
stop_proxy
echo "Starting proxy on port $PROXY_PORT..."
export PYTHONPATH="." ADMIN_KEY
"$VENV_PYTHON" -m uvicorn main:app --host 0.0.0.0 --port "$PROXY_PORT" &>/tmp/proxy_bench.log &
PROXY_PID=$!
for i in $(seq 1 15); do
    if curl -s "http://localhost:$PROXY_PORT/health" &>/dev/null; then
        echo "Proxy started (PID $PROXY_PID)"
        break
    fi
    sleep 1
done

# Create user key for benchmarking
TIMESTAMP=$(date +%s)
USER_KEY=$(curl -s "http://localhost:$PROXY_PORT/admin/keys" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"bench_user_$TIMESTAMP\", \"active\": true}" \
    | "$VENV_PYTHON" -c "import sys, json; print(json.load(sys.stdin)['key'])")

export PROXY_URL="http://localhost:$PROXY_PORT"
export USER_KEY

cleanup() {
    echo ""
    echo "Cleaning up..."
    local pids=$(ps aux | grep "uvicorn main:app.*--port $PROXY_PORT" | grep -v grep | awk '{print $2}')
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
    fi
    rm -f "$DB_PATH" 2>/dev/null || true
}
trap cleanup EXIT

run_bench() {
    local target=$1
    "$VENV_PYTHON" -m locust -f "$LOCUSTFILE" --headless \
        -u "$USERS" -r 10 -t "${DURATION}s" \
        --csv "/tmp/bench_${target}" \
        2>&1 | tee "/tmp/bench_${target}.log"
}

echo "=============================================="
echo " Benchmark: $MODE ($USERS users, ${DURATION}s)"
echo "=============================================="

export BENCH_PROXY=0
run_bench direct

echo ""
export BENCH_DIRECT=0
export BENCH_PROXY=1
run_bench proxy

echo ""
echo "=============================================="
echo " Results comparison"
echo "=============================================="

# Parse CSV values using process substitution (avoids subshell)
direct_p50=""; direct_p95=""; direct_mean=""; direct_rps=""
proxy_p50=""; proxy_p95=""; proxy_mean=""; proxy_rps=""

for target in direct proxy; do
    csv="/tmp/bench_${target}_stats.csv"
    if [ ! -f "$csv" ]; then
        echo "No data for $target — check logs above"
        continue
    fi
    
    while IFS=',' read -r type name req_count fail_count median avg min max content_size rps fps t50 t67 t75 t80 t90 t95 t98 t99 t999 t9999 t100; do
        if [ "$target" = "direct" ]; then
            direct_p50=$t50; direct_p95=$t95; direct_mean=$avg; direct_rps=$rps
        else
            proxy_p50=$t50; proxy_p95=$t95; proxy_mean=$avg; proxy_rps=$rps
        fi
    done < <(grep "v1/chat/completions" "$csv" | tail -1)
done

echo ""
printf "%-15s %-12s %-14s %s\n" "Metric" "Direct" "Through proxy" "Overhead"
printf "%-15s %-12s %-14s %s\n" "------" "------" "-------------" "--------"

fmt_ms() { printf '%.0fms' "$1"; }
fmt_rps() { printf '%.1f' "$1"; }

d_p50=${direct_p50:-0}; d_p95=${direct_p95:-0}; d_mean=${direct_mean:-0}; d_rps=${direct_rps:-0}
p_p50=${proxy_p50:-0}; p_p95=${proxy_p95:-0}; p_mean=${proxy_mean:-0}; p_rps=${proxy_rps:-0}

echo "P50 latency    $(fmt_ms $d_p50)  $(fmt_ms $p_p50)      +$(( ${p_p50%.*} - ${d_p50%.*} ))ms"
echo "P95 latency    $(fmt_ms $d_p95)  $(fmt_ms $p_p95)      +$(( ${p_p95%.*} - ${d_p95%.*} ))ms"
echo "Mean latency   $(fmt_ms $d_mean)  $(fmt_ms $p_mean)      $($VENV_PYTHON -c "print(f'{float(${p_mean}) - float(${d_mean}):+4.0f}')")ms"
echo "RPS            $(fmt_rps $d_rps)  $(fmt_rps $p_rps)      $($VENV_PYTHON -c "print(f'{($p_rps - $d_rps):+.1f}')")"
