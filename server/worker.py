"""arq workers, two roles (python -m server.run_worker --role encode|aux):

encode  queue upscale:queue  download -> probe -> ffmpeg (limited slots) -> upload. Needs ffmpeg/GPU.
aux     queue upscale:aux    webhooks + storage retention sweep. Light; runs in Docker.

Only the ffmpeg step holds one of FFMPEG_SLOTS; downloads and uploads of other jobs overlap it
(WORKER_MAX_JOBS jobs are held at once). Each API key may hold at most max_running of them; extra
jobs are deferred a few seconds so other keys' jobs get picked meanwhile. Webhooks go to the aux
queue when an aux worker is alive, else they run on the encode worker as before.
"""
import asyncio
import contextlib
import logging
import os
import random
import shutil
import socket
import threading
import time
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from arq import Retry, cron
from arq.connections import RedisSettings
from arq.worker import func

from upscale_core import Canceled, EncodeError, ProbeError, make_plan, probe, resolve_encoder, upscale

from . import auth, jobs, registry, retention, webhook
from .config import get_settings
from .fetch import FetchError, TransientFetchError, download
from .storage import NotFound, TooLarge, make_storage
from .views import output_filename, public_job

log = logging.getLogger("upscale.worker")
MAX_TRIES = 100_000  # arq tries; real retries are capped by settings.max_retries via job.attempts


class InputError(Exception):
    """The input itself is unusable. Fail without retrying."""


PERMANENT = (InputError, FetchError, ProbeError, EncodeError, TooLarge, NotFound)


def worker_name(s, role):
    return s.worker_name or f"{socket.gethostname()}-{role}-{os.getpid()}"


async def _register(ctx):
    s, info = ctx["s"], ctx["info"]
    while True:
        try:
            await registry.beat(ctx["r"], info["name"], {**info, "running": len(ctx["running"]),
                                                         "done": ctx["count"]["done"],
                                                         "failed": ctx["count"]["failed"]})
        except Exception:
            log.warning("registry heartbeat failed", exc_info=True)
        await asyncio.sleep(registry.INTERVAL)


def _startup(role):
    async def startup(ctx):
        s = get_settings()
        ctx["s"] = s
        ctx["role"] = role
        ctx["queue"] = s.queue_name if role == "encode" else s.aux_queue_name
        ctx["r"] = aioredis.from_url(s.redis_url, decode_responses=True)
        ctx["storage"] = make_storage(s)
        ctx["slots"] = asyncio.Semaphore(s.ffmpeg_slots)
        ctx["http"] = httpx.AsyncClient(timeout=15, follow_redirects=False)
        ctx["running"], ctx["count"] = set(), {"done": 0, "failed": 0}
        # "auto" is resolved once here (test encodes on NVIDIA / AMD / Intel), not per job
        ctx["encoder"] = await asyncio.to_thread(resolve_encoder, s.encoder) if role == "encode" else ""
        name = jobs.WORKER = jobs.WORKER or worker_name(s, role)
        ctx["info"] = {"name": name, "role": role, "host": socket.gethostname(), "pid": os.getpid(),
                       "queue": ctx["queue"], "slots": s.ffmpeg_slots if role == "encode" else 0,
                       "encoder": ctx["encoder"], "started_at": f"{time.time():.0f}"}
        ctx["reg"] = asyncio.create_task(_register(ctx))
        if role == "encode":
            Path(s.work_dir).mkdir(parents=True, exist_ok=True)
        log.info("%s worker %s up: queue=%s slots=%s threads/job=%s max_jobs=%s storage=%s encoder=%s",
                 role, name, ctx["queue"], s.ffmpeg_slots, s.threads_per_job, s.worker_max_jobs,
                 s.storage_backend, f"{ctx['encoder']} ({s.encoder})" if role == "encode" else "-")
    return startup


startup = _startup("encode")


