"""Admin dashboard: jobs, their status history, API request log, workers, keys.

Read-only, behind HTTP basic auth (ADMIN_USER / ADMIN_PASSWORD). Bound to 127.0.0.1 by default;
do not route it through the public tunnel without Cloudflare Access in front.
Run: python -m server.run_dashboard  (or the `dashboard` service in deploy/docker-compose.yml)
"""
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import auth, jobs, registry, security
from .config import Settings, get_settings

PAGE = Path(__file__).with_name("dashboard.html")
KEY_FIELDS = ("key_id", "name", "created_at", "disabled", "max_queued", "max_running", "rate_limit", "allowed_ips")
SCAN_CHUNK = 500


def create_dashboard(settings: Settings | None = None, r=None):
    s = settings or get_settings()
    if not s.admin_password:
        raise RuntimeError("set ADMIN_PASSWORD in .env to run the dashboard")

    @asynccontextmanager
    async def lifespan(app):
        app.state.r = r or aioredis.from_url(s.redis_url, decode_responses=True)
        yield
        if r is None:
            await app.state.r.aclose()

    app = FastAPI(title="Upscale dashboard", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in security.HEADERS.items():
            resp.headers.setdefault(k, v)
        resp.headers["Cache-Control"] = "no-store"
        # The page is one self-contained file with inline script/style; data goes in via textContent.
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; script-src 'unsafe-inline'; "
                                                   "style-src 'unsafe-inline'; connect-src 'self'; "
                                                   "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        return resp

    async def admin(request: Request, cred: HTTPBasicCredentials | None = Depends(HTTPBasic(auto_error=False))):
        rr = request.app.state.r
        ip = security.client_ip(request)
        await security.check_banned(rr, ip)
        ok = cred is not None and secrets.compare_digest(cred.username.encode(), s.admin_user.encode()) \
            and secrets.compare_digest(cred.password.encode(), s.admin_password.encode())
        if not ok:
            if cred is not None:
                await security.auth_failed(rr, s, ip, "dashboard")
            raise HTTPException(401, "login required", headers={"WWW-Authenticate": 'Basic realm="upscale admin"'})

    def R(request):
        return request.app.state.r

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
    async def page():
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/overview", dependencies=[Depends(admin)])
    async def overview(request: Request):
        rr = R(request)
        now = time.time()
        workers = await registry.alive(rr)
        recent = await _recent_jobs(rr, now - 86400, 5000)
        by_status = {}
        for j in recent:
            by_status[j["status"]] = by_status.get(j["status"], 0) + 1
        done = [j for j in recent if j["status"] == jobs.DONE and j.get("started_at") and j.get("finished_at")]
        timing = {}
        for k in ("download", "encode", "upload"):
            v = [(j.get("output") or {}).get("timing", {}).get(k) for j in done]
            v = [x for x in v if x is not None]
            timing[k] = round(sum(v) / len(v), 2) if v else None
        waits = [j["started_at"] - j["created_at"] for j in done]
        return {"time": now,
                "queues": {s.queue_name: await rr.zcard(s.queue_name),
                           s.aux_queue_name: await rr.zcard(s.aux_queue_name)},
                "workers": {"encode": sum(w.get("role") == "encode" for w in workers),
                            "aux": sum(w.get("role") == "aux" for w in workers),
                            "slots": sum(int(w.get("slots") or 0) for w in workers),
                            "running": sum(int(w.get("running") or 0) for w in workers)},
                "last_24h": {"total": len(recent), "by_status": by_status, "avg_timing_s": timing,
                             "avg_wait_s": round(sum(waits) / len(waits), 2) if waits else None},
                **(await jobs.stats(rr)),
                "media_tracked": await rr.zcard("media:expire")}

    @app.get("/api/jobs", dependencies=[Depends(admin)])
    async def list_jobs(request: Request, status: str = "", key_id: str = "", q: str = "",
                        limit: int = 100, before: float | None = None):
        """Newest first. status may be a comma list or 'active' (= not finished)."""
        rr = R(request)
        limit = max(1, min(limit, 500))
        want = set(status.split(",")) - {""}
        if "active" in want:
            want = (want - {"active"}) | {jobs.QUEUED, jobs.DOWNLOADING, jobs.PROCESSING, jobs.UPLOADING}
        q = q.lower()
        out, hi, scanned = [], "+inf" if before is None else f"({before}", 0
        while len(out) < limit and scanned < 20_000:
            ids = await rr.zrevrangebyscore(jobs.ALL, hi, "-inf", start=0, num=SCAN_CHUNK, withscores=True)
            if not ids:
                break
            scanned += len(ids)
            hi = f"({ids[-1][1]}"
            for j in await jobs.get_many(rr, [jid for jid, _ in ids]):
                jid = j["id"]
                if (want and j["status"] not in want) or (key_id and j["key_id"] != key_id):
                    continue
                if q and q not in f"{jid} {j.get('filename') or ''} {j.get('source') or ''}".lower():
                    continue
                out.append(_row(j))
                if len(out) >= limit:
                    break
        return {"jobs": out, "next_before": out[-1]["created_at"] if len(out) >= limit else None}

    @app.get("/api/jobs/{job_id}", dependencies=[Depends(admin)])
    async def job_detail(job_id: str, request: Request):
        rr = R(request)
        j = await jobs.get(rr, job_id)
        if not j:
            raise HTTPException(404, "job not found")
        events = [e for _, e in await rr.xrevrange(jobs.EVENTS, count=20_000) if e.get("job_id") == job_id]
        reqs = [e for _, e in await rr.xrevrange(security.REQUEST_LOG, count=20_000) if e.get("job_id") == job_id]
        return {"job": j, "events": events[::-1], "requests": reqs[::-1]}

    @app.get("/api/events", dependencies=[Depends(admin)])
    async def events(request: Request, limit: int = 200, status: str = ""):
        rows = await R(request).xrevrange(jobs.EVENTS, count=max(1, min(limit, 2000)) * (5 if status else 1))
        rows = [{"id": i, **e} for i, e in rows if not status or e.get("status") == status]
        return {"events": rows[:limit]}

    @app.get("/api/requests", dependencies=[Depends(admin)])
    async def requests_log(request: Request, limit: int = 200, key_id: str = "", ip: str = "", path: str = "",
                           errors: bool = False):
        limit = max(1, min(limit, 2000))
        filtered = key_id or ip or path or errors
        rows = await R(request).xrevrange(security.REQUEST_LOG, count=limit * 20 if filtered else limit)
        out = []
        for i, e in rows:
            if key_id and e.get("key_id") != key_id or ip and e.get("ip") != ip:
                continue
            if path and path not in e.get("path", "") or errors and int(e.get("status", 0)) < 400:
                continue
            out.append({"id": i, **e})
            if len(out) >= limit:
                break
        return {"requests": out}

    @app.get("/api/workers", dependencies=[Depends(admin)])
    async def workers(request: Request):
        return {"workers": await registry.recent(R(request))}

    @app.get("/api/keys", dependencies=[Depends(admin)])
    async def keys(request: Request):
        rr = R(request)
        out = []
        for k in await auth.list_keys(rr):
            row = {f: k.get(f) for f in KEY_FIELDS}
            mq, mr = auth.limits(k, s)
            row.update(max_queued=mq, max_running=mr, unfinished=await jobs.active_count(rr, k["key_id"]),
                       running=await rr.zcard(f"running:{k['key_id']}"))
            out.append(row)
        return {"keys": out}

    return app


def _row(j):
    o, i = j.get("output") or {}, j.get("input") or {}
    return {"id": j["id"], "key_id": j["key_id"], "status": j["status"], "progress": j.get("progress"),
            "filename": j.get("filename"), "source_type": j.get("source_type"),
            "created_at": j.get("created_at"), "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"), "attempts": j.get("attempts"), "worker": j.get("worker"),
            "error": j.get("error"), "in": f"{i['width']}x{i['height']}" if i.get("width") else None,
            "out": f"{o['width']}x{o['height']}" if o.get("width") else None, "fit": o.get("fit"),
            "platform": (j.get("options") or {}).get("platform"), "timing": o.get("timing"),
            "bytes": o.get("bytes"), "webhook": j.get("webhook_status")}


async def _recent_jobs(r, since, cap):
    return await jobs.get_many(r, await r.zrevrangebyscore(jobs.ALL, "+inf", since, start=0, num=cap))
