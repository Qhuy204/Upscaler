"""Upscale low-res video to 1080p using only ffmpeg filters.

Chain: deblock -> hqdn3d -> lanczos scale + pad -> cas -> encode (NVENC or libx264).
Usage: python upscale.py in.mp4 [--short 1080 | --to WxH | --scale N] [--strength 1.0]
                         [--quality master|delivery] [--crf N] [--cpu] [--threads N] [-o out.mp4]
                         [--platform tiktok|meta|any] [--fit auto|pad|crop|stretch] [--no-snap] [--keep-borders]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from upscale_core import FFMPEG, EncodeError, ProbeError, probe, upscale  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("-o", "--out")
    ap.add_argument("--to")
    ap.add_argument("--scale", type=float)
    ap.add_argument("--short", type=int, default=1080)
    ap.add_argument("--strength", type=float, default=1.0)
    ap.add_argument("--quality", choices=["master", "delivery"], default="master")
    ap.add_argument("--crf", type=int, help="override cq/crf of the quality preset")
    ap.add_argument("--cpu", action="store_true", help="same as --encoder x264")
    ap.add_argument("--encoder", choices=["auto", "nvenc", "amf", "qsv", "x264"], default="auto",
                    help="auto picks the first working GPU (NVIDIA, AMD, Intel), else CPU")
    ap.add_argument("--threads", type=int)
    ap.add_argument("--aspect", help="force the output aspect, e.g. 9:16 (default: nearest the platform allows)")
    ap.add_argument("--fit", choices=["auto", "pad", "crop", "stretch"], default="auto")
    ap.add_argument("--platform", choices=["tiktok", "meta", "any"], default="tiktok",
                    help="force an aspect the platform accepts (tiktok: 9:16, 16:9, 1:1)")
    ap.add_argument("--no-snap", action="store_true", help="keep the exact input aspect (no 1920x1033 -> 1920x1080)")
    ap.add_argument("--keep-borders", action="store_true", help="do not cut black bars baked into the input")
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args()

    src = Path(a.input)
    out = Path(a.out) if a.out else src.with_name(src.stem + "_up.mp4")
    try:
        info = probe(src)
    except ProbeError as e:
        sys.exit(f"cannot read video: {e}")

    t0 = time.time()
    try:
        r = upscale(src, out, a.to, a.scale, a.short, a.strength, "x264" if a.cpu else a.encoder,
                    a.quality, a.crf, a.threads, info=info, fit=a.fit, snap=not a.no_snap,
                    remove_borders=not a.keep_borders, platform=a.platform, aspect=a.aspect)
    except EncodeError as e:
        sys.exit(str(e))
    dt = time.time() - t0
    (w, h), (tw, th), plan = r["in_size"], r["out_size"], r["plan"]
    borders = f" borders cut to {plan.width}x{plan.height}" if plan.crop else ""
    print(f"{w}x{h} -> {tw}x{th} fit={plan.fit}{borders} [{r['encoder']}]\nvf: {r['vf']}")
    print(f"done in {dt:.1f}s -> {out} ({out.stat().st_size / 1e6:.1f} MB)")

    if a.compare:
        t = info.duration / 2 if info.duration else 1
        base = out.with_suffix("")
        subprocess.run([FFMPEG, "-hide_banner", "-y", "-ss", str(t), "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={tw}:{th}:flags=bicubic", f"{base}_before.png"], capture_output=True)
        subprocess.run([FFMPEG, "-hide_banner", "-y", "-ss", str(t), "-i", str(out), "-frames:v", "1",
                        f"{base}_after.png"], capture_output=True)
        print(f"compare: {base}_before.png / {base}_after.png")


if __name__ == "__main__":
    main()