async def shutdown(ctx):
    # startup may not have run (e.g. Redis unreachable), so close only what exists
    if "reg" in ctx:
        ctx["reg"].cancel()
        with contextlib.suppress(Exception):
            await registry.leave(ctx["r"], ctx["info"]["name"])
    if "http" in ctx:
        await ctx["http"].aclose()
    if "r" in ctx:
        await ctx["r"].aclose()


async def _in_thread(stop, fn, *args, **kwargs):
    """Run blocking fn in a thread; if we get cancelled, set stop and wait so files are released."""
    fut = asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except BaseException:
        stop.set()
        with contextlib.suppress(BaseException):
            await fut
        raise


async def _heartbeat(r, job, cancel, state):
    n, last = 0, None
    while True:
        await asyncio.sleep(1)
        n += 1
        try:
            if await r.hget(f"job:{job['id']}", "cancel_requested") == "1":
                cancel.set()
            p = state.get("progress")
            if p is not None and p != last:
                last = p
                await jobs.update(r, job["id"], progress=round(p, 3))
            if n % 5 == 0:
                await jobs.heartbeat(r, job["key_id"], job["id"])
        except Exception:  # redis blip: keep the job running
            log.warning("heartbeat failed for %s", job["id"], exc_info=True)


async def _get_input(ctx, job, work, cancel):
    s, st = ctx["s"], ctx["storage"]
    max_bytes = s.max_input_mb * 2**20
    kind, source = job["source_type"], job["source"]
    if kind == "url":
        f = await download(source, work / "input", max_bytes, s.download_timeout, cancel)
        if not job.get("filename") and f.filename:
            job["filename"] = f.filename
            await jobs.update(ctx["r"], job["id"], filename=f.filename)
        return f.path, f.size
    if kind == "upload":
        dest = work / "input"
        try:
            await asyncio.to_thread(st.download_file, source, dest, max_bytes)
        except NotFound:
            raise InputError("upload_key not found; upload the file before creating the job") from None
        except TooLarge:
            raise InputError(f"input is larger than {s.max_input_mb} MB") from None
        return dest, dest.stat().st_size
    if kind == "local":
        p = Path(source)
        if not p.exists():
            raise InputError("uploaded file is no longer available")
        return p, p.stat().st_size
    raise InputError(f"unknown source type {kind}")


async def _pipeline(ctx, job, work, cancel, state):
    s, r, st = ctx["s"], ctx["r"], ctx["storage"]
    o = job.get("options") or {}
    await jobs.update(r, job["id"], status=jobs.DOWNLOADING, progress=0)
    t0 = time.time()
    src, in_bytes = await _get_input(ctx, job, work, cancel)
    t_dl = time.time() - t0

    try:
        info = await asyncio.to_thread(probe, src)
    except ProbeError:
        raise InputError("input is not a readable video") from None
    if info.duration > s.max_duration:
        raise InputError(f"video is {info.duration:.0f}s long; limit is {s.max_duration:.0f}s")
    if max(info.width, info.height) > s.max_side:
        raise InputError(f"input is {info.width}x{info.height}; limit is {s.max_side}px per side")
    plan = await asyncio.to_thread(make_plan, src, info, o.get("to"), o.get("scale"), o.get("short") or 1080,
                                   o.get("fit", "auto"), o.get("snap_aspect", True), o.get("remove_borders", True),
                                   o.get("platform", "tiktok"), o.get("aspect"))
    tw, th = plan.tw, plan.th
    if max(tw, th) > s.max_side or min(tw, th) < 16:
        raise InputError(f"target {tw}x{th} is out of range")
    inp = {"bytes": in_bytes, "width": info.width, "height": info.height, "duration": info.duration,
           "fps": info.fps, "has_audio": info.has_audio,
           "borders_removed": list(plan.crop) if plan.crop else None}
    await jobs.update(r, job["id"], status=jobs.PROCESSING, input=inp)

    out = work / "output.mp4"
    async with ctx["slots"]:
        if cancel.is_set():
            raise Canceled()
        t1 = time.time()
        res = await _in_thread(
            cancel, upscale, src, out, o.get("to"), o.get("scale"), o.get("short") or 1080,
            o.get("strength", 1.0), ctx.get("encoder") or s.encoder, o.get("quality", "delivery"), o.get("crf"), s.threads_per_job,
            on_progress=lambda p: state.__setitem__("progress", p), cancel=cancel, info=info, plan=plan,
            platform=o.get("platform", "tiktok"))
        t_enc = time.time() - t1

    state["progress"] = None
    await jobs.update(r, job["id"], status=jobs.UPLOADING, progress=1)
    key = f"outputs/{job['key_id']}/{job['id']}.mp4"
    t2 = time.time()
    # track before uploading so a crash mid-upload still gets swept
    await retention.track(r, key, s.media_ttl, t2)
    await asyncio.to_thread(st.upload_file, out, key)
    output = {"key": key, "bytes": out.stat().st_size, "width": tw, "height": th, "encoder": res["encoder"],
              "fit": plan.fit, "kbps": res.get("kbps"),
              "filename": output_filename(job, min(tw, th)), "expires_at": t2 + s.media_ttl,
              "timing": {"download": round(t_dl, 2), "encode": round(t_enc, 2), "upload": round(time.time() - t2, 2)}}
    return {"output": output, "error": None}


