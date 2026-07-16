#!/usr/bin/env python3
"""Profile rate limiter — before vs after."""

import cProfile
import pstats
import io
import os
import sys
import time as _time

sys.path.insert(0, "/workspace/smol-llm-proxy")
os.environ["ADMIN_KEY"] = "Fdczv9kefrH2BctYxhToOWvEaBREkR7YfOaH3GIwFcE"
os.environ["DB_PATH"] = "/tmp/profile_proxy.db"

from smol_llm_proxy.database import init_db, reset_db_connection
from smol_llm_proxy.auth import create_api_key
from smol_llm_proxy.rate_limiter import reserve_rate, reconcile_rate

init_db()
result = create_api_key("profile-test")
with __import__("sqlite3").connect(os.environ["DB_PATH"]) as conn:
    conn.execute("UPDATE api_keys SET rpm_limit = 1000000, tpm_limit = 1000000 WHERE id = ?", (result["id"],))


def run_single():
    ws = int(_time.time())
    reserve_rate(result["id"], 1000000, 1000000, 5)
    reconcile_rate(result["id"], 8, ws, 5)


def run_batch(n=2000):
    pr = cProfile.Profile()
    pr.enable()
    for _ in range(n):
        run_single()
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(20)
    print(s.getvalue())


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    reset_db_connection()
    print(f"Profiling {n} iterations...\n")
    run_batch(n)
