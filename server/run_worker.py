"""python -m server.run_worker [--role encode|aux] [--name NAME]

encode: upscale jobs (needs ffmpeg + GPU), aux: webhooks + storage cleanup. Run as many of each as
you like, on any machine that reaches the same Redis; give each a unique --name.
"""
import argparse

from .logs import setup


def main():
    from .config import get_settings
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["encode", "aux"], default=s.worker_role)
    ap.add_argument("--name", default=s.worker_name)
    a = ap.parse_args()
    setup(f"worker-{a.role}")
    from arq import run_worker

    from . import jobs
    from .worker import ROLES, worker_name
    jobs.WORKER = a.name or worker_name(s, a.role)
    run_worker(ROLES[a.role])


if __name__ == "__main__":
    main()
