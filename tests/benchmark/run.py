#!/usr/bin/env python3
"""Benchmark proxy overhead vs direct llama-server."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

json = __import__("orjson")

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"

PROXY_PORT = int(os.environ.get("PROXY_PORT", 7077))
DB_PATH = os.environ.get("DB_PATH", "/tmp/bench_proxy.db")
CONFIG_PATH = str(SCRIPT_DIR / "config.yaml")

MODE = sys.argv[1] if len(sys.argv) > 1 else "low"
MODES = {"low": (5, 30), "medium": (20, 60), "high": (100, 60)}
USERS, DURATION = MODES.get(MODE, (5, 30))

LOCUSTFILE = str(SCRIPT_DIR / "locustfile.py")


def read_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip('"').strip("'")
    if not val:
        print(f"ERROR: {key} is not set")
        sys.exit(1)
    return val


def load_config():
    import yaml

    with open(CONFIG_PATH) as f:
        data = yaml.safe_load(f)
    srv = data["servers"][0]
    return srv["url"], srv.get("api_key", ""), srv["models"][0]


def wait_for_proxy(timeout=30):
    for i in range(timeout):
        try:
            r = subprocess.run(
                ["curl", "-s", f"http://localhost:{PROXY_PORT}/health"], capture_output=True, text=True, timeout=2
            )
            if r.returncode == 0 and "ok" in r.stdout:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def start_proxy(admin_key: str):
    env = os.environ.copy()
    env["ADMIN_KEY"] = admin_key
    env["PROXY_PORT"] = str(PROXY_PORT)
    env["DB_PATH"] = DB_PATH
    env["CONFIG_PATH"] = CONFIG_PATH
    env["PYTHONPATH"] = str(PROJECT_DIR)

    log_path = "/tmp/proxy_bench.log"
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(PROXY_PORT)],
        env=env,
        cwd=str(PROJECT_DIR),
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )
    return proc


def create_user_key(admin_key: str) -> str:
    import urllib.request

    url = f"http://localhost:{PROXY_PORT}/admin/keys"
    req = urllib.request.Request(
        url,
        data=json.dumps({"name": "bench_user", "active": True}),
        headers={"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["key"]


def run_locust(key: str, target: str, is_proxy: bool, direct_url: str, upstream_key: str, bench_model: str):
    env = os.environ.copy()
    env["BENCH_MODEL"] = bench_model

    if is_proxy:
        env["PROXY_URL"] = f"http://localhost:{PROXY_PORT}"
        env["USER_KEY"] = key
    else:
        env["DIRECT_URL"] = direct_url
        env["UPSTREAM_KEY"] = upstream_key

    locust_file = LOCUSTFILE if is_proxy else str(SCRIPT_DIR / "locust_direct.py")

    cmd = [
        str(VENV_PYTHON),
        "-m",
        "locust",
        "-f",
        locust_file,
        "--headless",
        "-u",
        str(USERS),
        "-r",
        "10",
        "-t",
        f"{DURATION}s",
        "--csv",
        f"/tmp/bench_{target}",
    ]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    return result


def run_both_parallel(key: str, direct_url: str, upstream_key: str, bench_model: str):
    """Run both benchmarks simultaneously on the same backend for fair comparison."""
    import threading

    def run_and_capture(cmd, env, label):
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        print(f"[{label}] exit={r.returncode}")
        if r.stdout:
            for line in r.stdout.splitlines()[-20:]:
                print(f"  {line}")

    cmd1 = [
        str(VENV_PYTHON),
        "-m",
        "locust",
        "-f",
        str(SCRIPT_DIR / "locust_direct.py"),
        "--headless",
        "-u",
        str(USERS),
        "-r",
        "10",
        "-t",
        f"{DURATION}s",
        "--csv",
        "/tmp/bench_direct",
    ]
    env1 = {**os.environ, "BENCH_MODEL": bench_model, "DIRECT_URL": direct_url, "UPSTREAM_KEY": upstream_key}

    cmd2 = [
        str(VENV_PYTHON),
        "-m",
        "locust",
        "-f",
        str(SCRIPT_DIR / "locust_proxy.py"),
        "--headless",
        "-u",
        str(USERS),
        "-r",
        "10",
        "-t",
        f"{DURATION}s",
        "--csv",
        "/tmp/bench_proxy",
    ]
    env2 = {**os.environ, "BENCH_MODEL": bench_model, "PROXY_URL": f"http://localhost:{PROXY_PORT}", "USER_KEY": key}

    t1 = threading.Thread(target=run_and_capture, args=(cmd1, env1, "DIRECT"))
    t2 = threading.Thread(target=run_and_capture, args=(cmd2, env2, "PROXY"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()


def warmup(direct_url: str, upstream_key: str, bench_model: str):
    """Send a few requests to warm up the backend."""
    import urllib.request

    for _ in range(3):
        try:
            data = json.dumps({"model": bench_model, "messages": [{"role": "user", "content": "hi"}]}).encode()
            req = urllib.request.Request(
                f"{direct_url}/v1/chat/completions",
                data=data,
                headers={"Authorization": f"Bearer {upstream_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception:
            pass
    time.sleep(2)


def parse_result(target: str):
    csv_path = f"/tmp/bench_{target}_stats.csv"
    if not os.path.exists(csv_path):
        return None

    stats = {}
    with open(csv_path) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 20:
                continue
            name = parts[1]
            if "v1/chat/completions" not in name and name != "Aggregated":
                continue

            try:
                stats["req_count"] = int(parts[2]) or 0
                stats["fail_count"] = int(parts[3]) or 0
                stats["avg"] = float(parts[5]) if parts[5] else 0.0
                stats["rps"] = float(parts[9]) if parts[9] else 0.0
                stats["t50"] = float(parts[11]) if parts[11] and parts[11] != "N/A" else 0.0
                stats["t95"] = float(parts[16]) if parts[16] and parts[16] != "N/A" else 0.0
            except (ValueError, IndexError):
                continue
    return stats


def main():
    admin_key = read_env("ADMIN_KEY")
    direct_url, upstream_key, bench_model = load_config()

    print(f"Mode: {MODE} ({USERS} users, {DURATION}s)")
    print(f"Direct URL: {direct_url}")
    print(f"Proxy port: {PROXY_PORT}")
    print(f"Config: {CONFIG_PATH}")
    print(f"DB: {DB_PATH}")

    # Clean up old db and kill old proxy
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    for pid_line in subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout.splitlines():
        if f"--port {PROXY_PORT}" in pid_line and "grep" not in pid_line:
            pid = int(pid_line.split()[1])
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
    time.sleep(1)

    # Start proxy
    print("\nStarting proxy...")
    proc = start_proxy(admin_key)
    if not wait_for_proxy():
        log_text = ""
        try:
            with open("/tmp/proxy_bench.log") as f:
                log_text = f.read()[-2000:]
        except Exception:
            pass
        print(f"Proxy failed to start. Last log:\n{log_text}")
        proc.terminate()
        sys.exit(1)
    print("Proxy started OK")

    # Create user key
    print("\nCreating benchmark user key...")
    user_key = create_user_key(admin_key)
    print(f"User key: {user_key[:20]}...")

    try:
        # Run both benchmarks SIMULTANEOUSLY for fair comparison
        print(f"\n{'=' * 50}")
        print(f"Running BOTH benchmarks simultaneously ({USERS} users each, {DURATION}s)")
        print(f"{'=' * 50}")
        run_both_parallel(user_key, direct_url, upstream_key, bench_model)

    finally:
        proc.terminate()
        proc.wait()

    # Parse and compare results
    direct_stats = parse_result("direct")
    proxy_stats = parse_result("proxy")

    print(f"\n{'=' * 50}")
    print("Results comparison")
    print(f"{'=' * 50}")

    if not direct_stats or not proxy_stats:
        print("Could not parse results from CSV files")
        sys.exit(1)

    def ms(val):
        return f"{val:.0f}ms"

    def rps(val):
        return f"{val:.1f}"

    overhead_p50 = proxy_stats["t50"] - direct_stats["t50"]
    overhead_p95 = proxy_stats["t95"] - direct_stats["t95"]
    overhead_mean = proxy_stats["avg"] - direct_stats["avg"]
    overhead_rps = proxy_stats["rps"] - direct_stats["rps"]

    print(f"\n{'Metric':<15} {'Direct':>12} {'Proxy':>14} {'Overhead':>10}")
    print(f"{'-' * 15} {'-' * 12} {'-' * 14} {'-' * 10}")
    print(f"{'P50 latency':<15} {ms(direct_stats['t50']):>12} {ms(proxy_stats['t50']):>14} {overhead_p50:>+9.0f}ms")
    print(f"{'P95 latency':<15} {ms(direct_stats['t95']):>12} {ms(proxy_stats['t95']):>14} {overhead_p95:>+9.0f}ms")
    print(f"{'Mean latency':<15} {ms(direct_stats['avg']):>12} {ms(proxy_stats['avg']):>14} {overhead_mean:>+9.0f}ms")
    print(f"{'RPS':<15} {rps(direct_stats['rps']):>12} {rps(proxy_stats['rps']):>14} {overhead_rps:>+9.1f}")

    direct_fail_pct = (direct_stats["fail_count"] / max(direct_stats["req_count"], 1)) * 100
    proxy_fail_pct = (proxy_stats["fail_count"] / max(proxy_stats["req_count"], 1)) * 100
    print(f"\nDirect failures: {direct_stats['fail_count']}/{direct_stats['req_count']} ({direct_fail_pct:.2f}%)")
    print(f"Proxy  failures: {proxy_stats['fail_count']}/{proxy_stats['req_count']} ({proxy_fail_pct:.2f}%)")

    if proxy_fail_pct > 1.0:
        print("\nWARNING: Proxy has high failure rate!")
        sys.exit(1)


if __name__ == "__main__":
    main()
