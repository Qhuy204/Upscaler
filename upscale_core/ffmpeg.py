"""ffmpeg-only upscale: deblock -> hqdn3d -> lanczos scale + pad -> cas -> GPU H.264 (NVENC / AMF / QSV) or libx264.

Shared by the CLI (scripts/upscale.py) and the API worker (server/worker.py).
"""
import math
import re
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProbeError(Exception):
    """Input is not a usable video (bad file, no video stream)."""


class EncodeError(Exception):
    pass


class Canceled(Exception):
    pass


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool


def probe(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)],
                       capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    err = r.stderr
    video = [l for l in err.splitlines() if "Video:" in l and "attached pic" not in l]
    m = re.search(r"Video:.*?,\s(\d{2,5})x(\d{2,5})", video[0]) if video else None
    if not m:
        raise ProbeError("no video stream found")
    w, h = int(m.group(1)), int(m.group(2))
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?) degrees", err)
    if rot and abs(round(float(rot.group(1)))) % 180 == 90:
        w, h = h, w  # ffmpeg autorotates, so the output frame is swapped
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    duration = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else 0.0
    f = re.search(r"(\d+(?:\.\d+)?) fps", video[0])
    return VideoInfo(w, h, duration, float(f.group(1)) if f else 0.0, "Audio:" in err)


def even(n):
    return int(round(n / 2)) * 2


# aspect ratios an output snaps to when the input is close (e.g. 1920x1033 -> 16:9 -> 1920x1080)
STANDARD_ASPECTS = [(16, 9), (9, 16), (1, 1), (4, 5), (5, 4), (4, 3), (3, 4), (3, 2), (2, 3), (21, 9)]
SNAP_TOLERANCE = 0.08  # max relative aspect difference that still snaps (platform "any")
FILL_TOLERANCE = 0.12  # fit=auto fills the frame without bars up to this gap (~6% stretch + ~6% crop)
FITS = ("auto", "pad", "crop", "stretch")
# aspect ratios each ad platform accepts; the output is always forced to the nearest one
PLATFORMS = {
    "tiktok": [(9, 16), (16, 9), (1, 1)],               # TikTok non-Spark ads: only these three
    "meta": [(9, 16), (4, 5), (1, 1), (16, 9)],
    "any": None,                                        # keep the input aspect unless it is near-standard
}
MIN_KBPS = {"tiktok": 600}  # TikTok rejects < 516 kbps; very static ads can encode below that


def _aspect_gap(a, b):
    return abs(math.log(a / b))


def nearest_aspect(w, h, aspects):
    """Closest of aspects to w/h; on a tie keep the input's orientation (4:3 -> 16:9, not 1:1)."""
    r = w / h
    return min(aspects, key=lambda a: (round(_aspect_gap(r, a[0] / a[1]), 6), (a[0] > a[1]) != (w > h)))


def snap_aspect(w, h, tol=SNAP_TOLERANCE):
    """Closest standard aspect (as (num, den)) within tol of w/h, or None."""
    best = nearest_aspect(w, h, STANDARD_ASPECTS)
    return best if _aspect_gap(w / h, best[0] / best[1]) <= math.log1p(tol) else None


def parse_aspect(aspect):
    """'9:16' -> (9, 16), reduced (18:32 -> 9:16)."""
    an, ad = map(int, aspect.split(":"))
    g = math.gcd(an, ad)
    return an // g, ad // g


def target_size(w, h, to=None, scale=None, short=1080, snap=True, platform="any", aspect=None):
    """aspect ('9:16'): force that frame instead of the nearest one; must be allowed by the platform."""
    if to:
        tw, th = map(int, to.lower().split("x"))
        return even(tw), even(th)
    allowed = PLATFORMS.get(platform)
    if aspect:
        a = parse_aspect(aspect)
        if allowed and a not in allowed:
            raise ValueError(f"aspect {aspect} is not accepted by {platform} "
                             f"({', '.join(f'{x}:{y}' for x, y in allowed)})")
    elif allowed:
        a = nearest_aspect(w, h, allowed)
    else:
        a = snap_aspect(w, h) if snap else None
    if scale:
        if not a:
            return even(w * scale), even(h * scale)
        short = min(w, h) * scale
    if a:  # standard frame with that short side: 16:9 -> 1920x1080, 4:5 -> 1080x1350
        an, ad = a
        return (even(short * an / ad), even(short)) if an >= ad else (even(short), even(short * ad / an))
    k = short / min(w, h)
    return even(w * k), even(h * k)


