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

MOCK_MODE = "--mock" in sys.argv
args = [a for a in sys.argv[1:] if a != "--mock"]
MODE = args[0] if args else "low"
MODES = {"low": (5, 30), "medium": (20, 60), "high": (100, 60)}
USERS, DURATION = MODES.get(MODE, (5, 30))

LOCUSTFILE = str(SCRIPT_DIR / "locustfile.py")
MOCK_PORT = int(os.environ.get("MOCK_PORT", "8765"))


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
    if os.environ.get("BENCH_COLD_CACHE") == "1":
        env["BENCH_COLD_CACHE"] = "1"

    log_path = "/tmp/proxy_bench.log"
    proc = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-m",
            "uvicorn",
            "smol_llm_proxy.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(PROXY_PORT),
            "--workers",
            "4",
        ],
        env=env,
        cwd=str(PROJECT_DIR),
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )
    return proc


def start_mock_server():
    env = os.environ.copy()
    env["MOCK_PORT"] = str(MOCK_PORT)
    log_path = "/tmp/mock_bench.log"
    proc = subprocess.Popen(
        [str(VENV_PYTHON), str(SCRIPT_DIR / "mock_server.py")],
        env=env,
        cwd=str(SCRIPT_DIR),
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )
    return proc


def wait_for_mock(timeout=15):
    for i in range(timeout):
        try:
            r = subprocess.run(
                ["curl", "-s", f"http://localhost:{MOCK_PORT}/v1/models"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0 and "mock" in r.stdout:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def setup_mock_backend(admin_key: str):
    """Configure proxy to route 'mock' model to mock server via admin API."""
    import urllib.request

    mock_url = f"http://localhost:{MOCK_PORT}"
    auth_header = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}

    # Create server pointing to mock
    req = urllib.request.Request(
        f"http://localhost:{PROXY_PORT}/admin/servers",
        data=json.dumps({"name": "mock-server", "url": mock_url}),
        headers=auth_header,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        server_id = json.loads(resp.read())["id"]

    # Assign 'mock' model to this server
    req = urllib.request.Request(
        f"http://localhost:{PROXY_PORT}/admin/servers/{server_id}/models",
        data=json.dumps({"model_name": "mock"}),
        headers=auth_header,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()

    print(f"Mock backend configured: mock -> {mock_url}")


def run_both_direct_mock(user_key: str):
    """Run both benchmarks against mock server simultaneously."""
    import threading

    def run_and_capture(cmd, env, label):
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        print(f"[{label}] exit={r.returncode}")
        if r.stdout:
            for line in r.stdout.splitlines()[-20:]:
                print(f"  {line}")

    mock_url = f"http://localhost:{MOCK_PORT}"
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
    env1 = {**os.environ, "BENCH_MODEL": "mock", "DIRECT_URL": mock_url}

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
    env2 = {
        **os.environ,
        "BENCH_MODEL": "mock",
        "PROXY_URL": f"http://localhost:{PROXY_PORT}",
        "USER_KEY": user_key,
        "BENCH_COLD_CACHE": "1",
    }

    t1 = threading.Thread(target=run_and_capture, args=(cmd1, env1, "DIRECT"))
    t2 = threading.Thread(target=run_and_capture, args=(cmd2, env2, "PROXY"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()


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
    env2 = {
        **os.environ,
        "BENCH_MODEL": bench_model,
        "PROXY_URL": f"http://localhost:{PROXY_PORT}",
        "USER_KEY": key,
        "BENCH_COLD_CACHE": "1",
    }

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
                stats["t99"] = float(parts[18]) if parts[18] and parts[18] != "N/A" else 0.0
            except (ValueError, IndexError):
                continue
    return stats


def main():
    admin_key = read_env("ADMIN_KEY")

    if MOCK_MODE:
        print(f"Mode: {MODE} ({USERS} users, {DURATION}s)")
        print(f"Mock port: {MOCK_PORT}")
        print(f"Proxy port: {PROXY_PORT}")
        print(f"DB: {DB_PATH}")

        os.environ["BENCH_COLD_CACHE"] = "1"

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

        # Clean up stale timing data
        timing_file = "/tmp/bench_proxy_timings.jsonl"
        if os.path.exists(timing_file):
            os.remove(timing_file)

        print("\nStarting mock server...")
        mock_proc = start_mock_server()
        if not wait_for_mock():
            print("Mock server failed to start")
            mock_proc.terminate()
            sys.exit(1)
        print("Mock server started OK")

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

        print("\nCreating benchmark user key...")
        user_key = create_user_key(admin_key)
        print(f"User key: {user_key[:20]}...")

        print("\nConfiguring proxy to route 'mock' model to mock server...")
        setup_mock_backend(admin_key)

        try:
            print(f"\n{'=' * 50}")
            print(f"Running BOTH benchmarks against mock server ({USERS} users each, {DURATION}s)")
            print(f"{'=' * 50}")
            run_both_direct_mock(user_key)
        finally:
            proc.terminate()
            proc.wait()
            mock_proc.terminate()
            mock_proc.wait()
    else:
        direct_url, upstream_key, bench_model = load_config()

        print(f"Mode: {MODE} ({USERS} users, {DURATION}s)")
        print(f"Direct URL: {direct_url}")
        print(f"Proxy port: {PROXY_PORT}")
        print(f"Config: {CONFIG_PATH}")
        print(f"DB: {DB_PATH}")

        os.environ["BENCH_COLD_CACHE"] = "1"

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

        # Clean up stale timing data
        timing_file = "/tmp/bench_proxy_timings.jsonl"
        if os.path.exists(timing_file):
            os.remove(timing_file)

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

        print("\nCreating benchmark user key...")
        user_key = create_user_key(admin_key)
        print(f"User key: {user_key[:20]}...")

        try:
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
    overhead_p99 = proxy_stats["t99"] - direct_stats["t99"]
    overhead_mean = proxy_stats["avg"] - direct_stats["avg"]
    overhead_rps = proxy_stats["rps"] - direct_stats["rps"]

    print(f"\n{'Metric':<15} {'Direct':>12} {'Proxy':>14} {'Overhead':>10}")
    print(f"{'-' * 15} {'-' * 12} {'-' * 14} {'-' * 10}")
    print(f"{'P50 latency':<15} {ms(direct_stats['t50']):>12} {ms(proxy_stats['t50']):>14} {overhead_p50:>+9.0f}ms")
    print(f"{'P95 latency':<15} {ms(direct_stats['t95']):>12} {ms(proxy_stats['t95']):>14} {overhead_p95:>+9.0f}ms")
    print(f"{'P99 latency':<15} {ms(direct_stats['t99']):>12} {ms(proxy_stats['t99']):>14} {overhead_p99:>+9.0f}ms")
    print(f"{'Mean latency':<15} {ms(direct_stats['avg']):>12} {ms(proxy_stats['avg']):>14} {overhead_mean:>+9.0f}ms")
    print(f"{'RPS':<15} {rps(direct_stats['rps']):>12} {rps(proxy_stats['rps']):>14} {overhead_rps:>+9.1f}")

    direct_fail_pct = (direct_stats["fail_count"] / max(direct_stats["req_count"], 1)) * 100
    proxy_fail_pct = (proxy_stats["fail_count"] / max(proxy_stats["req_count"], 1)) * 100
    print(f"\nDirect failures: {direct_stats['fail_count']}/{direct_stats['req_count']} ({direct_fail_pct:.2f}%)")
    print(f"Proxy  failures: {proxy_stats['fail_count']}/{proxy_stats['req_count']} ({proxy_fail_pct:.2f}%)")

    if proxy_fail_pct > 1.0:
        print("\nWARNING: Proxy has high failure rate!")
        sys.exit(1)

    # Parse timing breakdown from headers (JSONL)
    timing_file = "/tmp/bench_proxy_timings.jsonl"
    timings = []
    try:
        if os.path.exists(timing_file):
            with open(timing_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        timings.append(json.loads(line))
    except Exception:
        pass

    if timings:
        print(f"\n{'=' * 50}")
        print("Full timing breakdown (proxy overhead per-request)")
        cache_mode = "COLD" if os.environ.get("BENCH_COLD_CACHE") == "1" else "WARM"
        print(f"Cache mode: {cache_mode} | Collected {len(timings)} request timings\n")

        def p(val, pct):
            sorted_vals = sorted(val)
            idx = int(len(sorted_vals) * pct / 100)
            return f"{sorted_vals[min(idx, len(sorted_vals) - 1)]:.2f}ms"

        keys = [
            ("x-proxy-body-read", "Body Read"),
            ("x-proxy-json-parse", "JSON Parse"),
            ("x-proxy-auth-time", "Auth (SHA256+DB)"),
            ("x-proxy-route-time", "Route (JOIN query)"),
            ("x-proxy-alias-time", "Alias lookup"),
            ("x-proxy-serialize-time", "Serialize (if alias changed)"),
            ("x-proxy-forward-time", "Forward (upstream)"),
            ("x-proxy-parse-time", "Parse Response"),
            ("x-proxy-pre-forward", "Pre-Forward Total"),
            ("x-proxy-total-overhead", "Total Overhead"),
        ]

        print(f"\n{'Phase':<30} {'P50':>8} {'P95':>8} {'P99':>8} {'Mean':>8}")
        print(f"{'-' * 30} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")

        for key, label in keys:
            vals = [t.get(key, 0) for t in timings if key in t]
            if not vals:
                continue
            print(f"{label:<30} {p(vals, 50):>8} {p(vals, 95):>8} {p(vals, 99):>8} {sum(vals) / len(vals):>7.2f}ms")

        # Show overhead composition %
        auths = [t.get("x-proxy-auth-time", 0) for t in timings if "x-proxy-auth-time" in t]
        routes = [t.get("x-proxy-route-time", 0) for t in timings if "x-proxy-route-time" in t]
        aliases = [t.get("x-proxy-alias-time", 0) for t in timings if "x-proxy-alias-time" in t]
        body_reads = [t.get("x-proxy-body-read", 0) for t in timings if "x-proxy-body-read" in t]
        json_parses = [t.get("x-proxy-json-parse", 0) for t in timings if "x-proxy-json-parse" in t]
        serialize = [t.get("x-proxy-serialize-time", 0) for t in timings if "x-proxy-serialize-time" in t]
        parses = [t.get("x-proxy-parse-time", 0) for t in timings if "x-proxy-parse-time" in t]
        overheads = [t.get("x-proxy-total-overhead", 0) for t in timings if "x-proxy-total-overhead" in t]

        if overheads:
            avg_overhead = sum(overheads) / len(overheads)
            print(f"\n{'Component':<30} {'Avg (ms)':>10} {'% of overhead':>14}")
            print(f"{'-' * 30} {'-' * 10} {'-' * 14}")
            for label, vals in [
                ("Body Read", body_reads),
                ("JSON Parse", json_parses),
                ("Auth (SHA256+DB)", auths),
                ("Route (JOIN)", routes),
                ("Alias lookup", aliases),
                ("Serialize", serialize),
                ("Parse Response", parses),
            ]:
                avg = sum(vals) / len(vals) if vals else 0
                pct = (avg / avg_overhead) * 100 if avg_overhead > 0 else 0
                print(f"{label:<30} {avg:>10.2f} {pct:>13.1f}%")


if __name__ == "__main__":
    main()
