import pytest
from upscale_core import build_filter, probe, target_size
from upscale_core.ffmpeg import encoder_args, pix_fmt, resolve_encoder


def test_target_size_short_side():
    assert target_size(360, 640) == (1080, 1920)
    assert target_size(640, 360) == (1920, 1080)
    assert target_size(640, 360, short=720) == (1280, 720)


def test_target_size_to_and_scale():
    assert target_size(640, 360, to="1919x1079") == (1920, 1080)  # forced even
    assert target_size(640, 360, scale=2) == (1280, 720)


def test_filter_skips_cleanup_when_not_upscaling():
    vf = build_filter(1920, 1080, 1920, 1080)
    assert vf.startswith("scale=") and "cas" not in vf and "hqdn3d" not in vf


def test_filter_full_chain_and_cas_cap():
    vf = build_filter(360, 640, 1080, 1920, strength=2.0)
    assert vf.startswith("deblock=") and "hqdn3d=3.0:3.0:6.0:6.0" in vf and vf.endswith("cas=0.90")


def test_encoder_presets():
    d = encoder_args("nvenc", "delivery", 1920, 1080)
    assert d[d.index("-cq") + 1] == "26" and "-maxrate" in d
    m = encoder_args("x264", "master", 1920, 1080)
    assert m[m.index("-crf") + 1] == "18" and "-maxrate" not in m
    assert encoder_args("x264", "delivery", 1920, 1080, crf=30)[5] == "30"


def test_probe(sample_video):
    info = probe(sample_video)
    assert (info.width, info.height) == (320, 180)
    assert 1.9 < info.duration < 2.2 and info.has_audio and info.fps == 25


def test_gpu_encoder_args():
    a = encoder_args("amf", "delivery", 1920, 1080)
    assert a[a.index("-c:v") + 1] == "h264_amf" and a[a.index("-qp_i") + 1] == "26" and a[a.index("-qp_p") + 1] == "28"
    q = encoder_args("qsv", "master", 1920, 1080, crf=22)
    assert q[q.index("-c:v") + 1] == "h264_qsv" and q[q.index("-global_quality") + 1] == "22"
    assert pix_fmt("qsv") == "nv12" and pix_fmt("nvenc") == pix_fmt("amf") == "yuv420p"


def test_resolve_encoder_order(monkeypatch):
    from upscale_core import ffmpeg
    assert resolve_encoder("x264") == "x264" and resolve_encoder("amf") == "amf"
    for works, want in (({"nvenc", "amf"}, "nvenc"), ({"amf", "qsv"}, "amf"), ({"qsv"}, "qsv"), (set(), "x264")):
        monkeypatch.setattr(ffmpeg, "gpu_works", lambda name, w=works: name in w)
        assert ffmpeg.resolve_encoder("auto") == want


def test_auto_falls_back_to_x264_when_gpu_encode_fails(sample_video, tmp_path, monkeypatch):
    from upscale_core import ffmpeg
    monkeypatch.setattr(ffmpeg, "resolve_encoder", lambda e: "amf")
    real = ffmpeg.encoder_args
    # simulate a broken AMD driver: the amf command gets an encoder name ffmpeg does not know
    monkeypatch.setattr(ffmpeg, "encoder_args",
                        lambda enc, *a, **k: ["-c:v", "no_such_enc"] if enc == "amf" else real(enc, *a, **k))
    r = ffmpeg.upscale(sample_video, tmp_path / "o.mp4", short=240, quality="delivery")
    assert r["encoder"] == "x264" and (tmp_path / "o.mp4").stat().st_size > 0


def test_forced_aspect():
    # 4:5 normally goes to 1:1 on tiktok; forced to 9:16 / 16:9 instead
    assert target_size(1080, 1350, platform="tiktok") == (1080, 1080)
    assert target_size(1080, 1350, platform="tiktok", aspect="9:16") == (1080, 1920)
    assert target_size(1080, 1350, platform="tiktok", aspect="16:9") == (1920, 1080)
    assert target_size(640, 360, short=720, platform="tiktok", aspect="1:1") == (720, 720)
    assert target_size(1080, 1920, platform="meta", aspect="4:5") == (1080, 1350)
    assert target_size(1080, 1920, platform="any", aspect="2:1") == (2160, 1080)
    assert target_size(640, 360, scale=2, platform="tiktok", aspect="9:16") == (720, 1280)
    with pytest.raises(ValueError):
        target_size(1080, 1920, platform="tiktok", aspect="4:5")
