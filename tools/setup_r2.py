"""Set up and check the R2 bucket using the R2_* values in .env.

python tools/setup_r2.py

Creates the bucket if missing, sets lifecycle (inputs/ and outputs/ 1 day) and CORS, then checks
upload, presigned GET, presigned PUT, and delete. Bucket creation, lifecycle and CORS need an
"Admin Read & Write" token; with an "Object Read & Write" token do those three in the dashboard.
"""
import sys
import tempfile
from pathlib import Path

import httpx
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import get_settings  # noqa: E402
from server.storage import R2Storage  # noqa: E402

LIFECYCLE = {"Rules": [
    {"ID": "inputs-1d", "Status": "Enabled", "Filter": {"Prefix": "inputs/"}, "Expiration": {"Days": 1}},
    {"ID": "outputs-1d", "Status": "Enabled", "Filter": {"Prefix": "outputs/"}, "Expiration": {"Days": 1}},
    {"ID": "abort-multipart-1d", "Status": "Enabled", "Filter": {"Prefix": ""},
     "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}},
]}
CORS = {"CORSRules": [{"AllowedOrigins": ["*"], "AllowedMethods": ["GET", "PUT"], "AllowedHeaders": ["*"],
                       "MaxAgeSeconds": 3600}]}


def step(name, fn):
    try:
        fn()
        print(f"  ok    {name}")
        return True
    except ClientError as e:
        err = e.response.get("Error", {})
        print(f"  FAIL  {name}: {err.get('Code')} {err.get('Message')}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  {name}: {e.__class__.__name__}: {e}")
    return False


def main():
    s = get_settings()
    st = R2Storage(s)
    s3, b = st.s3, st.bucket
    print(f"endpoint {s.r2_endpoint_url}  bucket {b}")

    def ensure_bucket():
        try:
            s3.head_bucket(Bucket=b)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket", "NotFound"):
                raise
            s3.create_bucket(Bucket=b)
            print(f"        created bucket {b}")

    ok = step("bucket exists", ensure_bucket)
    step("lifecycle inputs/ 1d, outputs/ 1d (backstop for the worker's 24h sweeper)",
         lambda: s3.put_bucket_lifecycle_configuration(Bucket=b, LifecycleConfiguration=LIFECYCLE))
    step("CORS GET/PUT", lambda: s3.put_bucket_cors(Bucket=b, CORSConfiguration=CORS))
    if not ok:
        sys.exit(1)

    key, body = "inputs/_setup_check.txt", b"upscale r2 check"
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "a.txt"
        src.write_bytes(body)
        ok &= step("upload", lambda: st.upload_file(src, key, "text/plain"))

    def get_presigned():
        r = httpx.get(st.presign_get(key, 300, "check.txt"), timeout=30)
        assert r.status_code == 200 and r.content == body, f"HTTP {r.status_code}"

    def put_presigned():
        r = httpx.put(st.presign_put(key, 300, "text/plain"), content=body + b"!",
                      headers={"Content-Type": "text/plain"}, timeout=30)
        assert r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}"
        assert st.size(key) == len(body) + 1

    ok &= step("presigned GET", get_presigned)
    ok &= step("presigned PUT", put_presigned)
    ok &= step("delete", lambda: st.delete(key))
    print("R2 READY" if ok else "R2 NOT READY")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
