"""Media retention: every object put in storage is deleted MEDIA_TTL seconds after it was created.

media:expire   zset  storage key -> unix time it must be gone by

The API adds upload keys when it hands out a presigned PUT, the worker adds output keys after
uploading. sweep() runs from the worker's cron every few minutes. The R2 lifecycle rule
(tools/setup_r2.py, 1 day on inputs/ and outputs/) is only a backstop for anything missed.
"""
import asyncio
import logging
import time
from pathlib import Path

log = logging.getLogger("upscale.retention")
EXPIRE_KEY = "media:expire"


async def track(r, key, ttl, now=None):
    await r.zadd(EXPIRE_KEY, {key: (now or time.time()) + ttl})


def expires_at(job, s):
    """When the job's output is (or was) deleted."""
    out = job.get("output") or {}
    if out.get("expires_at"):
        return float(out["expires_at"])
    return float(job.get("finished_at") or job.get("created_at") or time.time()) + s.media_ttl


async def sweep(r, storage, s, now=None, batch=200):
    """Delete every tracked object whose time is up, plus stale local scratch files. Returns count."""
    now = now or time.time()
    return await sweep_objects(r, storage, now, batch) + await sweep_local(s, now)


async def sweep_local(s, now=None):
    """Multipart uploads kept on the encode host's disk (work_dir/uploads) older than media_ttl."""
    now = now or time.time()
    n = await asyncio.to_thread(_sweep_dir, Path(s.work_dir) / "uploads", now - s.media_ttl)
    if n:
        log.info("retention: deleted %s stale local uploads", n)
    return n


async def sweep_objects(r, storage, now=None, batch=200):
    now = now or time.time()
    deleted = 0
    while keys := await r.zrangebyscore(EXPIRE_KEY, "-inf", now, start=0, num=batch):
        for key in keys:
            try:
                await asyncio.to_thread(storage.delete, key)
            except Exception:  # keep it in the set; next sweep retries
                log.warning("could not delete %s", key, exc_info=True)
                continue
            await r.zrem(EXPIRE_KEY, key)
            deleted += 1
        if len(keys) < batch:
            break
    if deleted:
        log.info("retention: deleted %s expired objects", deleted)
    return deleted


def _sweep_dir(d, cutoff):
    n = 0
    if d.is_dir():
        for p in d.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    return n