async def _after_terminal(ctx, job):
    """Cleanup of inputs that only matter while the job may still run, then webhook."""
    s, st = ctx["s"], ctx["storage"]
    if job["source_type"] == "local":
        Path(job["source"]).unlink(missing_ok=True)
    elif job["source_type"] == "upload":
        with contextlib.suppress(Exception):
            await asyncio.to_thread(st.delete, job["source"])
            await ctx["r"].zrem(retention.EXPIRE_KEY, job["source"])
    if job.get("callback_url"):
        await ctx["redis"].enqueue_job("deliver_webhook", job["id"], 0, _queue_name=await _webhook_queue(ctx))


async def _webhook_queue(ctx):
    s = ctx["s"]
    with contextlib.suppress(Exception):
        if await registry.alive(ctx["r"], "aux"):
            return s.aux_queue_name
    return s.queue_name


async def process_job(ctx, job_id):
    s, r = ctx["s"], ctx["r"]
    job = await jobs.get(r, job_id)
    if not job or job["status"] in jobs.TERMINAL:
        return "skipped"
    key = await auth.get_by_id(r, job["key_id"]) or {}
    _, max_running = auth.limits(key, s)
    if not await jobs.try_acquire_running(r, job["key_id"], job_id, max_running):
        raise Retry(defer=random.uniform(2, 5))

    cancel, state = threading.Event(), {"progress": None}
    running = ctx.setdefault("running", set())
    running.add(job_id)
    hb = asyncio.create_task(_heartbeat(r, job, cancel, state))
    work = Path(s.work_dir) / job_id
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    attempts = job["attempts"] + 1
    await jobs.update(r, job_id, attempts=attempts, started_at=time.time(), error="", worker=jobs.WORKER)
    outcome, retry_in = None, None
    try:
        try:
            async with asyncio.timeout(s.job_timeout):
                fields = await _pipeline(ctx, job, work, cancel, state)
            outcome = (jobs.DONE, fields)
        except asyncio.CancelledError:
            # worker shutting down: put the job back, arq re-runs it after restart
            if await r.hget(f"job:{job_id}", "cancel_requested") != "1":
                await jobs.update(r, job_id, status=jobs.QUEUED, progress=0, attempts=attempts - 1)
                raise
            outcome = (jobs.CANCELED, {"error": "canceled"})
        except Exception as e:
            if await r.hget(f"job:{job_id}", "cancel_requested") == "1" or isinstance(e, Canceled):
                outcome = (jobs.CANCELED, {"error": "canceled"})
            elif isinstance(e, TimeoutError):
                outcome = (jobs.FAILED, {"error": f"job exceeded {s.job_timeout}s"})
            elif isinstance(e, PERMANENT):
                msg = str(e) if not isinstance(e, EncodeError) else "ffmpeg failed: " + str(e)[-400:]
                outcome = (jobs.FAILED, {"error": msg})
            elif attempts <= s.max_retries:
                retry_in = 10 * attempts
                log.warning("job %s attempt %s failed, retry in %ss: %r", job_id, attempts, retry_in, e)
                await jobs.update(r, job_id, status=jobs.QUEUED, progress=0, error=f"retrying: {e!r}"[:500])
            else:
                log.exception("job %s failed", job_id)
                outcome = (jobs.FAILED, {"error": (f"{e.__class__.__name__}: {e}"
                                                   if not isinstance(e, TransientFetchError) else str(e))[:500]})
    finally:
        hb.cancel()
        running.discard(job_id)
        with contextlib.suppress(BaseException):
            await hb
        await jobs.release_running(r, job["key_id"], job_id)
        shutil.rmtree(work, ignore_errors=True)

    if retry_in is not None:
        raise Retry(defer=retry_in)
    status, fields = outcome
    await jobs.finish(r, job, status, **fields)
    count = ctx.setdefault("count", {"done": 0, "failed": 0})
    if status in count:
        count[status] += 1
    log.info("job %s %s %s", job_id, status, fields.get("error") or "")
    await _after_terminal(ctx, job)
    return status


