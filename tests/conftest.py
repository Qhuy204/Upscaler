import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakeredis import FakeAsyncRedis  # noqa: E402

from server.config import Settings  # noqa: E402
from server.storage import LocalStorage  # noqa: E402
from upscale_core import FFMPEG  # noqa: E402


class FakeArq:
    """Records enqueue_job calls instead of talking to arq."""

    def __init__(self):
        self.calls = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return object()


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, storage_backend="local", local_storage_dir=tmp_path / "storage",
                    work_dir=tmp_path / "work", public_base_url="http://testserver", signing_secret="test",
                    ffmpeg_slots=2, threads_per_job=2, default_max_queued=5, default_max_running=2)


@pytest.fixture
def redis():
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def storage(settings):
    return LocalStorage(settings)


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    """2 s 320x180 test video with audio."""
    p = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run([FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(p)], check=True, capture_output=True)
    return p
