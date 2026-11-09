"""HTTP API. Run: python -m server.run_api (or uvicorn server.api:app)."""
import asyncio
import contextlib
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from . import auth, jobs, retention, security
from .config import Settings, get_settings
from .fetch import FetchError, check_url, drive_folder_id, list_drive_folder
from .models import BatchCreate, JobCreate, Options, UploadCreate
from .storage import LocalStorage, make_storage, verify
from .views import public_job

bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None, r=None, arq=None, storage=None):
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.r = r or aioredis.from_url(s.redis_url, decode_responses=True)
        app.state.arq = arq or await create_pool(RedisSettings.from_dsn(s.redis_url),
                                                 default_queue_name=s.queue_name)
        app.state.storage = storage or make_storage(s)
        (Path(s.work_dir) / "uploads").mkdir(parents=True, exist_ok=True)
        yield
        if r is None:
            await app.state.r.aclose()
        if arq is None:
            await app.state.arq.aclose()

    # no /docs, /redoc or /openapi.json on the public host
    app = FastAPI(title="Upscale API", version="1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.s = s
    json_limit = s.max_json_kb * 1024

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        t0 = time.perf_counter()
        status = 500
        try:
            resp = await call_next(request)
            status = resp.status_code
            for k, v in security.HEADERS.items():
                resp.headers.setdefault(k, v)
            return resp
        finally:
            if request.url.path != "/healthz":
                await _log_request(request, status, time.perf_counter() - t0)

    async def _log_request(request, status, dt):
        st = request.state
        e = {"t": f"{time.time():.3f}", "ip": security.client_ip(request), "method": request.method,
             "path": request.url.path[:200], "status": str(status), "ms": str(round(dt * 1000))}
        for k in ("key_id", "job_id", "jobs"):
            if v := getattr(st, k, None):
                e[k] = str(v)
        if ua := request.headers.get("user-agent"):
            e["ua"] = ua[:120]
        with contextlib.suppress(Exception):  # logging must never break a request
            await request.app.state.r.xadd(security.REQUEST_LOG, e, maxlen=security.REQUEST_LOG_MAX,
                                           approximate=True)

    async def api_key(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(bearer)):
        rr = request.app.state.r
        ip = security.client_ip(request)
        await security.check_banned(rr, ip)
        rec = await auth.lookup(rr, cred.credentials if cred else None)
        if not rec:
            await security.auth_failed(rr, s, ip, request.url.path)
            raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
        if not security.ip_allowed(rec, ip):
            await security.auth_failed(rr, s, ip, request.url.path)
            raise HTTPException(403, "this API key is not allowed from your IP")
        request.state.key_id = rec["key_id"]
        await security.rate_limit(rr, s, rec)
        return rec

    def bad_request(e: ValidationError):
        raise HTTPException(422, jsonable_encoder(e.errors(include_url=False, include_context=False)))

    async def check_quota(request, key, n=1):
        max_queued, _ = auth.limits(key, s)
        active = await jobs.active_count(request.app.state.r, key["key_id"])
        if active + n > max_queued:
            raise HTTPException(429, f"too many unfinished jobs ({active}/{max_queued}); wait for some to finish",
                                headers={"Retry-After": "30"})

    async def check_source_url(url):
        if drive_folder_id(url):
            raise HTTPException(422, "source_url is a Google Drive folder; use POST /v1/batches")
        try:
            await check_url(url)
        except FetchError as e:
            raise HTTPException(422, f"source_url rejected: {e}") from None

    async def check_callback(url):
        if url:
            try:
                await check_url(url)
            except FetchError as e:
                raise HTTPException(422, f"callback_url rejected: {e}") from None

    async def enqueue(request, kid, source_type, source, options, callback_url, idem, filename=None, job_id=None):
        rr = request.app.state.r
        if idem:
            old = await jobs.find_idempotent(rr, kid, idem)
            if old and (job := await jobs.get(rr, old)):
                return job, False
        job = await jobs.create(rr, s, kid, source_type, source, options, callback_url, idem, filename, job_id)
        await request.app.state.arq.enqueue_job("process_job", job["id"], _job_id=job["id"],
                                                _queue_name=s.queue_name)
        return job, True

    def view(request, job, status_code=200):
        request.state.job_id = job["id"]
        return JSONResponse(public_job(job, request.app.state.storage, s), status_code=status_code)

    # ---------- health / stats ----------

    @app.get("/healthz")
    async def healthz(request: Request):
        rr = request.app.state.r
        try:
            await rr.ping()
        except Exception:
            return JSONResponse({"ok": False, "redis": False}, status_code=503)
        worker = await rr.get(s.queue_name + ":health-check")
        return {"ok": True, "redis": True, "worker": worker is not None, "time": time.time()}

    @app.get("/v1/stats")
    async def stats(request: Request, key=Depends(api_key)):
        rr = request.app.state.r
        running = 0
        async for k in rr.scan_iter("running:*"):
            running += await rr.zcount(k, time.time() - jobs.RUNNING_STALE, "+inf")
        max_queued, max_running = auth.limits(key, s)
        return {"queue_depth": await rr.zcard(s.queue_name), "running": running,
                "ffmpeg_slots": s.ffmpeg_slots, **(await jobs.stats(rr)),
                "you": {"unfinished": await jobs.active_count(rr, key["key_id"]),
                        "max_queued": max_queued, "max_running": max_running}}

    # ---------- uploads (presigned PUT) ----------

    @app.post("/v1/uploads")
    async def create_upload(request: Request, key=Depends(api_key)):
        raw = await security.read_limited(request, json_limit)
        try:
            body = UploadCreate.model_validate_json(raw or b"{}")
        except ValidationError as e:
            bad_request(e)
        ext = Path(body.filename or "").suffix.lower()
        ext = ext if ext.isascii() and len(ext) <= 6 and ext[1:].isalnum() else ""
        upload_key = f"inputs/{key['key_id']}/{secrets.token_hex(12)}{ext}"
        await retention.track(request.app.state.r, upload_key, s.media_ttl)
        await request.app.state.r.set(f"upload:{upload_key}", key["key_id"], ex=s.upload_url_ttl + s.job_ttl)
        if body.filename:
            await request.app.state.r.set(f"upload_name:{upload_key}", body.filename, ex=s.upload_url_ttl + s.job_ttl)
        url = await asyncio.to_thread(request.app.state.storage.presign_put, upload_key, s.upload_url_ttl,
                                      body.content_type)
        headers = {"Content-Type": body.content_type} if body.content_type else {}
        return {"upload_key": upload_key, "upload_url": url, "method": "PUT", "headers": headers,
                "expires_in": s.upload_url_ttl, "max_bytes": s.max_input_mb * 2**20}

    @app.put("/v1/uploads/put/{key:path}")
    async def local_put(key: str, request: Request, exp: str = "", sig: str = ""):
        st = request.app.state.storage
        if not isinstance(st, LocalStorage):
            raise HTTPException(404)
        if not verify(s.signing_secret, "PUT", key, exp, sig):
            raise HTTPException(403, "bad or expired signature")
        limit = s.max_input_mb * 2**20
        dest = st.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"file is larger than {s.max_input_mb} MB")
                    f.write(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
        return {"ok": True, "bytes": size}

    @app.get("/files/{key:path}")
    async def local_get(key: str, request: Request, exp: str = "", sig: str = "", name: str | None = None):
        st = request.app.state.storage
        if not isinstance(st, LocalStorage) or not verify(s.signing_secret, "GET", key, exp, sig):
            raise HTTPException(404)
        p = st.path(key)
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="video/mp4", filename=name)

    # ---------- jobs ----------

    @app.post("/v1/jobs", status_code=202)
    async def create_job(request: Request, key=Depends(api_key)):
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            return await create_job_multipart(request, key)
        try:
            body = JobCreate.model_validate_json(await security.read_limited(request, json_limit))
        except ValidationError as e:
            bad_request(e)
        kid = key["key_id"]
        await check_callback(body.callback_url)
        filename = None
        if body.source_url:
            await check_source_url(body.source_url)
            kind, source = "url", body.source_url
        else:
            owner = await request.app.state.r.get(f"upload:{body.upload_key}")
            if owner != kid:
                raise HTTPException(404, "upload_key not found")
            kind, source = "upload", body.upload_key
            filename = await request.app.state.r.get(f"upload_name:{body.upload_key}")
        if not (body.idempotency_key and await jobs.find_idempotent(request.app.state.r, kid, body.idempotency_key)):
            await check_quota(request, key)
        job, created = await enqueue(request, kid, kind, source, body.options.model_dump(exclude_none=True),
                                     body.callback_url, body.idempotency_key, filename)
        return view(request, job, 202 if created else 200)

    async def create_job_multipart(request, key):
        cl = request.headers.get("content-length")
        limit = s.max_upload_mb * 2**20
        if cl and cl.isdigit() and int(cl) > limit + 2**20:
            raise HTTPException(413, f"multipart upload is limited to {s.max_upload_mb} MB; use POST /v1/uploads")
        form = await request.form(max_files=1, max_fields=10)
        try:
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                raise HTTPException(422, "multipart field 'file' is required")
            try:
                options = Options.model_validate_json(form.get("options") or "{}")
            except ValidationError as e:
                bad_request(e)
            callback_url = form.get("callback_url") or None
            idem = form.get("idempotency_key") or None
            await check_callback(callback_url)
            kid = key["key_id"]
            if idem and (old := await jobs.find_idempotent(request.app.state.r, kid, idem)):
                if job := await jobs.get(request.app.state.r, old):
                    return view(request, job, 200)
            await check_quota(request, key)
            job_id = jobs.new_id()
            ext = Path(upload.filename or "").suffix.lower()
            ext = ext if ext.isascii() and len(ext) <= 6 and ext[1:].isalnum() else ""
            dest = Path(s.work_dir) / "uploads" / f"{job_id}{ext}"
            size = await asyncio.to_thread(_copy_limited, upload.file, dest, limit)
            if size is None:
                raise HTTPException(413, f"multipart upload is limited to {s.max_upload_mb} MB; use POST /v1/uploads")
            kind, source = "local", str(dest)
            if s.multipart_via_storage:
                # API in a container: the host encode worker cannot read our disk, so hand over via storage
                source = f"inputs/{kid}/{secrets.token_hex(12)}{ext}"
                await retention.track(request.app.state.r, source, s.media_ttl)
                try:
                    await asyncio.to_thread(request.app.state.storage.upload_file, dest, source,
                                            upload.content_type or "application/octet-stream")
                finally:
                    dest.unlink(missing_ok=True)
                kind = "upload"
            job, _ = await enqueue(request, kid, kind, source, options.model_dump(exclude_none=True),
                                   callback_url, idem, upload.filename, job_id)
            return view(request, job, 202)
        finally:
            await form.close()

    @app.post("/v1/batches", status_code=202)
    async def create_batch(request: Request, key=Depends(api_key)):
        try:
            body = BatchCreate.model_validate_json(await security.read_limited(request, json_limit))
        except ValidationError as e:
            bad_request(e)
        await check_callback(body.callback_url)
        if body.drive_folder_url:
            fid = drive_folder_id(body.drive_folder_url)
            if not fid:
                raise HTTPException(422, "drive_folder_url is not a Google Drive folder link")
            try:
                async with httpx.AsyncClient(timeout=30) as c:
                    files = await list_drive_folder(c, fid)
            except FetchError as e:
                raise HTTPException(422, str(e)) from None
            except httpx.HTTPError as e:
                raise HTTPException(502, f"could not list the Drive folder: {e.__class__.__name__}") from None
            items = [(f["url"], f["name"], f["id"]) for f in files]
        else:
            items = [(u, None, str(i)) for i, u in enumerate(body.source_urls)]
            for u, _, _ in items:
                await check_source_url(u)
        if not items:
            raise HTTPException(422, "no videos found")
        if len(items) > s.max_batch:
            raise HTTPException(422, f"batch has {len(items)} videos; limit is {s.max_batch}")
        await check_quota(request, key, len(items))
        out = []
        for url, name, item_id in items:
            idem = f"{body.idempotency_key}:{item_id}" if body.idempotency_key else None
            job, _ = await enqueue(request, key["key_id"], "url", url, body.options.model_dump(exclude_none=True),
                                   body.callback_url, idem, name)
            out.append(public_job(job, request.app.state.storage, s))
        request.state.jobs = len(out)
        return {"count": len(out), "jobs": out}

    @app.get("/v1/jobs")
    async def list_jobs(request: Request, limit: int = 50, offset: int = 0, key=Depends(api_key)):
        limit = max(1, min(limit, 200))
        items = await jobs.list_for_key(request.app.state.r, key["key_id"], limit, max(0, offset))
        return {"jobs": [public_job(j, request.app.state.storage, s) for j in items]}

    async def owned_job(request, job_id, key):
        job = await jobs.get(request.app.state.r, job_id)
        if not job or job["key_id"] != key["key_id"]:
            raise HTTPException(404, "job not found")
        return job

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request, key=Depends(api_key)):
        return view(request, await owned_job(request, job_id, key))

    @app.delete("/v1/jobs/{job_id}", status_code=202)
    async def cancel_job(job_id: str, request: Request, key=Depends(api_key)):
        rr = request.app.state.r
        job = await owned_job(request, job_id, key)
        if job["status"] in jobs.TERMINAL:
            raise HTTPException(409, f"job is already {job['status']}")
        await jobs.update(rr, job_id, cancel_requested="1")
        if job["status"] == jobs.QUEUED:
            await jobs.finish(rr, job, jobs.CANCELED, error="canceled")
            if job["source_type"] == "local":
                with contextlib.suppress(OSError):
                    Path(job["source"]).unlink(missing_ok=True)
        return view(request, await jobs.get(rr, job_id), 202)

    return app


def _copy_limited(src, dest, limit):
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(dest, "wb") as f:
        while chunk := src.read(1 << 20):
            size += len(chunk)
            if size > limit:
                break
            f.write(chunk)
    if size > limit:
        dest.unlink(missing_ok=True)
        return None
    return size


app = create_app()
