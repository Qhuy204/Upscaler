"""Run the worker against tools/fake_redis.py (fakeredis lacks INFO, which arq only logs at startup).

Local testing only; production uses server/run_worker.py with real Redis.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arq.worker  # noqa: E402


async def _skip_info(*a, **k):
    pass


arq.worker.log_redis_info = _skip_info

from server.run_worker import main  # noqa: E402

main()
