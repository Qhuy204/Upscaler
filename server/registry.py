"""Live worker registry.

workers          zset  worker name -> last heartbeat
worker:{name}    hash  role, host, queue, slots, running, done, failed, started_at, seen (TTL)
"""
import time

WORKERS = "workers"
TTL = 30          # a worker missing heartbeats this long is gone
INTERVAL = 5


async def beat(r, name, info):
    now = time.time()
    async with r.pipeline(transaction=True) as p:
        p.hset(f"worker:{name}", mapping={**{k: str(v) for k, v in info.items()}, "seen": f"{now:.0f}"})
        p.expire(f"worker:{name}", TTL)
        p.zadd(WORKERS, {name: now})
        p.zremrangebyscore(WORKERS, 0, now - 86400)  # forget long-dead names
        await p.execute()


async def leave(r, name):
    async with r.pipeline(transaction=True) as p:
        p.delete(f"worker:{name}")
        p.zrem(WORKERS, name)
        await p.execute()


async def alive(r, role=None):
    """Workers with a heartbeat in the last TTL seconds, optionally of one role."""
    names = await r.zrangebyscore(WORKERS, time.time() - TTL, "+inf")
    out = []
    for n in names:
        h = await r.hgetall(f"worker:{n}")
        if h and (role is None or h.get("role") == role):
            out.append({"name": n, **h})
    return out


async def recent(r, since=3600):
    """Alive and recently-dead workers (dead ones only have a name and last-seen time)."""
    now = time.time()
    out = []
    for n, seen in await r.zrangebyscore(WORKERS, now - since, "+inf", withscores=True):
        h = await r.hgetall(f"worker:{n}")
        out.append({"name": n, **h, "seen": seen, "alive": bool(h) and now - seen <= TTL})
    return out
