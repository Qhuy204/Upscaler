"""Manage API keys.

python tools/manage_keys.py create <name> [--max-queued N] [--max-running N]
python tools/manage_keys.py list
python tools/manage_keys.py disable <key_id> | enable <key_id>
python tools/manage_keys.py limits <key_id> [--max-queued N] [--max-running N] [--rate-limit N]
python tools/manage_keys.py allow <key_id> <ip/cidr,...|*>     # only accept this key from these IPs
python tools/manage_keys.py unban <ip>
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import redis.asyncio as aioredis  # noqa: E402

from server import auth, security  # noqa: E402
from server.config import get_settings  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("name")
    lim = sub.add_parser("limits")
    lim.add_argument("key_id")
    lim.add_argument("--rate-limit", type=int)
    for p in (c, lim):
        p.add_argument("--max-queued", type=int)
        p.add_argument("--max-running", type=int)
    sub.add_parser("list")
    sub.add_parser("disable").add_argument("key_id")
    sub.add_parser("enable").add_argument("key_id")
    al = sub.add_parser("allow")
    al.add_argument("key_id")
    al.add_argument("ips")
    sub.add_parser("unban").add_argument("ip")
    a = ap.parse_args()

    s = get_settings()
    r = aioredis.from_url(s.redis_url, decode_responses=True)
    try:
        if a.cmd == "create":
            key, rec = await auth.create_key(r, a.name, a.max_queued, a.max_running)
            print(f"key_id:         {rec['key_id']}\napi_key:        {key}\nwebhook_secret: {rec['webhook_secret']}")
            print("Store the api_key now; only its hash is kept.")
        elif a.cmd == "list":
            for rec in await auth.list_keys(r):
                mq, mr = auth.limits(rec, s)
                state = "disabled" if rec.get("disabled") == "1" else "active"
                created = time.strftime("%Y-%m-%d", time.localtime(float(rec["created_at"])))
                rl = rec.get("rate_limit") or s.rate_limit_per_min
                ips = rec.get("allowed_ips") or "any"
                print(f"{rec['key_id']}  {state:8}  queued<={mq:<4} running<={mr:<3} rpm<={rl:<5} {created}  "
                      f"{rec['name']}  ips={ips}")
        elif a.cmd in ("disable", "enable"):
            ok = await auth.set_fields(r, a.key_id, disabled="1" if a.cmd == "disable" else "0")
            print("ok" if ok else "no such key")
        elif a.cmd == "limits":
            fields = {k: v for k, v in (("max_queued", a.max_queued), ("max_running", a.max_running),
                                        ("rate_limit", a.rate_limit)) if v is not None}
            ok = fields and await auth.set_fields(r, a.key_id, **fields)
            print("ok" if ok else "nothing changed")
        elif a.cmd == "allow":
            try:
                ips = security.parse_networks(a.ips)
            except ValueError as e:
                sys.exit(f"bad IP/CIDR: {e}")
            ok = await auth.set_fields(r, a.key_id, allowed_ips=ips)
            print(f"ok, allowed: {ips or 'any'}" if ok else "no such key")
        elif a.cmd == "unban":
            n = await r.delete(f"ban:{a.ip}", f"authfail:{a.ip}")
            print("ok" if n else "not banned")
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())
