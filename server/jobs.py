"""Job state in Redis.

job:{id}              hash   all job fields (TTL job_ttl)
jobs:{key_id}         zset   job ids by created_at (for listing)
active:{key_id}       set    jobs not yet finished (max_queued quota)
running:{key_id}      zset   jobs holding a worker, scored by heartbeat (max_running quota)
idem:{key_id}:{k}     string idempotency key -> job id
stats:{event}:{hour}  counter done / failed per hour
jobs:all              zset   every job id by created_at (dashboard)
log:jobs              stream status changes {t, job_id, key_id, status, worker, error} (dashboard)
"""
import json
import time
import uuid

ALL = "jobs:all"
EVENTS = "log:jobs"
EVENTS_MAX = 100_000
WORKER = ""  # name of this process, set by run_worker; recorded on status changes

QUEUED, DOWNLOADING, PROCESSING, UPLOADING = "queued", "downloading", "processing", "uploading"
DONE, FAILED, CANCELED = "done", "failed", "canceled"
TERMINAL = {DONE, FAILED, CANCELED}
RUNNING_STALE = 60  # a running entry without heartbeat for this long is dropped

_JSON_FIELDS = ("options", "input", "output")


def new_id():
    return "j_" + uuid.uuid4().hex[:20]


def _dump(d):
    return {k: json.dumps(v) if k in _JSON_FIELDS else ("" if v is None else str(v)) for k, v in d.items()}


def _load(h):
    if not h:
        return None
    out = {}
    for k, v in h.items():
        if k in _JSON_FIELDS:
            out[k] = json.loads(v) if v else None
        elif k in ("progress", "created_at", "started_at", "finished_at"):
            out[k] = float(v) if v else None
        elif k in ("attempts",):
            out[k] = int(v) if v else 0
        else:
            out[k] = v if v != "" else None
    return out


def _event(p, job_id, status, key_id=None, error=None):
    e = {"t": f"{time.time():.3f}", "job_id": job_id, "status": status}
    if key_id:
        e["key_id"] = key_id
    if WORKER:
        e["worker"] = WORKER
    if error:
        e["error"] = str(error)[:300]
    p.xadd(EVENTS, e, maxlen=EVENTS_MAX, approximate=True)


async def create(r, s, key_id, source_type, source, options, callback_url=None, idempotency_key=None,
                 filename=None, job_id=None):
    job_id = job_id or new_id()
    now = time.time()
    job = {"id": job_id, "key_id": key_id, "status": QUEUED, "progress": 0, "created_at": now,
           "source_type": source_type, "source": source, "options": options, "callback_url": callback_url,
           "idempotency_key": idempotency_key, "filename": filename, "attempts": 0}
    async with r.pipeline(transaction=True) as p:
        p.hset(f"job:{job_id}", mapping=_dump(job))
        p.expire(f"job:{job_id}", s.job_ttl)
        p.zadd(f"jobs:{key_id}", {job_id: now})
        p.zremrangebyscore(f"jobs:{key_id}", 0, now - s.job_ttl)
        p.sadd(f"active:{key_id}", job_id)
        p.zadd(ALL, {job_id: now})
        p.zremrangebyscore(ALL, 0, now - s.job_ttl)
        _event(p, job_id, QUEUED, key_id)
        if idempotency_key:
            p.set(f"idem:{key_id}:{idempotency_key}", job_id, ex=s.job_ttl)
        await p.execute()
    return job


async def get(r, job_id):
    return _load(await r.hgetall(f"job:{job_id}"))


async def get_many(r, job_ids):
    """Jobs in the given order, skipping expired ones (one round trip)."""
    async with r.pipeline(transaction=False) as p:
        for i in job_ids:
            p.hgetall(f"job:{i}")
        return [j for j in map(_load, await p.execute()) if j]


async def update(r, job_id, **fields):
    if "status" not in fields:
        await r.hset(f"job:{job_id}", mapping=_dump(fields))
        return
    async with r.pipeline(transaction=True) as p:
        p.hset(f"job:{job_id}", mapping=_dump(fields))
        _event(p, job_id, fields["status"], error=fields.get("error"))
        await p.execute()


async def finish(r, job, status, **fields):
    """Move a job to a terminal state and release its quota slot."""
    now = time.time()
    fields.update(status=status, finished_at=now)
    if status == DONE:
        fields["progress"] = 1
    hour = time.strftime("%Y%m%d%H", time.gmtime(now))
    async with r.pipeline(transaction=True) as p:
        p.hset(f"job:{job['id']}", mapping=_dump(fields))
        p.srem(f"active:{job['key_id']}", job["id"])
        p.zrem(f"running:{job['key_id']}", job["id"])
        p.incr(f"stats:{status}:{hour}")
        p.expire(f"stats:{status}:{hour}", 3 * 86400)
        _event(p, job["id"], status, job.get("key_id"), fields.get("error"))
        await p.execute()


async def find_idempotent(r, key_id, idempotency_key):
    return await r.get(f"idem:{key_id}:{idempotency_key}")


async def active_count(r, key_id):
    return await r.scard(f"active:{key_id}")


async def try_acquire_running(r, key_id, job_id, limit):
    """Reserve one of the key's running slots. Crash-safe: entries expire without heartbeat."""
    k = f"running:{key_id}"
    now = time.time()
    await r.zremrangebyscore(k, 0, now - RUNNING_STALE)
    await r.zadd(k, {job_id: now})
    rank = await r.zrank(k, job_id)
    if rank is not None and rank < limit:  # we were added last, so rank = jobs already running
        return True
    await r.zrem(k, job_id)
    return False


async def heartbeat(r, key_id, job_id):
    await r.zadd(f"running:{key_id}", {job_id: time.time()}, xx=True)


async def release_running(r, key_id, job_id):
    await r.zrem(f"running:{key_id}", job_id)


async def list_for_key(r, key_id, limit=50, offset=0):
    ids = await r.zrevrange(f"jobs:{key_id}", offset, offset + limit - 1)
    out = []
    for i in ids:
        j = await get(r, i)
        if j:
            out.append(j)
    return out


async def stats(r, hours=24):
    now = time.time()
    res = {}
    for ev in (DONE, FAILED, CANCELED):
        keys = [f"stats:{ev}:{time.strftime('%Y%m%d%H', time.gmtime(now - h * 3600))}" for h in range(hours)]
        vals = await r.mget(keys)
        res[f"{ev}_last_hour"] = int(vals[0] or 0)
        res[f"{ev}_last_{hours}h"] = sum(int(v or 0) for v in vals)
    return res
