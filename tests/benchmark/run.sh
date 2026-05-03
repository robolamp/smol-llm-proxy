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
LOCUSTFILE="$SCRIPT_DIR/locustfile.py"

run_bench() {
    local target=$1
    shift
    locust -f "$LOCUSTFILE" --headless \
        -u "$USERS" -r 10 -t "${DURATION}s" \
        --csv "/tmp/bench_${target}" \
        "$@" 2>&1 | tee "/tmp/bench_${target}.log"
}

echo "=============================================="
echo " Benchmark: $MODE ($USERS users, ${DURATION}s)"
echo "=============================================="

run_bench direct BENCH_PROXY=0

echo ""
run_bench proxy BENCH_DIRECT=0

echo ""
echo "=============================================="
echo " Results comparison"
echo "=============================================="

# Parse CSV for latency percentiles
for target in direct proxy; do
    csv="/tmp/bench_${target}_stats_percentiles.csv"
    if [ ! -f "$csv" ]; then
        echo "No data for $target — check logs above"
        continue
    fi
    
    echo ""
    echo "--- $target percentiles ---"
    grep "v1/chat/completions" "$csv" | while IFS=',' read -r label t50 t67 t80 t90 t95 t99 t999 avg median; do
        echo "  P50: ${t50}ms, P95: ${t95}ms, P99: ${t99}ms, mean: ${avg}ms"
    done
    
    # Requests count from requests CSV
    csv2="/tmp/bench_${target}_requests.csv"
    if [ -f "$csv2" ]; then
        tail -1 "$csv2" | IFS=',' read -r label requests fails avg_response min_response max_response avg_rps ttfb avg_ttfb 2>/dev/null || true
        echo "  Requests: $requests, Avg: ${avg_response}ms"
    fi
done
