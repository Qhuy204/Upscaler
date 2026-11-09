import json

import httpx
import pytest

from server import auth, jobs
from server.api import create_app
from conftest import FakeArq


@pytest.fixture
async def ctx(settings, redis, storage):
    app = create_app(settings)
    app.state.r, app.state.arq, app.state.storage = redis, FakeArq(), storage
    (settings.work_dir / "uploads").mkdir(parents=True)
    key, rec = await auth.create_key(redis, "test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                               headers={"Authorization": f"Bearer {key}"})
    async with client:
        yield {"app": app, "c": client, "key": key, "rec": rec, "r": redis}


async def test_requires_key(ctx):
    r = await ctx["c"].get("/v1/jobs", headers={"Authorization": "Bearer upk_wrong"})
    assert r.status_code == 401
    r = await ctx["c"].get("/v1/jobs", headers={"Authorization": ""})
    assert r.status_code == 401


async def test_healthz(ctx):
    r = await ctx["c"].get("/healthz")
    assert r.json()["ok"] is True


async def test_create_url_job_and_get(ctx):
    r = await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/a.mp4",
                                              "options": {"quality": "master", "short": 720}})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "queued" and job["options"] == {"short": 720, "strength": 1.0, "quality": "master",
                                                                 "fit": "auto", "snap_aspect": True,
                                                                 "platform": "tiktok",
                                                                 "remove_borders": True}
    name, args, kw = ctx["app"].state.arq.calls[0]
    assert name == "process_job" and args == (job["job_id"],) and kw["_job_id"] == job["job_id"]
    r = await ctx["c"].get(f"/v1/jobs/{job['job_id']}")
    assert r.status_code == 200 and r.json()["source_url"] == "https://example.com/a.mp4"


async def test_rejects_private_url_and_bad_options(ctx):
    r = await ctx["c"].post("/v1/jobs", json={"source_url": "http://127.0.0.1:6379/"})
    assert r.status_code == 422 and "rejected" in r.text
    r = await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/a.mp4",
                                              "options": {"short": 720, "to": "1920x1080"}})
    assert r.status_code == 422
    r = await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/a.mp4", "upload_key": "x"})
    assert r.status_code == 422
    r = await ctx["c"].post("/v1/jobs", json={
        "source_url": "https://drive.google.com/drive/folders/11QmYP3gE7KX_Ni3jsPGs7-GyCsSOI7ad"})
    assert r.status_code == 422 and "batches" in r.text
    for opts, ok in (({"aspect": "9:16"}, True), ({"aspect": "4:5"}, False), ({"aspect": "4:5", "platform": "meta"}, True),
                     ({"aspect": "1:1", "to": "1080x1080"}, False), ({"aspect": "9-16"}, False),
                     ({"aspect": "1:9", "platform": "any"}, False)):
        r = await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/a.mp4", "options": opts})
        assert (r.status_code == 202) == ok, (opts, r.text)


async def test_idempotency(ctx):
    body = {"source_url": "https://example.com/a.mp4", "idempotency_key": "abc"}
    a = await ctx["c"].post("/v1/jobs", json=body)
    b = await ctx["c"].post("/v1/jobs", json=body)
    assert a.status_code == 202 and b.status_code == 200 and a.json()["job_id"] == b.json()["job_id"]
    assert len(ctx["app"].state.arq.calls) == 1


async def test_quota_429(ctx):
    for i in range(5):
        assert (await ctx["c"].post("/v1/jobs", json={"source_url": f"https://example.com/{i}.mp4"})).status_code == 202
    r = await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/x.mp4"})
    assert r.status_code == 429 and r.headers["retry-after"]


async def test_other_key_cannot_see_job(ctx):
    job = (await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/a.mp4"})).json()
    other, _ = await auth.create_key(ctx["r"], "other")
    r = await ctx["c"].get(f"/v1/jobs/{job['job_id']}", headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404


async def test_cancel_queued(ctx):
    job = (await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/a.mp4"})).json()
    r = await ctx["c"].delete(f"/v1/jobs/{job['job_id']}")
    assert r.status_code == 202 and r.json()["status"] == "canceled"
    assert await jobs.active_count(ctx["r"], ctx["rec"]["key_id"]) == 0
    assert (await ctx["c"].delete(f"/v1/jobs/{job['job_id']}")).status_code == 409


async def test_multipart_job(ctx, sample_video, settings):
    with open(sample_video, "rb") as f:
        r = await ctx["c"].post("/v1/jobs", files={"file": ("ad.mp4", f, "video/mp4")},
                                data={"options": json.dumps({"quality": "delivery"})})
    assert r.status_code == 202, r.text
    job = await jobs.get(ctx["r"], r.json()["job_id"])
    assert job["source_type"] == "local" and job["filename"] == "ad.mp4"
    assert (settings.work_dir / "uploads" / f"{job['id']}.mp4").stat().st_size == sample_video.stat().st_size


async def test_presigned_upload_flow(ctx, sample_video):
    up = (await ctx["c"].post("/v1/uploads", json={"filename": "big.mp4", "content_type": "video/mp4"})).json()
    assert up["upload_key"].endswith(".mp4") and up["method"] == "PUT"
    path = up["upload_url"].removeprefix("http://testserver")
    r = await ctx["c"].put(path, content=sample_video.read_bytes())
    assert r.status_code == 200 and r.json()["bytes"] == sample_video.stat().st_size
    bad = await ctx["c"].put(path.replace("sig=", "sig=0"), content=b"x")
    assert bad.status_code == 403
    r = await ctx["c"].post("/v1/jobs", json={"upload_key": up["upload_key"]})
    assert r.status_code == 202 and r.json()["upload_key"] == up["upload_key"] and r.json()["filename"] == "big.mp4"
    other, _ = await auth.create_key(ctx["r"], "other")
    r = await ctx["c"].post("/v1/jobs", json={"upload_key": up["upload_key"]},
                            headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 404


async def test_batch_urls(ctx):
    r = await ctx["c"].post("/v1/batches", json={"source_urls": ["https://example.com/1.mp4",
                                                                 "https://example.com/2.mp4"]})
    assert r.status_code == 202 and r.json()["count"] == 2
    r = await ctx["c"].post("/v1/batches", json={"source_urls": [f"https://example.com/{i}.mp4" for i in range(4)]})
    assert r.status_code == 429  # 2 + 4 > max_queued 5