def detect_borders(src, info, limit=24):
    """Black bars baked into the video, as (w, h, x, y) of the picture, or None.

    Runs cropdetect on keyframes only (fast) and keeps the union of all boxes, so a dark scene
    is never mistaken for a border.
    """
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-skip_frame", "nokey", "-i", str(src),
                        "-map", "0:V:0", "-vf", f"cropdetect=limit={limit}:round=2:reset=1", "-frames:v", "200",
                        "-f", "null", "-"], capture_output=True, text=True, errors="replace",
                       creationflags=NO_WINDOW)
    boxes = [tuple(map(int, m)) for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)]
    if not boxes:
        return None
    x1 = min(b[2] for b in boxes)
    y1 = min(b[3] for b in boxes)
    x2 = max(b[2] + b[0] for b in boxes)
    y2 = max(b[3] + b[1] for b in boxes)
    cw, ch = min(x2, info.width) - x1, min(y2, info.height) - y1
    # ignore tiny trims and anything that would remove more than 40% of a side (probably wrong)
    if cw < 16 or ch < 16 or (cw >= info.width - 4 and ch >= info.height - 4):
        return None
    if cw < info.width * 0.6 or ch < info.height * 0.6:
        return None
    return cw - cw % 2, ch - ch % 2, x1, y1


def resolve_fit(w, h, tw, th, fit="auto"):
    """auto: fill the frame without bars when aspects are within FILL_TOLERANCE, else letterbox."""
    if fit != "auto":
        return fit
    return "blend" if _aspect_gap(w / h, tw / th) <= math.log1p(FILL_TOLERANCE) else "pad"


def resize_filter(w, h, tw, th, fit):
    if fit == "stretch":
        return f"scale={tw}:{th}:flags=lanczos,setsar=1"
    if fit in ("crop", "blend"):
        # blend: stretch halfway to the target aspect, crop the rest, so neither exceeds ~half the gap
        r = w / h
        m = math.sqrt(r * tw / th) if fit == "blend" else r
        sw, sh = (even(th * m), th) if m > tw / th else (tw, even(tw / m))
        sw, sh = max(sw, tw), max(sh, th)
        return f"scale={sw}:{sh}:flags=lanczos,crop={tw}:{th},setsar=1"
    return (f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def build_filter(w, h, tw, th, strength=1.0, fit="auto", crop=None):
    """crop: (cw, ch, x, y) black-border box to cut first; w, h are the frame size before it."""
    pre = ""
    if crop:
        pre = f"crop={crop[0]}:{crop[1]}:{crop[2]}:{crop[3]},"
        w, h = crop[0], crop[1]
    k = min(tw / w, th / h)
    resize = pre + resize_filter(w, h, tw, th, resolve_fit(w, h, tw, th, fit))
    if k <= 1.05:
        return resize
    cas = min(0.25 + 0.15 * k * strength, 0.9)
    s = strength
    return (f"{pre}deblock=filter=weak:block=8,hqdn3d={1.5*s}:{1.5*s}:{3*s}:{3*s},"
            f"{resize[len(pre):]},cas={cas:.2f}")


# quality -> (GPU cq/qp, x264 crf, cap bits per pixel per second or None)
QUALITY = {"delivery": (26, 23, 3.0), "master": (20, 18, None)}

# GPU encoders tried by encoder="auto", in this order; libx264 (CPU) when none works
GPU_ENCODERS = {"nvenc": "h264_nvenc", "amf": "h264_amf", "qsv": "h264_qsv"}
ENCODERS = ("auto", *GPU_ENCODERS, "x264")


@lru_cache
def gpu_works(name):
    """True if this machine can actually encode with that GPU encoder (driver + hardware present)."""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=black:s=320x240:r=25:d=0.4", *encoder_args(name, "delivery", 320, 240),
           "-pix_fmt", pix_fmt(name), "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_encoder(encoder="auto"):
    """auto -> the first GPU encoder that works here (NVIDIA, then AMD, then Intel), else x264."""
    if encoder != "auto":
        return encoder
    return next((name for name in GPU_ENCODERS if gpu_works(name)), "x264")


def pix_fmt(encoder):
    # QSV takes nv12 only; AMF converts anything else to nv12 itself, so hand it nv12 directly
    return "nv12" if encoder in ("qsv", "amf") else "yuv420p"


def encoder_args(encoder, quality, tw, th, crf=None):
    cq, x264_crf, bpp = QUALITY[quality]
    if crf is not None:
        cq = x264_crf = crf
    cap = []
    if bpp:
        rate = int(tw * th * bpp)  # 1920x1080 -> ~6.2 Mbit/s ceiling
        cap = ["-maxrate", str(rate), "-bufsize", str(rate * 2)]
    if encoder == "x264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(x264_crf), *cap]
    if encoder == "amf":
        # Tuned on RDNA2 (RX 6600, VCN 3.0). Constant QP behaves the same on every AMD generation,
        # unlike AMF's VBR modes; B/P frames 2 QP coarser than I. AMF's auto profile can pick Main,
        # so ask for High + CABAC (~10% smaller at the same QP).
        return ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "quality",
                "-profile", "high", "-coder", "cabac", "-rc", "cqp",
                "-qp_i", str(cq), "-qp_p", str(cq + 2), "-qp_b", str(cq + 2)]
    if encoder == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "slower", "-global_quality", str(cq)]
    return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(cq), "-b:v", "0", *cap]


