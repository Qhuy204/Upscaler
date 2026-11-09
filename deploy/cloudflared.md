# Đưa API ra internet: Cloudflare Tunnel + R2

Domain `qhuy204.id.vn` đã dùng nameserver của Cloudflare. Các bước dưới đây cần **bạn đăng nhập Cloudflare**
(Claude không nhập credential hộ).

## 1. R2 (lưu input/output)

1. Dashboard → **R2 Object Storage** → *Create bucket* → tên `upscale` (location: Automatic / APAC).
2. Bucket `upscale` → *Settings* → **Object lifecycle rules**:
   - `inputs/`  → delete objects after **1 day**
   - `outputs/` → delete objects after **1 day**
   (`tools/setup_r2.py` đặt sẵn. Đây chỉ là lưới an toàn: worker tự xoá chính xác 24h sau khi upload,
   cron `sweep_media` mỗi 5 phút, danh sách ở Redis zset `media:expire`.)
3. R2 → *Manage R2 API Tokens* → **Create API token** → quyền *Object Read & Write*, chỉ áp dụng cho bucket `upscale`.
   Ghi lại **Access Key ID**, **Secret Access Key**, và **Account ID** (hiện ở trang R2).
4. Bucket `upscale` → *Settings* → **CORS policy** (chỉ cần nếu trình duyệt upload thẳng lên R2):
   ```json
   [{"AllowedOrigins": ["*"], "AllowedMethods": ["PUT", "GET"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 3600}]
   ```
5. Tạo `G:\upscale\.env` từ `.env.example`, điền `STORAGE_BACKEND=r2`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
   `R2_SECRET_ACCESS_KEY`, và `SIGNING_SECRET` ngẫu nhiên.

## 2. Tunnel `upscale.qhuy204.id.vn` → `localhost:8080`

**Đã làm (2026-11-09):** tunnel `upscale` (id `0c5a8f6c-f628-4219-ad5b-90d946023b90`), CNAME `upscale.qhuy204.id.vn`,
config ở `deploy/cloudflared.yml`, chạy bằng `deploy/start-tunnel.ps1` (task `Upscale-tunnel` trong `install-tasks.ps1`).
cloudflared nằm ở `C:\cloudflared\cloudflared.exe`. Các bước dưới đây chỉ để tham khảo khi dựng lại trên máy khác.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel login                     # mở trình duyệt, chọn zone qhuy204.id.vn
cloudflared tunnel create upscale            # tạo credentials %USERPROFILE%\.cloudflared\<TUNNEL_ID>.json
cloudflared tunnel route dns upscale upscale.qhuy204.id.vn
```

Tạo `%USERPROFILE%\.cloudflared\config.yml`:

```yaml
tunnel: upscale
credentials-file: C:\Users\Qhuy\.cloudflared\<TUNNEL_ID>.json
ingress:
  - hostname: upscale.qhuy204.id.vn
    service: http://localhost:8080
    originRequest:
      connectTimeout: 30s
      noHappyEyeballs: true
  - service: http_status:404
```

**Máy AMD (i5-12400F + RX 6600):** tunnel riêng `upscaler` (id `e67cf44d-5bee-4b1f-b71a-50471e8dfe2c`), CNAME
`upscaler.qhuy204.id.vn`, ingress `service: http://127.0.0.1:8081` (port 8080 bị Apache chiếm). API chạy trong Docker:
`docker compose --env-file .env -f deploy\docker-compose.yml --profile api up -d --build`. Không chạy chung một tunnel
trên hai máy: cloudflared coi đó là replica và chia request ngẫu nhiên giữa hai máy.

Chạy thử: `cloudflared tunnel run upscale`, rồi `curl https://upscale.qhuy204.id.vn/healthz`.

Cài thành Windows service (PowerShell **Run as Administrator**):

```powershell
cloudflared service install
# service chạy dưới LocalSystem nên đọc config ở C:\Windows\System32\config\systemprofile\.cloudflared\
New-Item -ItemType Directory -Force C:\Windows\System32\config\systemprofile\.cloudflared
Copy-Item $env:USERPROFILE\.cloudflared\* C:\Windows\System32\config\systemprofile\.cloudflared\
# sửa credentials-file trong bản copy cho đúng đường dẫn mới, rồi:
Restart-Service cloudflared
```

## 3. Bảo vệ trên Cloudflare

