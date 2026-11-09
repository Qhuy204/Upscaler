"""End-to-end check against a running API + worker.

python tools/e2e.py --base http://127.0.0.1:8080 --key upk_... --secret whsec_... [--drive-file URL] [--drive-folder URL]

Sends ads from input/ via multipart and via presigned upload, plus optional Drive links, waits for all
jobs, downloads every output, checks resolution/audio, and checks webhook signatures (needs
ALLOW_PRIVATE_URLS=true on the server because the receiver listens on 127.0.0.1).
"""
import argparse
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.webhook import verify  # noqa: E402
from upscale_core import probe  # noqa: E402

hooks = []


def receiver(port, secret):
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            ok = verify(secret, self.headers["X-Upscale-Timestamp"], body, self.headers["X-Upscale-Signature"])
            hooks.append((json.loads(body)["job_id"], self.headers["X-Upscale-Event"], ok))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}/hook"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("--key", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--drive-file")
    ap.add_argument("--drive-folder")
    ap.add_argument("--quality", default="delivery")
    a = ap.parse_args()

    hook = receiver(9099, a.secret)
    c = httpx.Client(base_url=a.base, headers={"Authorization": f"Bearer {a.key}"}, timeout=120)
    ads = sorted((ROOT / "input").glob("ad*.mp4"))
    opts = json.dumps({"quality": a.quality})
    job_ids = {}
    t0 = time.time()

    for p in ads[:2]:
        with open(p, "rb") as f:
            r = c.post("/v1/jobs", files={"file": (p.name, f, "video/mp4")},
                       data={"options": opts, "callback_url": hook})
        r.raise_for_status()
        job_ids[r.json()["job_id"]] = f"multipart {p.name}"
    for p in ads[2:4]:
        up = c.post("/v1/uploads", json={"filename": p.name, "content_type": "video/mp4"}).json()
        httpx.put(up["upload_url"], content=p.read_bytes(), headers=up["headers"], timeout=120).raise_for_status()
        r = c.post("/v1/jobs", json={"upload_key": up["upload_key"], "options": json.loads(opts),
                                     "callback_url": hook})
        r.raise_for_status()
        job_ids[r.json()["job_id"]] = f"presigned {p.name}"
    if a.drive_file:
        r = c.post("/v1/jobs", json={"source_url": a.drive_file, "options": json.loads(opts), "callback_url": hook})
        r.raise_for_status()
        job_ids[r.json()["job_id"]] = "drive file"
    if a.drive_folder:
        r = c.post("/v1/batches", json={"drive_folder_url": a.drive_folder, "options": json.loads(opts),
                                        "callback_url": hook})
        r.raise_for_status()
        for j in r.json()["jobs"]:
            job_ids[j["job_id"]] = f"drive folder: {j['filename']}"
    print(f"submitted {len(job_ids)} jobs in {time.time() - t0:.1f}s")

    pending, results = set(job_ids), {}
    while pending and time.time() - t0 < 900:
        time.sleep(1)
        for jid in list(pending):
            j = c.get(f"/v1/jobs/{jid}").json()
            if j["status"] in ("done", "failed", "canceled"):
                pending.discard(jid)
                results[jid] = j
                print(f"  {j['status']:6} {time.time() - t0:6.1f}s  {job_ids[jid]}  {j.get('error') or ''}")
    total = time.time() - t0

    ok = True
    out_dir = Path(tempfile.mkdtemp(prefix="e2e_"))
    for jid, j in results.items():
        if j["status"] != "done":
            ok = False
            continue
        dest = out_dir / f"{jid}.mp4"
        dest.write_bytes(httpx.get(j["output"]["url"], timeout=120).content)
        info = probe(dest)
        good = (info.width, info.height) == (j["output"]["width"], j["output"]["height"]) and \
            min(info.width, info.height) == 1080 and info.has_audio == j["input"]["has_audio"]
        ok &= good
        t = j["output"].get("timing") or {}
        print(f"  {'OK ' if good else 'BAD'} {job_ids[jid]:45} {j['input']['width']}x{j['input']['height']} -> "
              f"{info.width}x{info.height} audio={info.has_audio} {j['output']['bytes'] / 1e6:.1f} MB "
              f"{j['output']['encoder']}")
    time.sleep(3)
    signed = {jid for jid, ev, good in hooks if good}
    print(f"webhooks: {len(hooks)} received, {len(signed)} valid signatures, "
          f"missing {len(set(job_ids) - signed)}")
    ok &= signed >= set(job_ids) and not pending
    print(f"{'PASS' if ok else 'FAIL'}: {len(results)}/{len(job_ids)} finished in {total:.1f}s; outputs in {out_dir}")
    print(json.dumps(c.get("/v1/stats").json()))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
