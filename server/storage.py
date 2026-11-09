"""Object storage: Cloudflare R2 (S3 API) or a local directory served by the API.

All methods are blocking; call them through asyncio.to_thread from async code.
"""
import hashlib
import hmac
import shutil
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from .config import Settings


class StorageError(Exception):
    pass


class TooLarge(StorageError):
    pass


class NotFound(StorageError):
    pass


def sign(secret, method, key, exp):
    msg = f"{method}\n{key}\n{exp}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret, method, key, exp, sig):
    try:
        if int(exp) < time.time():
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, method, key, exp), sig or "")


class LocalStorage:
    """Dev / single-box backend. URLs point at /files and /v1/uploads/put on this API."""

    def __init__(self, s: Settings):
        self.root = Path(s.local_storage_dir)
        self.base = s.public_base_url.rstrip("/")
        self.secret = s.signing_secret

    def path(self, key):
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError("bad key")
        return p

    def _url(self, route, method, key, ttl, extra=None):
        exp = int(time.time() + ttl)
        q = {"exp": exp, "sig": sign(self.secret, method, key, exp), **(extra or {})}
        return f"{self.base}{route}/{quote(key)}?{urlencode(q)}"

    def presign_put(self, key, ttl, content_type=None):
        return self._url("/v1/uploads/put", "PUT", key, ttl)

    def presign_get(self, key, ttl, filename=None):
        return self._url("/files", "GET", key, ttl, {"name": filename} if filename else None)

    def size(self, key):
        p = self.path(key)
        if not p.exists():
            raise NotFound(key)
        return p.stat().st_size

    def upload_file(self, src, key, content_type="video/mp4"):
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, p)

    def download_file(self, key, dest, max_bytes):
        if self.size(key) > max_bytes:
            raise TooLarge(key)
        shutil.copyfile(self.path(key), dest)

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)


class R2Storage:
    def __init__(self, s: Settings):
        import boto3
        from botocore.config import Config

        if not (s.r2_account_id or s.r2_endpoint) or not s.r2_access_key_id:
            raise StorageError("R2 is not configured (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")
        self.bucket = s.r2_bucket
        self.s3 = boto3.client(
            "s3", endpoint_url=s.r2_endpoint_url, region_name="auto",
            aws_access_key_id=s.r2_access_key_id, aws_secret_access_key=s.r2_secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                          max_pool_connections=32))

    def presign_put(self, key, ttl, content_type=None):
        params = {"Bucket": self.bucket, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self.s3.generate_presigned_url("put_object", Params=params, ExpiresIn=ttl)

    def presign_get(self, key, ttl, filename=None):
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)

    def size(self, key):
        from botocore.exceptions import ClientError

        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise NotFound(key) from e
            raise

    def upload_file(self, src, key, content_type="video/mp4"):
        # boto3 switches to multipart automatically above 8 MB
        self.s3.upload_file(str(src), self.bucket, key, ExtraArgs={"ContentType": content_type})

    def download_file(self, key, dest, max_bytes):
        if self.size(key) > max_bytes:
            raise TooLarge(key)
        self.s3.download_file(self.bucket, key, str(dest))

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)


def make_storage(s: Settings):
    return R2Storage(s) if s.storage_backend == "r2" else LocalStorage(s)
