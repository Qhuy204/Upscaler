"""Run a public Drive folder through the API and build a before/after review page.

python tools/drive_review.py <drive_folder_url> [--base https://upscale.qhuy204.id.vn] [--out output/drive_review]
         [--platform tiktok] [--quality delivery]

Creates a temporary API key (disabled afterwards), submits the folder as a batch, downloads the
originals and the outputs, grabs a frame from each and writes <out>/index.html.
"""
import argparse
import asyncio
import html
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402

from server import auth, fetch  # noqa: E402
from server.config import get_settings  # noqa: E402
from upscale_core import FFMPEG, probe  # noqa: E402


def frame(video, png, t):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=-2:540", str(png)], check=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--base", default="https://upscale.qhuy204.id.vn")
    ap.add_argument("--out", default=str(ROOT / "output" / "drive_review"))
    ap.add_argument("--platform", default="tiktok")
    ap.add_argument("--quality", default="delivery")
    a = ap.parse_args()
    out = Path(a.out)
    for d in ("src", "out", "img"):
        (out / d).mkdir(parents=True, exist_ok=True)

    s = get_settings()
    r = aioredis.from_url(s.redis_url, decode_responses=True)
    key, rec = await auth.create_key(r, "drive-review", max_queued=500, max_running=50)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(base_url=a.base, timeout=120, headers={"Authorization": f"Bearer {key}"}) as c, \
                httpx.AsyncClient(timeout=120) as plain:
            b = await c.post("/v1/batches", json={"drive_folder_url": a.folder,
                                                  "options": {"platform": a.platform, "quality": a.quality}})
            b.raise_for_status()
            jobs = b.json()["jobs"]
            print(f"submitted {len(jobs)} jobs in {time.time() - t0:.1f}s", flush=True)

            async def get_src(j):
                dest = out / "src" / j["filename"]
                if not dest.exists():
                    await fetch.download(j["source_url"], dest, 2**30) if j.get("source_url") else None
            srcs = asyncio.gather(*(get_src(j) for j in jobs), return_exceptions=True)

            pending, done = {j["job_id"]: j for j in jobs}, {}
            while pending and time.time() - t0 < 1800:
                await asyncio.sleep(2)
                for jid in list(pending):
                    try:
                        resp = await c.get(f"/v1/jobs/{jid}")
                        j = resp.json()
                    except (httpx.HTTPError, ValueError):
                        print(f"  poll error ({getattr(resp, 'status_code', '?')}), retrying", flush=True)
                        continue
                    if j["status"] in ("done", "failed", "canceled"):
                        pending.pop(jid)
                        done[jid] = j
                        print(f"  {j['status']:6} {time.time() - t0:6.1f}s {j.get('filename')} {j.get('error') or ''}",
                              flush=True)
            total = time.time() - t0
            for jid, j in done.items():
                if j["status"] == "done":
                    dest = out / "out" / Path(j["filename"]).with_suffix(".mp4").name
                    dest.write_bytes((await plain.get(j["output"]["url"])).content)
                    j["_out"] = dest
            await srcs
    finally:
        await auth.set_fields(r, rec["key_id"], disabled="1")
        await r.aclose()

    rows = []
    for j in sorted(done.values(), key=lambda j: j.get("filename") or ""):
        name = j.get("filename") or j["job_id"]
        src = out / "src" / name
        i, o = j.get("input") or {}, j.get("output") or {}
        row = {"name": name, "status": j["status"], "error": j.get("error"),
               "in": f"{i.get('width')}x{i.get('height')}" if i.get("width") else "?",
               "out": f"{o.get('width')}x{o.get('height')}" if o.get("width") else "",
               "fit": o.get("fit"), "kbps": o.get("kbps"), "mb_in": src.stat().st_size / 1e6 if src.exists() else None,
               "mb_out": (o.get("bytes") or 0) / 1e6, "src": src if src.exists() else None, "outp": j.get("_out")}
        if row["outp"]:
            t = min(3.0, probe(row["outp"]).duration / 2)
            stem = Path(name).stem
            if row["src"]:
                frame(row["src"], out / "img" / f"{stem}_before.png", t)
                row["before"] = f"img/{stem}_before.png"
            frame(row["outp"], out / "img" / f"{stem}_after.png", t)
            row["after"] = f"img/{stem}_after.png"
        rows.append(row)

    ok = sum(r_["status"] == "done" for r_ in rows)
    e = html.escape
    cards = []
    for x in rows:
        imgs = "".join(f'<figure><img src="{e(x[k])}" loading="lazy"><figcaption>{lbl}</figcaption></figure>'
                       for k, lbl in (("before", f"Gốc {x['in']}"), ("after", f"Sau {x['out']} · {x['fit']}"))
                       if x.get(k))
        vids = "".join(f'<a href="{d}/{e(p.name)}">{lbl}</a> '
                       for d, p, lbl in (("src", x["src"], "video gốc"), ("out", x["outp"], "video sau"))
                       if p)
        meta = (f"{x['in']} → {x['out'] or '–'} · fit {x['fit'] or '–'} · {x['kbps'] or '–'} kbps · "
                f"{x['mb_in'] or 0:.1f} → {x['mb_out']:.1f} MB")
        err = f'<p class="err">{e(x["error"])}</p>' if x["error"] else ""
        cards.append(f'<section><h2>{e(x["name"])} <span class="{x["status"]}">{x["status"]}</span></h2>'
                     f'<p class="m">{e(meta)}</p>{err}<div class="pair">{imgs}</div><p>{vids}</p></section>')
    (out / "index.html").write_text(f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Drive Review</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7280;--line:#e5e7eb;--ok:#15803d;--bad:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1218;--card:#171b23;--fg:#e5e7eb;--mut:#9aa3b2;--line:#273041;--ok:#4ade80;--bad:#f87171}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px system-ui,sans-serif}}
section{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:0 auto 12px;max-width:1300px}}
h1{{max-width:1300px;margin:0 auto 12px}}h2{{font-size:15px;margin:0 0 4px;word-break:break-all}}.m{{color:var(--mut);margin:0 0 8px}}
.pair{{display:flex;gap:10px;flex-wrap:wrap}}figure{{margin:0;flex:1 1 300px}}img{{width:100%;border-radius:6px;background:#000;border:1px solid var(--line)}}
figcaption{{color:var(--mut);font-size:12px}}.done{{color:var(--ok)}}.failed,.err{{color:var(--bad)}}a{{color:inherit}}
</style></head><body><h1>Drive review: {ok}/{len(rows)} done in {total:.0f}s (platform {e(a.platform)})</h1>
{''.join(cards)}</body></html>""", encoding="utf-8")
    (out / "summary.json").write_text(json.dumps([{k: v for k, v in x.items() if not isinstance(v, Path)}
                                                  for x in rows], indent=1), encoding="utf-8")
    print(f"{ok}/{len(rows)} done in {total:.1f}s -> {out / 'index.html'}")


if __name__ == "__main__":
    asyncio.run(main())
