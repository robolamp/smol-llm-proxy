"""System test: reconcile_rate persists deltas across flush cycles.

Reproduces T1: on 6d398fb the bucket is flushed from _rate_store before
reconcile_rate fires, so the delta vanishes and token_sum stays at the
estimated input tokens instead of the actual completion tokens.

Fix: reconcile_rate queues unmatched deltas in _pending; _flush_to_db
applies them with the same UPSERT.
"""

import asyncio
import os

os.environ["ADMIN_KEY"] = "test-admin-key"
os.environ["PROXY_PORT"] = "8099"


class TestRateReconcileSystem:
    """Verify reconcile_rate survives a flush cycle."""

    def test_reconcile_across_flush(self):
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.rate_limiter import (
            _rate_store,
            _pending,
            _thread_lock,
            _flush_to_db,
            reserve_rate,
            reconcile_rate,
            start_rate_flush,
            stop_rate_flush,
        )
        from smol_llm_proxy.auth import create_api_key

        async def _run():
            with _thread_lock:
                _rate_store.clear()
                _pending.clear()

            result = create_api_key("reconcile-system-flush")
            key_id = result["id"]

            with get_db() as conn:
                conn.execute("UPDATE api_keys SET rpm_limit = 10, tpm_limit = 100000 WHERE id = ?", (key_id,))
                conn.execute("DELETE FROM rate_limits WHERE key_id = ?", (key_id,))

            start_rate_flush()
            try:
                allowed, _, ws = reserve_rate(key_id, 10, 1000000, 10)
                assert allowed is True

                await _flush_to_db()

                reconcile_rate(key_id, actual_tokens=5010, admission_bucket=ws, tokens_estimated=10)

                await _flush_to_db()

                with get_db() as conn:
                    row = conn.execute(
                        "SELECT COALESCE(SUM(token_sum), 0) as ts FROM rate_limits WHERE key_id = ?",
                        (key_id,),
                    ).fetchone()
                    assert row["ts"] == 5010, f"Expected token_sum=5010, got {row['ts']}"
            finally:
                stop_rate_flush()

        asyncio.run(_run())

    def test_reconcile_pending_cleared_after_flush(self):
        from smol_llm_proxy.rate_limiter import (
            _pending,
            _thread_lock,
            _flush_to_db,
            reconcile_rate,
            start_rate_flush,
            stop_rate_flush,
        )

        async def _run():
            with _thread_lock:
                _pending.clear()

            start_rate_flush()
            try:
                reconcile_rate(999, 5010, 1700000000, 10)

                assert len(_pending) == 1

                await _flush_to_db()

                with _thread_lock:
                    assert len(_pending) == 0
            finally:
                stop_rate_flush()

        asyncio.run(_run())
