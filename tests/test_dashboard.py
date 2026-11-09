import httpx
import pytest

from server import auth, jobs, registry, security, worker
from server.api import create_app
from server.dashboard import create_dashboard
from conftest import FakeArq


@pytest.fixture
async def dash(settings, redis):
    s = settings.model_copy(update={"admin_password": "pw-test"})
    app = create_dashboard(s, redis)
    app.state.r = redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dash",
                                 auth=("admin", "pw-test")) as c:
        yield c


async def test_requires_password(settings, redis):
    with pytest.raises(RuntimeError):
        create_dashboard(settings, redis)
    app = create_dashboard(settings.model_copy(update={"admin_password": "pw"}), redis)
    app.state.r = redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dash") as c:
        assert (await c.get("/api/overview")).status_code == 401
        assert (await c.get("/", auth=("admin", "nope"))).status_code == 401
        r = await c.get("/", auth=("admin", "pw"))
        assert r.status_code == 200 and "<title>Upscale Dashboard</title>" in r.text
        assert "script-src 'unsafe-inline'" in r.headers["content-security-policy"]


async def test_job_events_and_listing(dash, settings, redis):
    j1 = await jobs.create(redis, settings, "k1", "url", "https://x/a.mp4", {}, filename="a.mp4")
    j2 = await jobs.create(redis, settings, "k2", "url", "https://x/b.mp4", {}, filename="b.mp4")
    await jobs.update(redis, j1["id"], status=jobs.PROCESSING, worker="host-encode")
    await jobs.finish(redis, await jobs.get(redis, j1["id"]), jobs.DONE,
                      output={"width": 1920, "height": 1080, "fit": "blend"})

    d = (await dash.get("/api/jobs")).json()["jobs"]
    assert [j["id"] for j in d] == [j2["id"], j1["id"]]
    assert d[1]["out"] == "1920x1080" and d[1]["fit"] == "blend"
    assert [j["id"] for j in (await dash.get("/api/jobs?status=active")).json()["jobs"]] == [j2["id"]]
    assert [j["id"] for j in (await dash.get("/api/jobs?key_id=k1")).json()["jobs"]] == [j1["id"]]
    assert [j["id"] for j in (await dash.get("/api/jobs?q=B.MP4")).json()["jobs"]] == [j2["id"]]

    det = (await dash.get(f"/api/jobs/{j1['id']}")).json()
    assert [e["status"] for e in det["events"]] == ["queued", "processing", "done"]
    assert (await dash.get("/api/jobs/nope")).status_code == 404

    ov = (await dash.get("/api/overview")).json()
    assert ov["last_24h"]["total"] == 2 and ov["last_24h"]["by_status"] == {"done": 1, "queued": 1}


async def test_api_requests_are_logged(settings, redis, storage, dash):
    app = create_app(settings)
    app.state.r, app.state.arq, app.state.storage = redis, FakeArq(), storage
    key, rec = await auth.create_key(redis, "logged")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                 headers={"Authorization": f"Bearer {key}"}) as c:
        jid = (await c.post("/v1/jobs", json={"source_url": "https://example.com/a.mp4"})).json()["job_id"]
        await c.get(f"/v1/jobs/{jid}")
        await c.get("/v1/jobs/missing")
        await c.get("/healthz")
    log = [e for _, e in await redis.xrange(security.REQUEST_LOG)]
    assert [e["path"] for e in log] == ["/v1/jobs", f"/v1/jobs/{jid}", "/v1/jobs/missing"]
    assert all(e["key_id"] == rec["key_id"] for e in log)
    assert log[0]["job_id"] == jid and log[0]["status"] == "202"

    rows = (await dash.get("/api/requests?errors=true")).json()["requests"]
    assert [e["path"] for e in rows] == ["/v1/jobs/missing"]
    det = (await dash.get(f"/api/jobs/{jid}")).json()
    assert len(det["requests"]) == 2
    keys = (await dash.get("/api/keys")).json()["keys"]
    assert keys[0]["key_id"] == rec["key_id"] and keys[0]["unfinished"] == 1
    assert "webhook_secret" not in keys[0] and "hash" not in keys[0]


async def test_registry_and_webhook_routing(settings, redis):
    ctx = {"s": settings, "r": redis}
    assert await worker._webhook_queue(ctx) == settings.queue_name
    await registry.beat(redis, "h-aux", {"role": "aux", "slots": 20, "running": 0})
    await registry.beat(redis, "h-encode", {"role": "encode", "slots": 4, "running": 1})
    assert [w["name"] for w in await registry.alive(redis, "aux")] == ["h-aux"]
    assert await worker._webhook_queue(ctx) == settings.aux_queue_name
    await registry.leave(redis, "h-aux")
    assert await worker._webhook_queue(ctx) == settings.queue_name
    assert {w["name"] for w in await registry.recent(redis)} == {"h-encode"}
