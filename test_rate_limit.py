#!/usr/bin/env python3
"""
Part B Acceptance Test — Rate Limiting
=======================================
Key fix vs previous version:
  Events are published CONCURRENTLY (via threads) so all 20 hit the
  API within ~0.5s. This creates a real burst that the rate limiter
  must throttle. Previously events were published sequentially (one
  per HTTP round-trip = ~0.5s each = 10s to publish 20), which meant
  the rate limiter never saw a burst at all.

Tests:
  1. Set rate limit to 5/second
  2. Publish 20 events concurrently (all in <1s)
  3. Verify all 20 arrive at mock receiver spread over ~4 seconds
  4. Update rate limit to 20/second live (no restart)
  5. Publish 20 more events concurrently
  6. Verify they arrive in ~1 second (much faster than batch 1)
  7. Verify zero losses across both batches (40/40)

Usage:
  python test_rate_limit.py [--register]
"""

import sys
import time
import json
import argparse
import threading
import urllib.request
import urllib.error

BASE_URL     = "http://localhost:8000"
RECEIVER_URL = "http://localhost:9000"
USER_ID      = "test_user_ratelimit"
WEBHOOK_URL  = "http://mock_receiver:9000/"  # internal docker network name


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def req(method, path, body=None, headers=None):
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    h    = {"Content-Type": "application/json", **(headers or {})}
    r    = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get_receiver_logs():
    try:
        with urllib.request.urlopen(f"{RECEIVER_URL}/logs", timeout=5) as r:
            return json.loads(r.read())['logs']
    except Exception as e:
        print(f"  ⚠ Could not reach mock receiver: {e}")
        return []


def user_logs(all_logs):
    """Filter logs to only our test user."""
    return [l for l in all_logs if l.get('body', {}).get('user_id') == USER_ID]


# ── Setup helpers ──────────────────────────────────────────────────────────────

def set_rate(n):
    status, body = req("PUT", "/api/internal/rate-limit/", {"deliveries_per_second": n})
    assert status == 200, f"Failed to set rate: {body}"
    print(f"  ✓ Rate limit → {n}/second")


def get_rate():
    _, body = req("GET", "/api/internal/rate-limit/")
    return body["deliveries_per_second"]


def register_webhook():
    status, body = req(
        "POST", "/api/webhooks/",
        {"url": WEBHOOK_URL, "event_types": ["request.created"]},
        {"X-User-Id": USER_ID},
    )
    if status == 201:
        print(f"  ✓ Registered webhook {body['id']}")
    else:
        print(f"  ✗ Failed to register webhook: {body}")
        sys.exit(1)


# ── Concurrent publish ─────────────────────────────────────────────────────────

def publish_burst(count, offset=0):
    """
    Fire `count` event-ingest requests simultaneously using threads.
    Returns (elapsed_seconds, errors).
    All threads are started before any waits, so the burst lands within
    one HTTP round-trip (~0.3-0.5s) rather than count × round-trips.
    """
    errors  = []
    threads = []
    start   = time.time()

    def _publish(i):
        status, body = req("POST", "/api/events/ingest/", {
            "user_id":    USER_ID,
            "event_type": "request.created",
            "payload":    {"index": offset + i},
        })
        if status != 201:
            errors.append(f"Event {offset+i} failed: {body}")

    for i in range(count):
        t = threading.Thread(target=_publish, args=(i,))
        threads.append(t)

    # Start all threads at once — this is the burst
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    elapsed = time.time() - start
    return elapsed, errors


# ── Wait & measure ─────────────────────────────────────────────────────────────

def wait_and_measure(baseline_count, expected, label, timeout=45):
    """
    Poll mock receiver until `expected` new deliveries arrive (for our user).
    Records timestamps of each delivery wave to compute spread.
    Returns (delivered_count, total_elapsed, spread_seconds).
    """
    start        = time.time()
    seen         = baseline_count
    first_time   = None
    last_time    = None
    deadline     = start + timeout

    print(f"  Polling for {expected} deliveries", end="", flush=True)

    while time.time() < deadline:
        logs     = get_receiver_logs()
        now_seen = len(user_logs(logs))
        if now_seen > seen:
            if first_time is None:
                first_time = time.time() - start
            last_time = time.time() - start
            print(f".", end="", flush=True)
            seen = now_seen
        if seen >= baseline_count + expected:
            break
        time.sleep(0.4)

    print()  # newline after dots

    delivered = seen - baseline_count
    elapsed   = time.time() - start
    spread    = (last_time - first_time) if (first_time and last_time) else 0
    return delivered, elapsed, spread, first_time or 0, last_time or 0


# ── Main test ──────────────────────────────────────────────────────────────────

