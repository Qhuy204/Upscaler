import asyncio
import shutil

import httpx
import pytest
from arq import Retry

from conftest import FakeArq
from server import auth, jobs, retention, worker
from upscale_core import probe


@pytest.fixture
async def wctx(settings, redis, storage):
    settings.work_dir.mkdir(parents=True)
    ctx = {"s": settings, "r": redis, "storage": storage, "slots": asyncio.Semaphore(settings.ffmpeg_slots),
           "http": httpx.AsyncClient(), "redis": FakeArq()}
    _, rec = await auth.create_key(redis, "w")
    ctx["kid"] = rec["key_id"]
    yield ctx
    await ctx["http"].aclose()


async def _upload_job(ctx, video, options=None, callback_url=None):
    key = f"inputs/{ctx['kid']}/x.mp4"
    ctx["storage"].upload_file(video, key)
    return await jobs.create(ctx["r"], ctx["s"], ctx["kid"], "upload", key, options or {"quality": "delivery"},
                             callback_url, filename="promo.mp4")


async def test_process_upload_job(wctx, sample_video):
    job = await _upload_job(wctx, sample_video, callback_url="https://example.com/hook")
    assert await worker.process_job(wctx, job["id"]) == jobs.DONE
    j = await jobs.get(wctx["r"], job["id"])
    assert j["status"] == "done" and j["progress"] == 1 and j["attempts"] == 1
    assert j["input"]["width"] == 320 and j["output"]["width"] == 1920 and j["output"]["height"] == 1080
    assert j["output"]["filename"] == "promo_1080p.mp4"
    out = wctx["storage"].path(j["output"]["key"])
    info = probe(out)
    assert (info.width, info.height) == (1920, 1080) and info.has_audio
    assert not wctx["storage"].path(job["source"]).exists()        # input removed after success
    assert await wctx["r"].zscore(retention.EXPIRE_KEY, j["output"]["key"])  # output deleted after 24h
    assert not (wctx["s"].work_dir / job["id"]).exists()            # work dir cleaned
    assert await jobs.active_count(wctx["r"], wctx["kid"]) == 0
    assert wctx["redis"].calls[0][0] == "deliver_webhook"


async def test_not_a_video_fails_without_retry(wctx, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 100)
    job = await _upload_job(wctx, bad)
    assert await worker.process_job(wctx, job["id"]) == jobs.FAILED
    j = await jobs.get(wctx["r"], job["id"])
    assert "not a readable video" in j["error"] and j["attempts"] == 1


async def test_too_long_is_rejected(wctx, sample_video):
    wctx["s"].max_duration = 1
    job = await _upload_job(wctx, sample_video)
    assert await worker.process_job(wctx, job["id"]) == jobs.FAILED
    assert "limit is 1s" in (await jobs.get(wctx["r"], job["id"]))["error"]


async def test_missing_upload_fails(wctx):
    job = await jobs.create(wctx["r"], wctx["s"], wctx["kid"], "upload", "inputs/nope.mp4", {})
    assert await worker.process_job(wctx, job["id"]) == jobs.FAILED


async def test_transient_error_retries(wctx, sample_video, monkeypatch):
    job = await _upload_job(wctx, sample_video)

    def boom(*a, **k):
        raise OSError("disk hiccup")

    monkeypatch.setattr(wctx["storage"], "upload_file", boom)
    for attempt in (1, 2):
        with pytest.raises(Retry):
            await worker.process_job(wctx, job["id"])
        j = await jobs.get(wctx["r"], job["id"])
        assert j["status"] == "queued" and j["attempts"] == attempt
    assert await worker.process_job(wctx, job["id"]) == jobs.FAILED  # max_retries=2 exhausted


async def test_max_running_defers(wctx, sample_video):
    job = await _upload_job(wctx, sample_video)
    for other in ("a", "b"):  # default_max_running = 2
        assert await jobs.try_acquire_running(wctx["r"], wctx["kid"], other, 2)
    with pytest.raises(Retry):
        await worker.process_job(wctx, job["id"])
    assert (await jobs.get(wctx["r"], job["id"]))["status"] == "queued"


async def test_canceled_job_is_skipped(wctx, sample_video):
    job = await _upload_job(wctx, sample_video)
    await jobs.finish(wctx["r"], job, jobs.CANCELED)
    assert await worker.process_job(wctx, job["id"]) == "skipped"


async def test_cancel_while_processing(wctx, tmp_path):
    from upscale_core import FFMPEG
    import subprocess
    long = tmp_path / "long.mp4"
    subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=60",
                    "-c:v", "libx264", "-preset", "ultrafast", str(long)], check=True, capture_output=True)
    job = await _upload_job(wctx, long, {"quality": "master"})
    wctx["s"].encoder = "x264"
    task = asyncio.create_task(worker.process_job(wctx, job["id"]))
    for _ in range(100):
        await asyncio.sleep(0.2)
        if (await jobs.get(wctx["r"], job["id"]))["status"] == "processing":
            break
    await jobs.update(wctx["r"], job["id"], cancel_requested="1")
    assert await asyncio.wait_for(task, 20) == jobs.CANCELED
    assert not (wctx["s"].work_dir / job["id"]).exists()


async def test_webhook_delivery(wctx, sample_video, monkeypatch):
    job = await _upload_job(wctx, sample_video, callback_url="https://hooks.example.com/x")
    await worker.process_job(wctx, job["id"])
    seen = {}

    async def fake_send(url, secret, event, payload, client=None):
        seen.update(url=url, secret=secret, event=event, payload=payload)
        return True

    monkeypatch.setattr(worker.webhook, "send", fake_send)
    await worker.deliver_webhook(wctx, job["id"], 0)
    key = await auth.get_by_id(wctx["r"], wctx["kid"])
    assert seen["event"] == "job.done" and seen["secret"] == key["webhook_secret"]
    assert seen["payload"]["output"]["url"].startswith("http://testserver/files/outputs/")
    assert (await jobs.get(wctx["r"], job["id"]))["webhook_status"] == "delivered"


async def test_webhook_retry_schedules(wctx, sample_video, monkeypatch):
    job = await _upload_job(wctx, sample_video, callback_url="https://hooks.example.com/x")
    await jobs.finish(wctx["r"], job, jobs.FAILED, error="x")

    async def fail(*a, **k):
        return False

    monkeypatch.setattr(worker.webhook, "send", fail)
    await worker.deliver_webhook(wctx, job["id"], 0)
    name, args, kw = wctx["redis"].calls[-1]
    assert name == "deliver_webhook" and args == (job["id"], 1) and kw["_defer_by"] == 10
