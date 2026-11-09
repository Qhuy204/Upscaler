"""In-memory Redis over TCP for local testing when Docker/Redis is unavailable. Not for production.

python tools/fake_redis.py [port]
"""
import sys

from fakeredis import TcpFakeServer

port = int(sys.argv[1]) if len(sys.argv) > 1 else 6379
server = TcpFakeServer(("127.0.0.1", port), server_type="redis")
print(f"fake redis on 127.0.0.1:{port}", flush=True)
server.serve_forever()