async def deliver_webhook(ctx, job_id, attempt):
    s, r = ctx["s"], ctx["r"]
    job = await jobs.get(r, job_id)
    if not job or not job.get("callback_url"):
        return
    key = await auth.get_by_id(r, job["key_id"]) or {}
    payload = public_job(job, ctx["storage"], s)
    try:
        ok = await webhook.send(job["callback_url"], key.get("webhook_secret", ""), f"job.{job['status']}",
                                payload, ctx["http"])
    except FetchError as e:
        await jobs.update(r, job_id, webhook_status=f"not sent: {e}")
        return
    if ok:
        await jobs.update(r, job_id, webhook_status="delivered")
    elif attempt < len(webhook.BACKOFF):
        await jobs.update(r, job_id, webhook_status=f"retrying (attempt {attempt + 1} failed)")
        await ctx["redis"].enqueue_job("deliver_webhook", job_id, attempt + 1,
                                       _defer_by=webhook.BACKOFF[attempt],
                                       _queue_name=ctx.get("queue") or s.queue_name)
    else:
        await jobs.update(r, job_id, webhook_status=f"failed after {attempt + 1} attempts")


async def sweep_media(ctx):
    """aux: delete expired objects from storage."""
    return await retention.sweep_objects(ctx["r"], ctx["storage"])


async def sweep_encode_host(ctx):
    """encode: stale local uploads; also storage objects while no aux worker runs."""
    n = await retention.sweep_local(ctx["s"])
    if not await registry.alive(ctx["r"], "aux"):
        n += await retention.sweep_objects(ctx["r"], ctx["storage"])
    return n


_s = get_settings()
_every5 = set(range(0, 60, 5))


class EncodeWorker:
    # deliver_webhook stays here for webhooks queued before the aux split / while no aux worker runs
    functions = [func(process_job, max_tries=MAX_TRIES, timeout=_s.job_timeout + 120),
                 func(deliver_webhook, max_tries=3, timeout=60)]
    cron_jobs = [cron(sweep_encode_host, minute=_every5, run_at_startup=True, timeout=600)]
    on_startup = _startup("encode")
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_s.redis_url)
    queue_name = _s.queue_name
    max_jobs = _s.worker_max_jobs
    keep_result = 0
    poll_delay = 0.5
    health_check_interval = 30


class AuxWorker:
    functions = [func(deliver_webhook, max_tries=3, timeout=60)]
    cron_jobs = [cron(sweep_media, minute=_every5, run_at_startup=True, timeout=600)]
    on_startup = _startup("aux")
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(_s.redis_url)
    queue_name = _s.aux_queue_name
    max_jobs = 20
    keep_result = 0
    poll_delay = 0.5
    health_check_interval = 30


WorkerSettings = EncodeWorker
ROLES = {"encode": EncodeWorker, "aux": AuxWorker}