def build_command(src, out, info, tw, th, strength=1.0, encoder="x264", quality="master",
                  crf=None, threads=None, fit="auto", crop=None):
    vf = build_filter(info.width, info.height, tw, th, strength, fit, crop)
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-y", "-protocol_whitelist", "file", "-i", str(src)]
    if threads:
        cmd += ["-threads", str(threads), "-filter_threads", str(threads)]
    cmd += ["-map", "0:V:0", "-map", "0:a:0?", "-vf", vf,
            *encoder_args(encoder, quality, tw, th, crf),
            "-pix_fmt", pix_fmt(encoder), "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)]
    return cmd, vf


def _run(cmd, duration, on_progress, cancel):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", creationflags=NO_WINDOW)
    tail = []
    t = threading.Thread(target=lambda: tail.extend(proc.stderr.read().splitlines()[-30:]), daemon=True)
    t.start()
    for line in proc.stdout:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise Canceled()
        if on_progress and duration > 0 and line.startswith("out_time_us="):
            v = line.split("=", 1)[1].strip()
            if v.isdigit():
                on_progress(min(int(v) / 1e6 / duration, 1.0))
    proc.wait()
    t.join(5)
    if cancel is not None and cancel.is_set():
        raise Canceled()
    return proc.returncode, "\n".join(tail)


@dataclass
class Plan:
    crop: tuple | None   # black-border box removed first (w, h, x, y)
    width: int           # picture size after that crop
    height: int
    tw: int
    th: int
    fit: str             # pad | crop | stretch | blend


def make_plan(src, info, to=None, scale=None, short=1080, fit="auto", snap=True, remove_borders=False,
              platform="any", aspect=None):
    crop = detect_borders(src, info) if remove_borders else None
    w, h = (crop[0], crop[1]) if crop else (info.width, info.height)
    tw, th = target_size(w, h, to, scale, short, snap, platform, aspect)
    return Plan(crop, w, h, tw, th, resolve_fit(w, h, tw, th, fit))


def upscale(src, out, to=None, scale=None, short=1080, strength=1.0, encoder="auto",
            quality="master", crf=None, threads=None, on_progress=None, cancel=None, info=None,
            fit="auto", snap=True, remove_borders=False, plan=None, platform="any", aspect=None):
    """Run the full pipeline. encoder: auto | nvenc | amf | qsv | x264; a failing GPU encoder falls back to libx264.

    Returns dict with the sizes, filter and encoder actually used.
    """
    info = info or probe(src)
    plan = plan or make_plan(src, info, to, scale, short, fit, snap, remove_borders, platform, aspect)
    tw, th = plan.tw, plan.th
    enc0 = resolve_encoder(encoder)
    order = [enc0, "x264"] if enc0 != "x264" else ["x264"]
    min_kbps = MIN_KBPS.get(platform, 0)
    err = ""
    for enc in order:
        q = crf
        for _ in range(3):  # re-encode at higher quality while below the platform's bitrate floor
            cmd, vf = build_command(src, out, info, tw, th, strength, enc, quality, q, threads, plan.fit, plan.crop)
            code, err = _run(cmd, info.duration, on_progress, cancel)
            if code != 0 or not Path(out).exists() or Path(out).stat().st_size == 0:
                break
            kbps = Path(out).stat().st_size * 8 / 1000 / info.duration if info.duration else 0
            if not min_kbps or not kbps or kbps >= min_kbps:
                return {"in_size": (info.width, info.height), "out_size": (tw, th), "vf": vf,
                        "encoder": enc, "info": info, "plan": plan, "kbps": round(kbps)}
            base = q if q is not None else QUALITY[quality][1 if enc == "x264" else 0]
            q = max(base - 6, 10)
        else:
            return {"in_size": (info.width, info.height), "out_size": (tw, th), "vf": vf,
                    "encoder": enc, "info": info, "plan": plan, "kbps": round(kbps)}
    raise EncodeError(err[-1500:])
