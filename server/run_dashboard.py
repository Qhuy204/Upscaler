"""python -m server.run_dashboard [--host 127.0.0.1] [--port 8090]"""
import argparse

import uvicorn

from .config import get_settings
from .logs import setup


def main():
    s = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=s.dashboard_port)
    a = ap.parse_args()
    setup("dashboard")
    from .dashboard import create_dashboard
    uvicorn.run(create_dashboard(s), host=a.host, port=a.port, log_config=None, server_header=False)


if __name__ == "__main__":
    main()
