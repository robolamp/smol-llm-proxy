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

# Load env vars from project .env
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Load benchmark-specific env vars
BENCH_ENV="$SCRIPT_DIR/.env.benchmark"
if [ -f "$BENCH_ENV" ]; then
    set -a
    source "$BENCH_ENV"
    set +a
fi

PROXY_PORT=${PROXY_PORT:-7070}
ADMIN_KEY=${ADMIN_KEY:?ADMIN_KEY is not set in .env}
DIRECT_URL=${DIRECT_URL:-http://host:port}
UPSTREAM_KEY=${UPSTREAM_KEY:-}

export DIRECT_URL UPSTREAM_KEY

# Auto-start proxy if not running
if ! curl -s "http://localhost:$PROXY_PORT/" &>/dev/null; then
    echo "Starting proxy on port $PROXY_PORT..."
    "$VENV_PYTHON" -m uvicorn main:app --host 0.0.0.0 --port "$PROXY_PORT" &>/tmp/proxy_bench.log &
    PROXY_PID=$!
    for i in $(seq 1 10); do
        if curl -s "http://localhost:$PROXY_PORT/" &>/dev/null; then
            echo "Proxy started (PID $PROXY_PID)"
            break
        fi
        sleep 1
    done
else
    PROXY_PID=""
fi

# Create user key for benchmarking
USER_KEY=$(curl -s "http://localhost:$PROXY_PORT/admin/keys" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -X POST \
    -H "Content-Type: application/json" \
    -d '{"key": "bench_user_'"$(date +%s)"'", "models": ["Qwen3.5-2B"], "active": true}' \
    | "$VENV_PYTHON" -c "import sys, json; print(json.load(sys.stdin)['key'])")

export PROXY_URL="http://localhost:$PROXY_PORT"
export USER_KEY

cleanup() {
    echo ""
    echo "Cleaning up..."
    if [ -n "$PROXY_PID" ]; then
        kill "$PROXY_PID" 2>/dev/null || true
    fi
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
unset BENCH_PROXY
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
