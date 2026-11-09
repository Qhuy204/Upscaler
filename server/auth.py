"""API keys. Only the SHA-256 of a key is stored.

apikey:{sha256}   hash  key_id, name, webhook_secret, max_queued, max_running, disabled, created_at
apikey_ids        hash  key_id -> sha256
"""
import hashlib
import secrets
import time

PREFIX = "upk_"


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


async def create_key(r, name, max_queued=None, max_running=None):
    key = PREFIX + secrets.token_urlsafe(32)
    key_id = "k_" + secrets.token_hex(6)
    h = hash_key(key)
    rec = {"key_id": key_id, "name": name, "webhook_secret": "whsec_" + secrets.token_urlsafe(24),
           "max_queued": "" if max_queued is None else str(max_queued),
           "max_running": "" if max_running is None else str(max_running),
           "disabled": "0", "created_at": str(time.time())}
    await r.hset(f"apikey:{h}", mapping=rec)
    await r.hset("apikey_ids", key_id, h)
    return key, rec


async def lookup(r, key):
    if not key or not key.startswith(PREFIX):
        return None
    rec = await r.hgetall(f"apikey:{hash_key(key)}")
    if not rec or rec.get("disabled") == "1":
        return None
    return rec


async def get_by_id(r, key_id):
    h = await r.hget("apikey_ids", key_id)
    return await r.hgetall(f"apikey:{h}") if h else None


async def list_keys(r):
    out = []
    for key_id, h in (await r.hgetall("apikey_ids")).items():
        rec = await r.hgetall(f"apikey:{h}")
        if rec:
            out.append(rec)
    return out


async def set_fields(r, key_id, **fields):
    h = await r.hget("apikey_ids", key_id)
    if not h:
        return False
    await r.hset(f"apikey:{h}", mapping={k: str(v) for k, v in fields.items()})
    return True


def limits(rec, s):
    mq = rec.get("max_queued") or ""
    mr = rec.get("max_running") or ""
    return (int(mq) if mq else s.default_max_queued, int(mr) if mr else s.default_max_running)
