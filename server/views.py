import time
from pathlib import Path

from . import jobs, retention


def output_filename(job, height):
    stem = Path(job.get("filename") or job["id"]).stem[:100] or job["id"]
    return f"{stem}_{height}p.mp4"


def public_job(job, storage, s):
    """Job as returned by GET /v1/jobs/{id} and sent in webhooks."""
    d = {k: job.get(k) for k in ("status", "progress", "created_at", "started_at", "finished_at",
                                 "attempts", "error", "filename", "options", "input")}
    d = {"job_id": job["id"], "source_type": job.get("source_type"), **d}
    if job.get("source_type") == "url":
        d["source_url"] = job.get("source")
    elif job.get("source_type") == "upload":
        d["upload_key"] = job.get("source")
    out = job.get("output")
    if job.get("status") == jobs.DONE and out and out.get("key"):
        d["output"] = {k: out.get(k) for k in ("bytes", "width", "height", "encoder", "fit", "kbps", "filename")}
        exp = retention.expires_at(job, s)
        left = int(exp - time.time())
        d["output"]["deleted_at"] = exp
        if left > 0:
            ttl = min(s.output_url_ttl, left)
            d["output"]["url"] = storage.presign_get(out["key"], ttl, out.get("filename"))
            d["output"]["expires_in"] = ttl
        else:
            d["output"]["url"] = None
            d["output"]["expired"] = True
    if job.get("webhook_status"):
        d["webhook_status"] = job["webhook_status"]
    return d
