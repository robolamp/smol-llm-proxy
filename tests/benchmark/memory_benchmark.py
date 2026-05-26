#!/usr/bin/env python3
"""Measure proxy worker memory usage under load."""

import argparse
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


def wait_for_proxy(timeout=30):
    for i in range(timeout):
        try:
            r = subprocess.run(
                ["curl", "-s", f"http://localhost:{PROXY_PORT}/health"],
                capture_output=True, text=True, timeout=2
            )
            if r.returncode == 0 and "ok" in r.stdout:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def get_worker_pids(port):
    """Find uvicorn worker PIDs by matching processes listening on the port."""
    out = subprocess.run(
        ["ss", "-tlnp"], capture_output=True, text=True
    ).stdout
    pids = set()
    for line in out.splitlines():
        if f":{port}" in line:
            # Extract PIDs from users:(("python",pid=XXX,...))
            import re
            for m in re.finditer(r"pid=(\d+)", line):
                pids.add(int(m.group(1)))
    return sorted(pids)


def get_rss(pid):
    """Read VmRSS from /proc/<pid>/status. Returns KB or None."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return None


def get_all_proxy_pids(port, master_pid=None):
    """Get master + worker PIDs. Workers are children of the master."""
    pids = set()
    
    # Find master process
    out = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    ).stdout
    master_found = None
    for line in out.splitlines():
        if f"--port {port}" in line and "grep" not in line:
            parts = line.split()
            if len(parts) > 1:
                try:
                    pid = int(parts[1])
                    pids.add(pid)
                    master_found = pid
                except ValueError:
                    pass
    
    # If we have a master PID, find its children (uvicorn workers)
    if master_found:
        child_out = subprocess.run(
            ["ps", "--ppid", str(master_found), "-o", "pid="],
            capture_output=True, text=True
        ).stdout
        for line in child_out.splitlines():
            line = line.strip()
            if line:
                try:
                    pids.add(int(line))
                except ValueError:
                    pass
    
    return sorted(pids)


def send_request(url, key, model="mock", timeout=30):
    """Send a single chat completion request via urllib."""
    import urllib.request
    data = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    })
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception:
        return False


def send_streaming_request(url, key, model="mock"):
    """Send a streaming chat completion request."""
    import urllib.request
    data = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    })
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            while True:
                chunk = resp.read(64)
                if not chunk:
                    break
        return True
    except Exception:
        return False


def setup_mock_backend(admin_key):
    """Configure proxy to route 'mock' model to mock server."""
    import urllib.request
    mock_url = f"http://localhost:{MOCK_PORT}"
    auth_header = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}

    req = urllib.request.Request(
        f"http://localhost:{PROXY_PORT}/admin/servers",
        data=json.dumps({"name": "mock-server", "url": mock_url}),
        headers=auth_header,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        server_id = json.loads(resp.read())["id"]

    req = urllib.request.Request(
        f"http://localhost:{PROXY_PORT}/admin/servers/{server_id}/models",
        data=json.dumps({"model_name": "mock"}),
        headers=auth_header,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def setup_real_backend(admin_key, backend_url, api_key, model_name):
    """Configure proxy to route a model to the real backend."""
    import urllib.request
    auth_header = {"Authorization": f"Bearer {admin_key}", "Content-Type": "application/json"}

    req = urllib.request.Request(
        f"http://localhost:{PROXY_PORT}/admin/servers",
        data=json.dumps({"name": "real-server", "url": backend_url, "api_key": api_key}),
        headers=auth_header,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        server_id = json.loads(resp.read())["id"]

    req = urllib.request.Request(
        f"http://localhost:{PROXY_PORT}/admin/servers/{server_id}/models",
        data=json.dumps({"model_name": model_name}),
        headers=auth_header,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()

    print(f"Real backend configured: {model_name} -> {backend_url}")


def create_user_key(admin_key):
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


def start_proxy(admin_key, workers):
    env = os.environ.copy()
    env["ADMIN_KEY"] = admin_key
    env["PROXY_PORT"] = str(PROXY_PORT)
    env["DB_PATH"] = DB_PATH
    env["CONFIG_PATH"] = CONFIG_PATH
    env["PYTHONPATH"] = str(PROJECT_DIR)

    log_path = "/tmp/proxy_memory_bench.log"
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "uvicorn", "smol_llm_proxy.main:app",
         "--host", "0.0.0.0", "--port", str(PROXY_PORT),
         "--workers", str(workers)],
        env=env,
        cwd=str(PROJECT_DIR),
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )
    return proc


def start_mock_server():
    env = os.environ.copy()
    env["MOCK_PORT"] = str(MOCK_PORT)
    log_path = "/tmp/mock_memory_bench.log"
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
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode == 0 and "mock" in r.stdout:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def kb(val):
    return f"{val / 1024:.1f}MB"


def main():
    parser = argparse.ArgumentParser(description="Proxy memory benchmark")
    parser.add_argument("workers", type=int, choices=[1, 4], default=4, nargs="?",
                        help="Number of uvicorn workers (1 or 4)")
    parser.add_argument("--mock", action="store_true", help="Use mock server instead of real backend")
    args = parser.parse_args()

    admin_key = read_env("ADMIN_KEY")
    num_workers = args.workers
    use_mock = args.mock

    print(f"Proxy workers: {num_workers}")
    print(f"Backend: {'mock' if use_mock else 'real'}")
    print(f"Proxy port: {PROXY_PORT}")
    print(f"DB: {DB_PATH}\n")

    # Cleanup old processes
    for pid_line in subprocess.run(["ps", "aux"], capture_output=True, text=True).stdout.splitlines():
        if f"--port {PROXY_PORT}" in pid_line and "grep" not in pid_line:
            parts = pid_line.split()
            if len(parts) > 1:
                try:
                    os.kill(int(parts[1]), signal.SIGKILL)
                except Exception:
                    pass
        if f"--port {MOCK_PORT}" in pid_line and "grep" not in pid_line:
            parts = pid_line.split()
            if len(parts) > 1:
                try:
                    os.kill(int(parts[1]), signal.SIGKILL)
                except Exception:
                    pass
    time.sleep(1)

    # Clean DB
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    # Start mock server if needed
    mock_proc = None
    if use_mock:
        print("Starting mock server...")
        mock_proc = start_mock_server()
        if not wait_for_mock():
            print("Mock server failed to start")
            mock_proc.terminate()
            sys.exit(1)
        print("Mock server started OK\n")

    # Start proxy
    print(f"Starting proxy with {num_workers} worker(s)...")
    proc = start_proxy(admin_key, num_workers)
    if not wait_for_proxy():
        log_text = ""
        try:
            with open("/tmp/proxy_memory_bench.log") as f:
                log_text = f.read()[-2000:]
        except Exception:
            pass
        print(f"Proxy failed to start. Last log:\n{log_text}")
        proc.terminate()
        sys.exit(1)
    print("Proxy started OK\n")

    # Create user key and configure mock backend
    user_key = create_user_key(admin_key)
    bench_model = "mock"
    if use_mock:
        setup_mock_backend(admin_key)
    else:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        srv = cfg["servers"][0]
        setup_real_backend(admin_key, srv["url"], srv.get("api_key", ""), srv["models"][0])
        bench_model = srv["models"][0]

    proxy_url = f"http://localhost:{PROXY_PORT}/v1/chat/completions"

    # Wait for workers to fully start
    time.sleep(2)

    # Get initial worker PIDs and RSS baselines
    print("Collecting baseline memory...")
    pids = get_all_proxy_pids(PROXY_PORT)
    if not pids:
        print("ERROR: No proxy processes found!")
        sys.exit(1)

    master_pid = pids[0]
    baseline = {}
    for pid in pids:
        rss = get_rss(pid)
        if rss is not None:
            baseline[pid] = rss

    # Print initial state
    print(f"\nFound {len(pids)} process(es): {pids} (master={master_pid})")
    print(f"{'=' * 60}")
    print(f"Baseline RSS:")
    for pid in pids:
        rss = baseline.get(pid, 0)
        role = "master" if len(pids) == 1 or pid == pids[0] and num_workers == 1 else f"worker-{pid}"
        print(f"  PID {pid:>6}: {kb(rss)}")
    print(f"{'=' * 60}\n")

    # Phase 1: Idle (5 seconds)
    print("[Phase 1] Idle (5s)...")
    time.sleep(5)
    samples_idle = measure_pids(master_pid, pids, baseline)
    print_samples("Idle", samples_idle)

    # Phase 2: Warmup (30 requests)
    print("\n[Phase 2] Warmup (30 non-streaming requests)...")
    for i in range(30):
        send_request(proxy_url, user_key, bench_model)
    time.sleep(1)
    samples_warmup = measure_pids(master_pid, pids, baseline)
    print_samples("Warmup", samples_warmup)

    # Phase 3: Load (60 seconds, 100 concurrent streaming requests)
    print("\n[Phase 3] Load (60s, 100 concurrent streaming requests)...")
    import threading

    stop_flag = threading.Event()
    success_count = [0]
    fail_count = [0]

    def load_worker():
        while not stop_flag.is_set():
            if send_streaming_request(proxy_url, user_key, bench_model):
                success_count[0] += 1
            else:
                fail_count[0] += 1

    threads = []
    for _ in range(100):
        t = threading.Thread(target=load_worker, daemon=True)
        t.start()
        threads.append(t)

    # Measure every 2 seconds during load
    samples_load = {}
    start_time = time.time()
    while time.time() - start_time < 60:
        elapsed = int(time.time() - start_time)
        sample = measure_pids(master_pid, pids, baseline)
        samples_load[elapsed] = sample
        if elapsed % 10 == 0:
            total = sum(sample.values())
            print(f"  t={elapsed}s: {len(sample)} processes, total={kb(total)}")
        time.sleep(2)

    stop_flag.set()
    for t in threads:
        t.join(timeout=5)

    total_requests = success_count[0] + fail_count[0]
    # Get final sample
    final_sample = measure_pids(master_pid, pids, baseline)
    if not samples_load:
        samples_load[60] = final_sample
    print(f"\n  Load complete: {total_requests} requests ({success_count[0]} ok, {fail_count[0]} fail)")
    print_samples("Load (final)", final_sample)

    # Summary table
    print(f"\n{'=' * 70}")
    print(f"MEMORY SUMMARY")
    print(f"{'=' * 70}")
    print(f"{'Process':<12} {'Baseline':>10} {'Idle':>10} {'Warmup':>10} {'Load':>10}")
    print(f"{'-' * 12} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 10}")

    # Find the latest load sample (closest to 60s)
    latest_load_pid = None
    latest_load_time = -1
    for t, sample in samples_load.items():
        if t > latest_load_time and sample:
            latest_load_time = t
            latest_load_sample = sample

    for pid in pids:
        role = f"PID {pid}"
        b = baseline.get(pid, 0)
        idle_rss = samples_idle.get(pid, b)
        warm_rss = samples_warmup.get(pid, b)
        load_rss = latest_load_sample.get(pid, b)

        print(f"{role:<12} {kb(b):>10} {kb(idle_rss):>10} {kb(warm_rss):>10} {kb(load_rss):>10}")

    total_baseline = sum(baseline.values())
    total_load = sum(latest_load_sample.get(pid, baseline.get(pid, 0)) for pid in pids)

    growth_pct = ((total_load - total_baseline) / max(total_baseline, 1)) * 100
    print(f"\n{'Total':<12} {kb(total_baseline):>10} {'':>10} {'':>10} {kb(total_load):>10}")
    print(f"{'Growth':<12} {'':>10} {'':>10} {'':>10} +{growth_pct:.1f}%")
    print(f"{'=' * 70}")

    # Cleanup
    proc.terminate()
    proc.wait()
    if mock_proc:
        mock_proc.terminate()
        mock_proc.wait()


def measure_pids(master_pid, known_pids, baseline):
    """Measure RSS for all pids. Returns {pid: rss_kb}."""
    result = {}
    
    # Try known pids first (including children of master)
    for pid in known_pids:
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        result[pid] = int(line.split()[1])
                        break
        except Exception:
            pass
    
    # Also discover fresh children of master (workers may have restarted)
    try:
        child_out = subprocess.run(
            ["ps", "--ppid", str(master_pid), "-o", "pid="],
            capture_output=True, text=True
        ).stdout
        for line in child_out.splitlines():
            line = line.strip()
            if line:
                try:
                    new_pid = int(line)
                    if new_pid not in result:
                        with open(f"/proc/{new_pid}/status") as f:
                            for line2 in f:
                                if line2.startswith("VmRSS:"):
                                    result[new_pid] = int(line2.split()[1])
                                    break
                except ValueError:
                    pass
    except Exception:
        pass
    
    # Include master itself
    try:
        with open(f"/proc/{master_pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    result[master_pid] = int(line.split()[1])
                    break
    except Exception:
        pass
    
    return result


def print_samples(label, samples):
    """Print a sample measurement row."""
    total = sum(samples.values())
    count = len(samples)
    print(f"  {label}: {count} processes, total={kb(total)}")


if __name__ == "__main__":
    main()
