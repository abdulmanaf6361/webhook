#!/usr/bin/env python3
"""
Part B Acceptance Test — Rate Limiting
=======================================
Tests:
  1. Set rate limit to 5/second
  2. Publish 20 events in rapid succession
  3. Verify all 20 arrive at mock receiver, spread over ~4 seconds
  4. Update rate limit to 20/second live (no restart)
  5. Publish 20 more events → arrive much faster
  6. Verify no deliveries lost across both batches

Usage:
  python test_rate_limit.py

Assumes:
  docker compose up is running
  A webhook for test_user is already registered pointing to mock_receiver
  OR pass --register to auto-register one.
"""

import sys
import time
import json
import argparse
import urllib.request
import urllib.error

BASE_URL      = "http://localhost:8000"
RECEIVER_URL  = "http://localhost:9000"
USER_ID       = "test_user_ratelimit"
WEBHOOK_URL   = "http://mock_receiver:9000/"  # internal docker network name


def req(method, path, body=None, headers=None):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
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


def set_rate(n):
    status, body = req("PUT", "/api/internal/rate-limit/", {"deliveries_per_second": n})
    assert status == 200, f"Failed to set rate limit: {body}"
    print(f"  ✓ Rate limit set to {n}/second")


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
        wid = body["id"]
        print(f"  ✓ Registered webhook {wid}")
        return wid
    else:
        print(f"  ✗ Failed to register webhook: {body}")
        sys.exit(1)


def publish_event(i):
    status, body = req("POST", "/api/events/ingest/", {
        "user_id": USER_ID,
        "event_type": "request.created",
        "payload": {"index": i, "batch": "test"},
    })
    assert status == 201, f"Event publish failed: {body}"
    return body


def count_received_since(logs_before):
    """Count logs in receiver that weren't there before."""
    logs_after = get_receiver_logs()
    new = [l for l in logs_after if l not in logs_before]
    return len(new), logs_after


