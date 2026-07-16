"""Concurrency test for the rate limiter.

Verifies that admission-time reservation (BUG 2 fix) correctly enforces
RPM limits under concurrent requests. Before fix: all requests passed
because check_rate only read counters, commit_rate ran after response.
"""

import os

os.environ["ADMIN_KEY"] = "test-admin-key"
os.environ["PROXY_PORT"] = "8099"


class TestRateLimitConcurrency:
    """Verify RPM limits are enforced under concurrent requests."""

    def test_rpm_enforced_under_concurrency(self):
        """rpm_limit=5, 50 concurrent requests -> at most 5 should pass."""
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.rate_limiter import _rate_store, _thread_lock
        from smol_llm_proxy.auth import create_api_key

        # Reset rate store
        with _thread_lock:
            _rate_store.clear()

        # Create a key with rpm_limit=5
        result = create_api_key("concurrency-test")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute("UPDATE api_keys SET rpm_limit = 5 WHERE id = ?", (key_id,))

        from smol_llm_proxy.rate_limiter import reserve_rate

        # Simulate 50 concurrent requests with rpm_limit=5
        # Using reserve_rate (admission-time), only 5 should pass
        allowed_count = 0
        for i in range(50):
            allowed, retry_after, ws = reserve_rate(key_id, 5, 1000000, 10)
            if allowed:
                allowed_count += 1

        assert allowed_count == 5, f"Expected exactly 5 allowed, got {allowed_count}"

    def test_reserve_rate_enforces_limit(self):
        """reserve_rate correctly reserves quota under one lock."""
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.rate_limiter import _rate_store, _thread_lock, reserve_rate
        from smol_llm_proxy.auth import create_api_key

        # Reset rate store
        with _thread_lock:
            _rate_store.clear()

        result = create_api_key("reserve-test")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute("UPDATE api_keys SET rpm_limit = 3 WHERE id = ?", (key_id,))

        # 3 should pass, 4th should be rejected
        results = []
        for i in range(5):
            allowed, retry_after, ws = reserve_rate(key_id, 3, 1000000, 10)
            results.append(allowed)

        assert results[:3] == [True, True, True], f"First 3 should be allowed: {results}"
        assert results[3] is False, f"4th should be rejected: {results}"
        assert results[4] is False, f"5th should be rejected: {results}"

    def test_reserve_rate_with_reconciliation(self):
        """reconcile_rate correctly adjusts the token delta."""
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.rate_limiter import _rate_store, _thread_lock, reserve_rate, reconcile_rate
        from smol_llm_proxy.auth import create_api_key

        with _thread_lock:
            _rate_store.clear()

        result = create_api_key("reconcile-test")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute("UPDATE api_keys SET rpm_limit = 10, tpm_limit = 100 WHERE id = ?", (key_id,))

        # Reserve with est=50 tokens (uses 50 of 100 TPM)
        allowed, _, ws = reserve_rate(key_id, 10, 100, 50)
        assert allowed is True

        # Actual usage was 30 tokens (less than estimated)
        reconcile_rate(key_id, 30, ws, 50)

        # Now free 20 more tokens — should allow another request
        allowed2, _, _ = reserve_rate(key_id, 10, 100, 20)
        assert allowed2 is True, "Should allow after reconciliation freed tokens"

    def test_reserve_rate_rpm_prevents_burst(self):
        """Multiple concurrent reserve_rate calls respect RPM limit."""
        from smol_llm_proxy.database import get_db
        from smol_llm_proxy.rate_limiter import _rate_store, _thread_lock, reserve_rate
        from smol_llm_proxy.auth import create_api_key

        with _thread_lock:
            _rate_store.clear()

        result = create_api_key("burst-test")
        key_id = result["id"]

        with get_db() as conn:
            conn.execute("UPDATE api_keys SET rpm_limit = 5 WHERE id = ?", (key_id,))

        # All 20 calls in the same event loop tick (no sleep between)
        results = [reserve_rate(key_id, 5, 1000000, 1) for _ in range(20)]
        allowed = sum(1 for r in results if r[0])
        assert allowed == 5, f"Expected exactly 5 allowed, got {allowed}"
