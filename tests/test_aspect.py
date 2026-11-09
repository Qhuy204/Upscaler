import subprocess

from upscale_core import FFMPEG, build_filter, detect_borders, make_plan, probe, snap_aspect, target_size
from upscale_core.ffmpeg import resolve_fit


def test_snap_aspect():
    assert snap_aspect(1920, 1033) == (16, 9)
    assert snap_aspect(1033, 1920) == (9, 16)
    assert snap_aspect(1080, 1350) == (4, 5)
    assert snap_aspect(1000, 1000) == (1, 1)
    assert snap_aspect(1920, 600) is None  # 3.2:1 is far from everything


def test_near_standard_inputs_get_standard_frame():
    assert target_size(1920, 1033) == (1920, 1080)
    assert target_size(1033, 1920) == (1080, 1920)
    assert target_size(854, 470) == (1920, 1080)
    assert target_size(640, 360) == (1920, 1080)
    assert target_size(1080, 1350) == (1080, 1350)
    assert target_size(1920, 1033, snap=False) == (2008, 1080)
    assert target_size(1920, 1033, to="1920x1080") == (1920, 1080)
    assert target_size(960, 516, scale=2) == (1834, 1032)  # short side x2, then 16:9


def test_tiktok_forces_its_three_aspects():
    t = lambda w, h: target_size(w, h, platform="tiktok")  # noqa: E731
    assert t(1920, 1146) == (1920, 1080)   # the rejected clip
    assert t(1920, 1200) == (1920, 1080)   # 16:10
    assert t(1080, 1350) == (1080, 1080)   # 4:5 is closer to 1:1 than to 9:16
    assert t(1000, 1000) == (1080, 1080)
    assert t(1440, 1080) == (1920, 1080)   # 4:3 ties 16:9 / 1:1, keeps landscape
    assert t(1080, 1440) == (1080, 1920)
    assert t(2560, 1080) == (1920, 1080)   # 21:9
    assert t(360, 640) == (1080, 1920)
    assert target_size(1080, 1350, platform="meta") == (1080, 1350)
    assert target_size(1920, 1146, platform="any") == (1920, 1080)
    assert target_size(1920, 600, platform="any") == (3456, 1080)


def test_tiktok_fill_vs_bars():
    assert resolve_fit(1920, 1146, 1920, 1080) == "blend"   # ~3% stretch + ~3% crop, no bars
    assert resolve_fit(1920, 1200, 1920, 1080) == "blend"
    assert resolve_fit(1080, 1350, 1080, 1080) == "pad"      # too far: bars instead of cutting faces


def test_fit_auto_fills_close_aspects_and_pads_far_ones():
    assert resolve_fit(1920, 1033, 1920, 1080) == "blend"
    assert resolve_fit(640, 360, 1080, 1920) == "pad"
    assert resolve_fit(1920, 1033, 1920, 1080, "stretch") == "stretch"


def test_blend_filter_has_no_bars():
    vf = build_filter(1920, 1033, 1920, 1080)
    assert "pad=" not in vf and "crop=1920:1080" in vf and "cas" not in vf
    vf = build_filter(960, 516, 1920, 1080)
    assert vf.startswith("deblock=") and "crop=1920:1080" in vf and vf.endswith(",cas=0.55")
    assert "pad=1080:1920" in build_filter(640, 360, 1080, 1920)


def test_border_crop_goes_first():
    vf = build_filter(640, 480, 1920, 1080, crop=(640, 360, 0, 60))
    assert vf.startswith("crop=640:360:0:60,deblock=") and "pad=" not in vf


def _make(path, vf):
    subprocess.run([FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=2",
                    "-vf", vf, "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True)


def test_detect_letterbox(tmp_path):
    p = tmp_path / "boxed.mp4"
    _make(p, "pad=640:480:0:60:black")
    info = probe(p)
    box = detect_borders(p, info)
    assert box and abs(box[1] - 360) <= 4 and abs(box[3] - 60) <= 4 and box[0] >= 636
    plan = make_plan(p, info, remove_borders=True)
    assert (plan.tw, plan.th) == (1920, 1080) and plan.fit == "blend"


def test_no_borders(sample_video):
    assert detect_borders(sample_video, probe(sample_video)) is None