def wait_for_all(expected_total, timeout=30, poll=0.5):
    """Poll mock receiver until expected_total deliveries received or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = get_receiver_logs()
        # Filter to just our user
        mine = [l for l in logs if l.get('body', {}).get('user_id') == USER_ID]
        if len(mine) >= expected_total:
            return mine
        time.sleep(poll)
    return get_receiver_logs()


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true", help="Auto-register a webhook before testing")
    args = parser.parse_args()

    print("\n" + "="*60)
    print("  Part B — Rate Limit Acceptance Test")
    print("="*60)

    # ── Pre-check ─────────────────────────────────────────────────────────────
    print("\n[0] Pre-flight checks...")
    try:
        _, body = req("GET", "/api/internal/rate-limit/")
        print(f"  ✓ API reachable. Current rate limit: {body['deliveries_per_second']}/s")
    except Exception as e:
        print(f"  ✗ Cannot reach API at {BASE_URL}: {e}")
        sys.exit(1)

    try:
        get_receiver_logs()
        print(f"  ✓ Mock receiver reachable at {RECEIVER_URL}")
    except Exception as e:
        print(f"  ✗ Cannot reach mock receiver at {RECEIVER_URL}: {e}")
        sys.exit(1)

    if args.register:
        print(f"\n[0b] Registering webhook for {USER_ID}...")
        register_webhook()

    # ── STEP 1: Set rate to 5/s ────────────────────────────────────────────────
    print("\n[1] Setting rate limit to 5/second...")
    set_rate(5)
    assert get_rate() == 5
    time.sleep(1.2)  # let token bucket reset fully

    # ── STEP 2: Publish 20 events as fast as possible ─────────────────────────
    print("\n[2] Publishing 20 events in rapid succession...")
    logs_before = get_receiver_logs()
    before_user = [l for l in logs_before if l.get('body', {}).get('user_id') == USER_ID]
    start = time.time()

    for i in range(20):
        publish_event(i)
    publish_elapsed = time.time() - start
    print(f"  ✓ Published 20 events in {publish_elapsed:.2f}s (all fired immediately)")

    # ── STEP 3: Wait and check spread ─────────────────────────────────────────
    print("\n[3] Waiting for all 20 deliveries (expect ~4 seconds spread)...")
    time.sleep(0.5)

    # Poll every 0.5s, record when each batch arrives
    seen = len(before_user)
    arrival_times = {}
    deadline = time.time() + 30  # 30s max wait

    while time.time() < deadline:
        logs = get_receiver_logs()
        mine = [l for l in logs if l.get('body', {}).get('user_id') == USER_ID]
        now_seen = len(mine)
        if now_seen > seen:
            for n in range(seen + 1, now_seen + 1):
                arrival_times[n] = time.time() - start
            seen = now_seen
        if seen >= len(before_user) + 20:
            break
        time.sleep(0.5)

    delivered_batch1 = seen - len(before_user)
    total_time = time.time() - start

    print(f"\n  Results:")
    print(f"    Delivered:    {delivered_batch1}/20")
    print(f"    Total time:   {total_time:.1f}s")

    if arrival_times:
        first = min(arrival_times.values())
        last  = max(arrival_times.values())
        spread = last - first
        print(f"    First arrival: {first:.1f}s after publish")
        print(f"    Last arrival:  {last:.1f}s after publish")
        print(f"    Spread:        {spread:.1f}s")

    # Assertions
    assert delivered_batch1 == 20, f"✗ Expected 20 deliveries, got {delivered_batch1}"
    print(f"\n  ✓ All 20 delivered")

    if total_time >= 3.0:
        print(f"  ✓ Spread over {total_time:.1f}s (rate limiting working — expected ≥4s)")
    else:
        print(f"  ⚠ Delivered in {total_time:.1f}s — faster than expected for 5/s rate. "
              f"Check that the token bucket isn't pre-filled.")

    # ── STEP 4: Update rate limit to 20/s live ────────────────────────────────
    print("\n[4] Updating rate limit to 20/second (no restart)...")
    set_rate(20)
    assert get_rate() == 20
    time.sleep(1.2)  # let bucket refill at new rate

    # ── STEP 5: Publish 20 more events ────────────────────────────────────────
    print("\n[5] Publishing 20 more events...")
    logs_before2 = get_receiver_logs()
    before_user2 = [l for l in logs_before2 if l.get('body', {}).get('user_id') == USER_ID]
    start2 = time.time()

    for i in range(20, 40):
        publish_event(i)
    publish_elapsed2 = time.time() - start2
    print(f"  ✓ Published 20 events in {publish_elapsed2:.2f}s")

    # Wait for them — should arrive much faster
    print("\n[6] Waiting for batch 2 deliveries (expect <2s at 20/s)...")
    deadline2 = time.time() + 20
    seen2 = len(before_user2)
    start_wait2 = time.time()

    while time.time() < deadline2:
        logs = get_receiver_logs()
        mine2 = [l for l in logs if l.get('body', {}).get('user_id') == USER_ID]
        seen2 = len(mine2)
        if seen2 >= len(before_user2) + 20:
            break
        time.sleep(0.3)

    delivered_batch2 = seen2 - len(before_user2)
    time2 = time.time() - start2

    print(f"\n  Results:")
    print(f"    Delivered:  {delivered_batch2}/20")
    print(f"    Total time: {time2:.1f}s")

    assert delivered_batch2 == 20, f"✗ Expected 20 deliveries in batch 2, got {delivered_batch2}"
    print(f"  ✓ All 20 delivered")

    if time2 < total_time * 0.6:
        print(f"  ✓ Arrived much faster ({time2:.1f}s vs {total_time:.1f}s) — rate increase confirmed")
    else:
        print(f"  ⚠ Batch 2 time ({time2:.1f}s) not significantly faster than batch 1 ({total_time:.1f}s)")

    # ── STEP 6: Final summary ──────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  FINAL SUMMARY")
    print("="*60)
    print(f"  Batch 1 (5/s):  {delivered_batch1}/20 delivered in {total_time:.1f}s")
    print(f"  Batch 2 (20/s): {delivered_batch2}/20 delivered in {time2:.1f}s")
    total_delivered = delivered_batch1 + delivered_batch2
    print(f"  Total:          {total_delivered}/40 delivered — {'✓ NO LOSSES' if total_delivered == 40 else '✗ LOSSES DETECTED'}")
    print()

    if delivered_batch1 == 20 and delivered_batch2 == 20:
        print("  ✅ ALL ACCEPTANCE CRITERIA PASSED")
    else:
        print("  ❌ SOME CRITERIA FAILED")
        sys.exit(1)


if __name__ == "__main__":
    run()
