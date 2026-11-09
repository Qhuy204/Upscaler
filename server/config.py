from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://127.0.0.1:6379/0"
    queue_name: str = "upscale:queue"      # encode workers (GPU host)
    aux_queue_name: str = "upscale:aux"    # aux workers: webhooks + storage cleanup
    worker_role: str = "encode"            # encode | aux (run_worker --role overrides)
    worker_name: str = ""                  # default <host>-<role>-<pid>

    # dashboard (python -m server.run_dashboard), HTTP basic auth
    admin_user: str = "admin"
    admin_password: str = ""               # empty = dashboard refuses to start
    dashboard_port: int = 8090

    # storage: "r2" (Cloudflare R2, S3 API) or "local" (files served by this API, for dev / no R2)
    storage_backend: str = "local"
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "upscale"
    r2_endpoint: str = ""  # default https://<account_id>.r2.cloudflarestorage.com
    local_storage_dir: Path = ROOT / "data" / "storage"
    # base URL callers use to reach this API (used for local-backend signed URLs)
    public_base_url: str = "http://127.0.0.1:8080"
    # HMAC secret for local-backend signed URLs; must be the same for API and worker
    signing_secret: str = "change-me"

    work_dir: Path = ROOT / "data" / "work"
    # multipart uploads go to storage (inputs/...) instead of a local path; needed when the API runs
    # in Docker and the encode worker on the host (docker compose service "api" sets it)
    multipart_via_storage: bool = False

    # worker capacity
    ffmpeg_slots: int = 4          # concurrent ffmpeg processes (NVENC on a 3060 allows 8 sessions)
    threads_per_job: int = 4       # ffmpeg -threads per job; slots * threads ~= CPU threads
    worker_max_jobs: int = 10      # jobs held by one worker (download/upload overlap the ffmpeg slots)
    encoder: str = "auto"          # auto (NVIDIA > AMD > Intel GPU, else CPU) | nvenc | amf | qsv | x264

    # limits
    job_timeout: int = 600
    max_retries: int = 2
    max_input_mb: int = 300
    max_upload_mb: int = 95        # Cloudflare free plan caps request bodies at 100 MB
    max_duration: float = 600
    max_side: int = 4096
    max_batch: int = 100
    download_timeout: int = 300
    # allow source/callback URLs on private/loopback addresses (local testing only, never in production)
    allow_private_urls: bool = False

    # every input/output object is deleted this long after it is created (worker sweeper + R2 lifecycle)
    media_ttl: int = 86400
    output_url_ttl: int = 86400    # capped to the time the output has left
    upload_url_ttl: int = 3600
    job_ttl: int = 7 * 86400

    # per API key defaults (overridable per key)
    default_max_queued: int = 200
    default_max_running: int = 4
    rate_limit_per_min: int = 600  # API requests per key per minute

    # abuse protection
    auth_fail_limit: int = 20      # failed API-key attempts per IP within auth_fail_window ...
    auth_fail_window: int = 600
    auth_ban_seconds: int = 900    # ... block that IP for this long
    max_json_kb: int = 256         # JSON request bodies

    @property
    def r2_endpoint_url(self) -> str:
        return self.r2_endpoint or f"https://{self.r2_account_id}.r2.cloudflarestorage.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