Trên dashboard (bạn tự bật):
- **Security → WAF → Rate limiting rules**: `URI Path starts with /v1/` → 120 requests / 1 phút / IP → Block 1 phút.
- **Security → Bots → Bot Fight Mode**: bật. **Security → Settings**: Security level *Medium*.
- **SSL/TLS → Edge Certificates**: Always Use HTTPS, Min TLS 1.2.
- Body upload qua Tunnel bị giới hạn **100 MB** (gói Free) → file lớn hơn dùng `POST /v1/uploads` (presigned R2).

Đã có sẵn trong app:
- API chỉ nghe `127.0.0.1`, vào được duy nhất qua Tunnel; không mở port router.
- Sai API key 20 lần / 10 phút từ một IP (`CF-Connecting-IP`) → chặn IP đó 15 phút (429). Gỡ: `manage_keys.py unban <ip>`.
- Rate limit 600 request/phút mỗi key (`RATE_LIMIT_PER_MIN`, riêng từng key: `manage_keys.py limits <id> --rate-limit N`).
- Khoá key theo IP server gọi tới: `manage_keys.py allow <key_id> 1.2.3.4,10.0.0.0/8` (`*` để bỏ).
- Tắt `/docs`, `/openapi.json`; header nosniff/DENY/HSTS/no-store; JSON body ≤ 256 KB; chặn SSRF cho source/callback URL.
- Redis có mật khẩu (`REDIS_PASSWORD` trong `.env`), tắt FLUSHALL/FLUSHDB/CONFIG/DEBUG, chỉ nghe `127.0.0.1`.
- Media trên R2 tự xoá sau 24h; link tải không bao giờ sống lâu hơn file.
- Nên đổi R2 token sang loại *Object Read & Write* chỉ cho bucket `upscale` (token hiện tại là Admin).

## 4. Chạy dịch vụ lúc khởi động

```powershell
docker compose --env-file G:\upscale\.env -f G:\upscale\deploy\docker-compose.yml up -d   # Redis (bật "Start Docker Desktop when you sign in")
powershell -ExecutionPolicy Bypass -File G:\upscale\deploy\install-tasks.ps1
Start-ScheduledTask Upscale-api; Start-ScheduledTask Upscale-worker
G:\upscale\venv\Scripts\python.exe G:\upscale\tools\manage_keys.py create <tên-client>
```

`up -d` dựng cả 3 container: `upscale-redis`, `upscale-dashboard`, `aux-worker`. Sau khi sửa code: thêm `--build`.

## 5. Các worker

| Worker | Chạy ở đâu | Queue | Làm gì |
|---|---|---|---|
| `encode` | Windows host (task `Upscale-worker`, cần NVENC) | `upscale:queue` | tải input → ffmpeg → upload R2 |
| `aux` | container `aux-worker` | `upscale:aux` | gửi webhook, xoá media R2 sau 24h |

- Thêm encode worker trên máy khác (cùng Redis): `python -m server.run_worker --role encode --name <may>-encode`.
- Thêm aux: `docker compose ... up -d --scale aux-worker=2`.
- Nếu không còn aux worker nào sống, encode worker tự gửi webhook và tự dọn R2, nên không job nào bị bỏ sót.

## 6. Dashboard

`http://127.0.0.1:8090`, đăng nhập bằng `ADMIN_USER` / `ADMIN_PASSWORD` trong `.env`. Các tab:
- Tổng quan: queue, worker, số job 24h, thời gian chờ/encode trung bình.
- Jobs: lọc theo trạng thái, key, tên file; bấm vào job để xem lịch sử trạng thái và các request liên quan.
- Trạng thái: luồng sự kiện của job.
- Requests: log mọi request API (IP, key, path, HTTP code, ms).
- Workers, API keys.

Dashboard chỉ nghe trên `127.0.0.1`. Nếu muốn mở ra ngoài, route qua Tunnel kèm **Cloudflare Access**, không mở trần.

## 7. Tỉ lệ khung hình (`options.platform`)

- `tiktok` (mặc định): luôn ra 16:9, 9:16 hoặc 1:1, lấy tỉ lệ gần nhất. Ví dụ 1920×1146 → 1920×1080, 1080×1350 → 1080×1080.
- `meta`: 9:16, 4:5, 1:1, 16:9.
- `any`: chỉ snap khi lệch ≤ 8% (1920×1033 → 1920×1080), còn lại giữ nguyên tỉ lệ.
- `fit=auto`: lệch ≤ 12% thì `blend` (kéo giãn ~6% + cắt ~6%, không viền đen); lệch nhiều hơn thì `pad` (thêm viền đen). Có thể ép bằng `pad`, `crop`, `stretch`.
- Bitrate tối thiểu cho TikTok là 600 kbps (TikTok yêu cầu ≥ 516); thấp hơn thì tự encode lại.
