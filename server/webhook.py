"""Signed webhook delivery.

Headers: X-Upscale-Event, X-Upscale-Timestamp, X-Upscale-Signature: sha256=<hex>
Signature = HMAC-SHA256(webhook_secret, f"{timestamp}.{raw_body}").
"""
import hashlib
import hmac
import json
import time

import httpx

from .fetch import FetchError, check_url

BACKOFF = [10, 30, 120, 600, 1800]  # seconds before attempt 2..6


def signature(secret, ts, body: bytes):
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def verify(secret, ts, body: bytes, sig, tolerance=300):
    """Helper for receivers (also used in tests)."""
    if abs(time.time() - int(ts)) > tolerance:
        return False
    return hmac.compare_digest(signature(secret, ts, body), sig)


async def send(url, secret, event, payload, client=None):
    """POST once. Returns True on 2xx, False on a retryable failure; raises FetchError if URL is blocked."""
    await check_url(url)
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "User-Agent": "upscale-webhook/1",
               "X-Upscale-Event": event, "X-Upscale-Timestamp": ts,
               "X-Upscale-Signature": signature(secret, ts, body)}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        r = await client.post(url, content=body, headers=headers)
        return 200 <= r.status_code < 300
    except httpx.HTTPError:
        return False
    finally:
        if own:
            await client.aclose()


__all__ = ["BACKOFF", "FetchError", "send", "signature", "verify"]
