import time

import httpx
import pytest

from server import auth, jobs, retention, security
from server.api import create_app
from server.views import public_job
from conftest import FakeArq


@pytest.fixture
async def ctx(settings, redis, storage):
    settings.auth_fail_limit, settings.rate_limit_per_min, settings.max_json_kb = 3, 5, 1
    app = create_app(settings)
    app.state.r, app.state.arq, app.state.storage = redis, FakeArq(), storage
    (settings.work_dir / "uploads").mkdir(parents=True)
    key, rec = await auth.create_key(redis, "test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                               headers={"Authorization": f"Bearer {key}"})
    async with client:
        yield {"c": client, "key": key, "rec": rec, "r": redis, "s": settings}


async def test_docs_disabled_and_headers(ctx):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await ctx["c"].get(path)).status_code == 404
    r = await ctx["c"].get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert "strict-transport-security" in r.headers


async def test_bruteforce_lockout_per_ip(ctx):
    bad = {"Authorization": "Bearer upk_guess", "CF-Connecting-IP": "203.0.113.9"}
    for _ in range(3):
        assert (await ctx["c"].get("/v1/jobs", headers=bad)).status_code == 401
    r = await ctx["c"].get("/v1/jobs", headers=bad)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # even a valid key is refused from the banned IP, other IPs are unaffected
    hdr = {"CF-Connecting-IP": "203.0.113.9"}
    assert (await ctx["c"].get("/v1/jobs", headers=hdr)).status_code == 429
    assert (await ctx["c"].get("/v1/jobs", headers={"CF-Connecting-IP": "198.51.100.1"})).status_code == 200


async def test_rate_limit_per_key(ctx):
    codes = [(await ctx["c"].get("/v1/jobs")).status_code for _ in range(6)]
    assert codes == [200] * 5 + [429]
    await auth.set_fields(ctx["r"], ctx["rec"]["key_id"], rate_limit=100)
    assert (await ctx["c"].get("/v1/jobs")).status_code == 200


async def test_ip_allowlist(ctx):
    await auth.set_fields(ctx["r"], ctx["rec"]["key_id"], allowed_ips=security.parse_networks("10.1.0.0/16, 1.2.3.4"))
    assert (await ctx["c"].get("/v1/jobs", headers={"CF-Connecting-IP": "10.1.5.6"})).status_code == 200
    assert (await ctx["c"].get("/v1/jobs", headers={"CF-Connecting-IP": "1.2.3.4"})).status_code == 200
    assert (await ctx["c"].get("/v1/jobs", headers={"CF-Connecting-IP": "8.8.8.8"})).status_code == 403
    with pytest.raises(ValueError):
        security.parse_networks("not-an-ip")


async def test_json_body_limit(ctx):
    r = await ctx["c"].post("/v1/jobs", json={"source_url": "https://example.com/" + "a" * 2000})
    assert r.status_code == 413


async def test_upload_key_is_tracked_for_deletion(ctx):
    t = time.time()
    up = (await ctx["c"].post("/v1/uploads", json={"filename": "a.mp4"})).json()
    score = await ctx["r"].zscore(retention.EXPIRE_KEY, up["upload_key"])
    assert t + ctx["s"].media_ttl - 5 < score < time.time() + ctx["s"].media_ttl + 5


async def test_sweep_deletes_due_objects(settings, redis, storage, tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"x")
    for k in ("outputs/k/old.mp4", "outputs/k/new.mp4"):
        storage.upload_file(src, k)
    now = time.time()
    await retention.track(redis, "outputs/k/old.mp4", 10, now - 20)
    await retention.track(redis, "outputs/k/new.mp4", 3600, now)
    await retention.track(redis, "inputs/k/gone.mp4", 10, now - 20)  # already deleted: still fine
    stale = settings.work_dir / "uploads" / "orphan.mp4"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x")
    import os
    os.utime(stale, (now - settings.media_ttl - 60,) * 2)
    assert await retention.sweep(redis, storage, settings, now) == 3
    assert not storage.path("outputs/k/old.mp4").exists() and storage.path("outputs/k/new.mp4").exists()
    assert not stale.exists()
    assert await redis.zrange(retention.EXPIRE_KEY, 0, -1) == ["outputs/k/new.mp4"]


def test_view_hides_expired_output(settings, storage):
    now = time.time()
    job = {"id": "j1", "status": jobs.DONE, "source_type": "url", "source": "https://x/a.mp4",
           "output": {"key": "outputs/k/j1.mp4", "bytes": 1, "expires_at": now + 100}}
    out = public_job(job, storage, settings)["output"]
    assert out["url"] and out["expires_in"] <= 100       # link never outlives the file
    job["output"]["expires_at"] = now - 1
    out = public_job(job, storage, settings)["output"]
    assert out["url"] is None and out["expired"] is True