def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true",
                        help="Register a fresh webhook before testing")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  Part B — Rate Limit Acceptance Test")
    print("="*60)

    # ── Pre-flight ─────────────────────────────────────────────────────────────
    print("\n[0] Pre-flight checks...")
    try:
        _, body = req("GET", "/api/internal/rate-limit/")
        print(f"  ✓ API reachable  |  current rate: {body['deliveries_per_second']}/s")
    except Exception as e:
        print(f"  ✗ Cannot reach API at {BASE_URL}: {e}")
        sys.exit(1)

    try:
        get_receiver_logs()
        print(f"  ✓ Mock receiver reachable at {RECEIVER_URL}")
    except Exception as e:
        print(f"  ✗ Cannot reach mock receiver: {e}")
        sys.exit(1)

    if args.register:
        print(f"\n[0b] Registering webhook for {USER_ID}...")
        register_webhook()

    # ── Batch 1: rate=5/s ──────────────────────────────────────────────────────
    print("\n[1] Setting rate limit to 5/second...")
    set_rate(5)
    time.sleep(1.5)   # let token bucket start empty after reset

    baseline1 = len(user_logs(get_receiver_logs()))
    print(f"\n[2] Publishing 20 events CONCURRENTLY (burst)...")
    pub_time1, errors1 = publish_burst(20, offset=0)

    if errors1:
        print(f"  ✗ Publish errors: {errors1}")
        sys.exit(1)
    print(f"  ✓ All 20 published in {pub_time1:.2f}s  ← this must be fast (< 2s)")

    if pub_time1 > 3:
        print(f"  ⚠ Publishing took {pub_time1:.1f}s — API may be overloaded or sequential")

    print(f"\n[3] Waiting for deliveries at 5/s (expect ~4s spread)...")
    delivered1, elapsed1, spread1, t_first1, t_last1 = wait_and_measure(
        baseline1, 20, "batch1", timeout=45
    )

    print(f"\n  ── Batch 1 Results ──")
    print(f"  Published in : {pub_time1:.2f}s")
    print(f"  Delivered    : {delivered1}/20")
    print(f"  Total time   : {elapsed1:.1f}s")
    print(f"  First arrived: {t_first1:.1f}s after publish")
    print(f"  Last arrived : {t_last1:.1f}s after publish")
    print(f"  Spread       : {spread1:.1f}s  (expected ≥ 3s for 5/s rate)")

    if delivered1 != 20:
        print(f"  ✗ FAIL: Only {delivered1}/20 delivered")
        sys.exit(1)
    print(f"  ✓ All 20 delivered")

    if spread1 >= 2.5:
        print(f"  ✓ Rate limiting confirmed — deliveries spread over {spread1:.1f}s")
    else:
        print(f"  ⚠ Spread was only {spread1:.1f}s — rate limiter may not be throttling correctly")

    # ── Update rate live ───────────────────────────────────────────────────────
    print(f"\n[4] Updating rate limit to 20/second (live, no restart)...")
    set_rate(20)
    assert get_rate() == 20
    time.sleep(1.5)   # let bucket reset at new rate

    # ── Batch 2: rate=20/s ─────────────────────────────────────────────────────
    baseline2 = len(user_logs(get_receiver_logs()))
    print(f"\n[5] Publishing 20 more events CONCURRENTLY (burst)...")
    pub_time2, errors2 = publish_burst(20, offset=20)

    if errors2:
        print(f"  ✗ Publish errors: {errors2}")
        sys.exit(1)
    print(f"  ✓ All 20 published in {pub_time2:.2f}s")

    print(f"\n[6] Waiting for deliveries at 20/s (expect ≤ 2s spread)...")
    delivered2, elapsed2, spread2, t_first2, t_last2 = wait_and_measure(
        baseline2, 20, "batch2", timeout=30
    )

    print(f"\n  ── Batch 2 Results ──")
    print(f"  Published in : {pub_time2:.2f}s")
    print(f"  Delivered    : {delivered2}/20")
    print(f"  Total time   : {elapsed2:.1f}s")
    print(f"  First arrived: {t_first2:.1f}s after publish")
    print(f"  Last arrived : {t_last2:.1f}s after publish")
    print(f"  Spread       : {spread2:.1f}s  (expected ≤ 2s for 20/s rate)")

    if delivered2 != 20:
        print(f"  ✗ FAIL: Only {delivered2}/20 delivered")
        sys.exit(1)
    print(f"  ✓ All 20 delivered")

    if spread2 < spread1 * 0.5:
        print(f"  ✓ Arrived much faster ({spread2:.1f}s vs {spread1:.1f}s) — rate increase confirmed")
    else:
        print(f"  ⚠ Batch 2 spread ({spread2:.1f}s) not significantly less than batch 1 ({spread1:.1f}s)")

    # ── Final summary ──────────────────────────────────────────────────────────
    total = delivered1 + delivered2
    print("\n" + "="*60)
    print("  FINAL SUMMARY")
    print("="*60)
    print(f"  Batch 1 (5/s) : {delivered1}/20 | spread {spread1:.1f}s | total {elapsed1:.1f}s")
    print(f"  Batch 2 (20/s): {delivered2}/20 | spread {spread2:.1f}s | total {elapsed2:.1f}s")
    print(f"  Grand total   : {total}/40 — {'✓ NO LOSSES' if total == 40 else '✗ LOSSES DETECTED'}")
    print()

    ok = (
        delivered1 == 20 and
        delivered2 == 20 and
        spread1 >= 2.5 and        # 5/s should spread 20 events over ≥ 2.5s
        spread2 < spread1 * 0.6   # 20/s should be noticeably faster
    )

    if ok:
        print("  ✅ ALL ACCEPTANCE CRITERIA PASSED")
    else:
        print("  ❌ SOME CRITERIA FAILED — check spread values above")
        sys.exit(1)


if __name__ == "__main__":
    run()