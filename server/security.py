"""Request-level protection: client IP, failed-auth lockout, per-key rate limit, IP allowlist, headers.

authfail:{ip}      counter, expires after auth_fail_window
ban:{ip}           set when an IP reaches auth_fail_limit; requests from it get 429 until it expires
rl:{key_id}:{min}  per-minute request counter for an API key
log:requests       stream of API calls {t, ip, key_id, method, path, status, ms, job_id, ua}
"""
import ipaddress
import logging
import time

from fastapi import HTTPException, Request

log = logging.getLogger("upscale.security")
REQUEST_LOG = "log:requests"
REQUEST_LOG_MAX = 50_000

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Cache-Control": "no-store",
}


def client_ip(request: Request):
    # The API listens on 127.0.0.1 only, so every remote request comes through cloudflared, and
    # Cloudflare overwrites CF-Connecting-IP with the real client address.
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


async def check_banned(r, ip):
    ttl = await r.ttl(f"ban:{ip}")
    if ttl and ttl > 0:
        raise HTTPException(429, "too many failed attempts; try again later", headers={"Retry-After": str(ttl)})


async def auth_failed(r, s, ip, path):
    k = f"authfail:{ip}"
    n = await r.incr(k)
    if n == 1:
        await r.expire(k, s.auth_fail_window)
    log.warning("auth failure %s from %s on %s", n, ip, path)
    if n >= s.auth_fail_limit:
        await r.set(f"ban:{ip}", "1", ex=s.auth_ban_seconds)
        await r.delete(k)
        log.warning("banned %s for %ss after %s failed attempts", ip, s.auth_ban_seconds, n)


async def rate_limit(r, s, rec):
    limit = int(rec.get("rate_limit") or 0) or s.rate_limit_per_min
    now = time.time()
    k = f"rl:{rec['key_id']}:{int(now // 60)}"
    n = await r.incr(k)
    if n == 1:
        await r.expire(k, 120)
    if n > limit:
        raise HTTPException(429, f"rate limit is {limit} requests per minute",
                            headers={"Retry-After": str(60 - int(now % 60))})


def ip_allowed(rec, ip):
    """Key field allowed_ips: comma-separated IPs/CIDRs; empty means any."""
    spec = (rec.get("allowed_ips") or "").strip()
    if not spec:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in spec.split(","):
        try:
            if addr in ipaddress.ip_network(net.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def parse_networks(spec):
    """Validate an allowed_ips value; returns it normalized or raises ValueError."""
    if spec.strip() in ("", "*"):
        return ""
    return ",".join(str(ipaddress.ip_network(n.strip(), strict=False)) for n in spec.split(",") if n.strip())


async def read_limited(request: Request, limit):
    if (cl := request.headers.get("content-length")) and cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, f"request body is limited to {limit // 1024} KB")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, f"request body is limited to {limit // 1024} KB")
    return bytes(body)
