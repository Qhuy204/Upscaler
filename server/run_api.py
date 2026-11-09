"""python -m server.run_api [--host 127.0.0.1] [--port 8080]"""
import argparse

import uvicorn

from .logs import setup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    setup("api")
    # behind cloudflared: trust X-Forwarded-* from localhost only
    uvicorn.run("server.api:app", host=a.host, port=a.port, log_config=None, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", timeout_keep_alive=30, server_header=False,
                limit_concurrency=500)


if __name__ == "__main__":
    main()
