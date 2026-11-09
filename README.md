# Upscaler — API upscale video quảng cáo bằng ffmpeg

Server HTTP nhận video quảng cáo (URL, Google Drive, upload), upscale lên 1080p và ép đúng tỉ lệ khung hình TikTok / Meta chấp nhận. Video được xử lý bằng ffmpeg, encode bằng GPU (NVIDIA / AMD / Intel, tự nhận card). Kết quả lưu trên Cloudflare R2. Server mở ra internet qua Cloudflare Tunnel.

- Production: `https://upscale.qhuy204.id.vn`
- Tài liệu API đầy đủ cho người gọi: [docs/API.md](docs/API.md)

## Pipeline xử lý

```
deblock → hqdn3d (khử nhiễu) → crop viền đen → lanczos (phóng) → khớp tỉ lệ (blend / pad / crop) → cas (làm nét) → H.264
```

- Chỉ dùng filter ffmpeg, không dùng AI. Mặt người và chữ không bị biến dạng, nhanh hơn Video2X / Real-ESRGAN khoảng 10 lần.
- Encoder `ENCODER=auto` thử theo thứ tự `nvenc` → `amf` → `qsv`, card nào encode thử được thì dùng card đó. Không có GPU thì dùng `x264` (CPU). Nếu GPU lỗi giữa chừng, job tự encode lại bằng x264.
- Với `platform: tiktok`, bitrate tối thiểu 600 kbps (TikTok từ chối file dưới khoảng 516 kbps).

## Kiến trúc

```
Caller ──HTTPS──> Cloudflare ──Tunnel──> cloudflared ──> FastAPI :8080 (host)
                                                            │ ghi job
                                                            ▼
                                              Redis 7 (Docker, AOF) — hàng đợi arq
                                               │                         │
                         Encode worker (host, GPU)              Aux worker (Docker)
                         tải input → ffmpeg → upload R2          webhook + xoá file R2 sau 24h
                                               │
                                     Cloudflare R2 ──presigned URL──> Caller tải kết quả
```

| Thành phần | Chạy ở đâu | Ghi chú |
|---|---|---|
| API (`server.run_api`) | Host, Task Scheduler `Upscale-api` | `127.0.0.1:8080` |
| Encode worker (`server.run_worker --role encode`) | Host, Task Scheduler `Upscale-worker` | Cần ffmpeg và GPU. 8 slot ffmpeg × 3 thread |
| Tunnel (`cloudflared`) | Host, Task Scheduler `Upscale-tunnel` | Xem [deploy/cloudflared.md](deploy/cloudflared.md) |
| Redis | Docker `upscale-redis` | `127.0.0.1:6379`, AOF, có mật khẩu |
| Dashboard admin | Docker `upscale-dashboard` | `http://127.0.0.1:8090`, đăng nhập bằng `ADMIN_USER` / `ADMIN_PASSWORD` |
| Aux worker | Docker `aux-worker` | Gửi webhook, dọn R2. Tăng bằng `--scale aux-worker=N` |

Có thể chạy thêm encode worker ở máy khác, chỉ cần trỏ cùng Redis và đặt `--name` khác nhau.

## Cài đặt (Windows)

Yêu cầu: Python 3.11+, Docker Desktop, card GPU có driver mới (không bắt buộc). ffmpeg được cài qua package `imageio-ffmpeg`.

```powershell
git clone https://github.com/Qhuy204/Upscaler.git G:\upscale
cd G:\upscale
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # rồi điền REDIS_PASSWORD, SIGNING_SECRET, R2_*, ADMIN_PASSWORD
```

1. **R2:** tạo bucket và API token trên Cloudflare dashboard, điền `R2_*` vào `.env`, rồi kiểm tra bucket (lifecycle, CORS, upload thử):
   ```powershell
   venv\Scripts\python tools\setup_r2.py
   ```
2. **Redis, dashboard, aux worker:**
   ```powershell
   docker compose --env-file .env -f deploy\docker-compose.yml up -d --build
   ```
3. **Tunnel:** làm theo [deploy/cloudflared.md](deploy/cloudflared.md). File `deploy/cloudflared.yml` đang trỏ tới tunnel và hostname của production, đổi lại nếu dùng domain khác.
4. **Tự chạy khi đăng nhập Windows** (PowerShell quyền Admin). Lệnh này tạo 3 task `Upscale-api`, `Upscale-worker`, `Upscale-tunnel`. Các script trong `deploy\` dùng đường dẫn cố định `G:\upscale`, nếu clone chỗ khác thì phải sửa lại:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1
   ```
   Log nằm ở `logs\` (`api.log`, `worker-encode.log`, `tunnel.log`, `supervisor.log`).
5. **Tạo API key cho người dùng.** Lệnh in ra `api_key` (`upk_...`) và `webhook_secret` (`whsec_...`) một lần duy nhất:
   ```powershell
   venv\Scripts\python tools\manage_keys.py create <tên>
   ```
6. **Kiểm tra:** `curl https://upscale.qhuy204.id.vn/healthz` phải trả `{"ok": true, "redis": true, "worker": true}`.

### Quản lý API key

```powershell
venv\Scripts\python tools\manage_keys.py list
venv\Scripts\python tools\manage_keys.py limits <key_id> --rate-limit 600 --max-queued 200 --max-running 8
venv\Scripts\python tools\manage_keys.py disable <key_id>     # enable <key_id> để bật lại
venv\Scripts\python tools\manage_keys.py allow <key_id> <ip1,ip2>   # giới hạn IP được gọi
venv\Scripts\python tools\manage_keys.py unban <ip>           # gỡ chặn IP do sai key nhiều lần
```

## Cấu hình (`.env`)

Các giá trị mẫu nằm trong [.env.example](.env.example). **Không commit file `.env`** (đã có trong `.gitignore`).

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `REDIS_PASSWORD`, `REDIS_URL` | – | Mật khẩu Redis, phải giống nhau ở cả hai biến |
| `STORAGE_BACKEND` | `r2` | Nơi lưu input và output |
| `R2_*` | – | Account ID, access key, secret, bucket của Cloudflare R2 |
| `PUBLIC_BASE_URL` | – | URL công khai của API |
| `SIGNING_SECRET` | – | Khoá ký nội bộ (chuỗi ngẫu nhiên dài) |
| `ENCODER` | `auto` | `auto` / `nvenc` / `amf` / `qsv` / `x264` |
| `FFMPEG_SLOTS` | `8` | Số ffmpeg chạy song song trên 1 encode worker |
| `THREADS_PER_JOB` | `3` | Số thread CPU cho mỗi ffmpeg |
| `WORKER_MAX_JOBS` | `24` | Số job một worker nhận cùng lúc (tải, upload, encode) |
| `JOB_TIMEOUT`, `MAX_RETRIES` | `600`, `2` | Thời gian tối đa (giây) và số lần thử lại khi gặp lỗi tạm |
| `MAX_INPUT_MB`, `MAX_UPLOAD_MB` | `300`, `95` | Giới hạn file nguồn và giới hạn upload multipart |
| `MAX_DURATION`, `MAX_SIDE`, `MAX_BATCH` | `600`, `4096`, `100` | Giới hạn thời lượng (giây), cạnh (px), số video mỗi batch |
| `OUTPUT_URL_TTL`, `JOB_TTL` | `86400`, `604800` | Thời hạn link tải (giây), thời gian giữ thông tin job |
| `DEFAULT_MAX_QUEUED`, `DEFAULT_MAX_RUNNING` | `200`, `8` | Quota mặc định của key mới |
| `ADMIN_USER`, `ADMIN_PASSWORD` | `admin`, – | Tài khoản đăng nhập dashboard |

## Dùng API (tóm tắt)

```bash
curl -X POST https://upscale.qhuy204.id.vn/v1/jobs \
  -H "Authorization: Bearer upk_..." -H "Content-Type: application/json" \
  -d '{"source_url": "https://drive.google.com/file/d/<id>/view",
       "options": {"platform": "tiktok", "aspect": "9:16", "fit": "pad"},
       "callback_url": "https://your.server/hook"}'
```

| Endpoint | Mô tả |
|---|---|
| `POST /v1/jobs` | Tạo job từ URL, `upload_key` hoặc file multipart (≤ 95 MB) |
| `POST /v1/uploads` | Lấy link PUT để upload file lớn (≤ 300 MB) thẳng lên R2 |
| `POST /v1/batches` | Nhiều URL hoặc cả folder Google Drive (≤ 100 video) |
| `GET /v1/jobs/{id}` | Trạng thái, tiến độ, link tải kết quả |
| `GET /v1/jobs` · `DELETE /v1/jobs/{id}` | Danh sách job · huỷ job |
| `GET /v1/stats` · `GET /healthz` | Hàng đợi và quota · tình trạng server |

Các option chính:

- `platform`: `tiktok` (mặc định; 9:16, 1:1, 16:9), `meta` (thêm 4:5), `any` (giữ tỉ lệ gốc).
- `aspect`: ép tỉ lệ đích, ví dụ `"9:16"`. Bỏ trống thì chọn tỉ lệ cho phép gần nhất với video gốc.
- `fit`: `auto` (lệch ≤ 12% thì blend, lệch hơn thì thêm viền đen), `pad`, `crop`, `stretch`.
- `short` / `to` / `scale`: kích thước đầu ra.
- `quality`: `delivery` (file nhẹ cho ads) hoặc `master`.

Webhook được ký bằng HMAC-SHA256. Mô tả chi tiết request, response, cách verify webhook và mã lỗi nằm trong [docs/API.md](docs/API.md).

**Lưu ý:** server chỉ nhận **video**, đầu ra luôn là MP4 H.264 + AAC. Video gốc và kết quả tự xoá sau 24h.

## Hiệu năng

Đo trên i5-13400F + RTX 3060 (NVENC), 8 slot × 3 thread: khoảng **690 ads/giờ** với ad dài 30–50 s, nguồn 360p–720p. Khi chạy một mình, mỗi ad mất khoảng 5–7 s. Dùng `scripts/bench.ps1` để đo lại (ghi log GPU bằng `nvidia-smi` và CPU bằng `typeperf`).

### Máy i5-12400F + AMD Radeon RX 6600

Encoder `h264_amf` đã được chỉnh cho RX 6600 (RDNA2, VCN 3.0): constant QP, profile High + CABAC, đưa thẳng khung hình nv12 vào encoder. i5-12400F có 6 nhân / 12 luồng và không có iGPU (không dùng được QSV). Các bước khử nhiễu, phóng và làm nét chạy trên CPU, nên số slot × số thread được đặt bằng đúng 12 luồng của CPU. Máy này có 16 GB RAM nhưng thường chỉ trống khoảng 3 GB. Mỗi ffmpeg 1080p dùng tối đa khoảng 350 MB, nên chạy 4 slot (~1,4 GB) thay vì 6 slot (~2,2 GB, quá sát với RAM còn trống). Trong `.env` của máy đó, đặt:

```ini
ENCODER=amf
FFMPEG_SLOTS=4
THREADS_PER_JOB=3
WORKER_MAX_JOBS=12
```

Ước tính khoảng 400–450 ads/giờ (con số này chưa đo). Nếu máy này chỉ là worker phụ của máy chính thì chỉ chạy encode worker, vì riêng Docker Desktop (WSL2) đã cần 1–2 GB RAM.

#### Chạy độc lập (tunnel riêng `upscaler.qhuy204.id.vn`)

Trên máy này port 8080 đã bị Apache chiếm, nên API chạy trong Docker và publish ra `127.0.0.1:8081` (service `api`, profile `api`). Encode worker vẫn chạy trên Windows host để dùng được AMF. Upload multipart được đưa qua R2 (`MULTIPART_VIA_STORAGE=true`, compose tự đặt), vì worker trên host không đọc được ổ đĩa của container, nên bắt buộc `STORAGE_BACKEND=r2`.

```powershell
cd C:\path\to\Upscaler
docker compose --env-file .env -f deploy\docker-compose.yml --profile api up -d --build
curl http://127.0.0.1:8081/healthz
venv\Scripts\python -m server.run_worker      # chạy thử encode worker trên host (AMF), Ctrl+C để dừng
```

Cho worker tự chạy khi đăng nhập (PowerShell chạy bằng quyền Administrator, trong thư mục project):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-tasks.ps1 -Only worker
Start-ScheduledTask Upscale-worker
```

Task tự chạy lại worker nếu nó thoát; log ở `logs\`. Docker Desktop cần bật "Start Docker Desktop when you sign in" để API và Redis cũng tự lên.

`PUBLIC_BASE_URL=https://upscaler.qhuy204.id.vn` trong `.env`. Ingress cloudflared trỏ vào `http://127.0.0.1:8081`. Đổi port bằng biến `API_PORT` trong `.env`.

Cần driver AMD Adrenalin mới (có `amfrt64.dll`). Kiểm tra bằng `venv\Scripts\python scripts\upscale.py input\ad.mp4 --encoder amf`: log phải ghi encoder `amf` và không bị fallback sang `x264`. Sau đó chạy `scripts\bench.ps1` để chỉnh lại `FFMPEG_SLOTS`. Trong file `-cpu.csv` có cột GPU Video Encode: nếu cột này gần 100% thì giảm số slot, nếu CPU chưa đầy thì tăng.

## CLI (không cần server)

```powershell
venv\Scripts\python scripts\upscale.py input\ad.mp4 -o output\ad_1080p.mp4 --platform tiktok
venv\Scripts\python scripts\upscale.py input\ad.mp4 --aspect 9:16 --fit pad --encoder auto
venv\Scripts\python scripts\upscale.py input\ad.mp4 --compare     # so sánh trước/sau
```

## Kiểm thử

```powershell
venv\Scripts\python -m pytest          # unit + API test (fakeredis, không cần Redis thật)
venv\Scripts\python tools\e2e.py --base http://127.0.0.1:8080 --key upk_... --secret whsec_...
venv\Scripts\python tools\drive_review.py <link folder Drive>   # trang so sánh trước/sau
```

Không có Docker: chạy `tools/fake_redis.py` và `tools/fake_worker.py` để test local (không dùng cho production).

## Cấu trúc thư mục

```
upscale_core/     ffmpeg: probe, tính kích thước/tỉ lệ, filter, chọn encoder, chạy có tiến độ
server/           FastAPI (api, models, auth, fetch, storage, webhook), worker arq, dashboard
scripts/          upscale.py (CLI), bench.ps1
tools/            manage_keys, setup_r2, e2e, drive_review, fake_redis, fake_worker
deploy/           docker-compose, Dockerfile, script Task Scheduler, cấu hình cloudflared
docs/API.md       tài liệu API cho người gọi
tests/            pytest
```

## Bảo mật

- API key chỉ lưu dạng hash SHA-256 trong Redis. Key có quota riêng, có thể giới hạn IP. Sai key 20 lần trong 10 phút thì IP bị chặn 15 phút.
- Khi tải URL, server chặn IP nội bộ, loopback và link-local (chống SSRF) và giới hạn dung lượng. ffmpeg chỉ đọc file local.
- Redis và dashboard chỉ nghe trên `127.0.0.1`. Ra internet chỉ có API, đi qua Cloudflare Tunnel.
- Kết quả tải qua link R2 có chữ ký, hết hạn sau 24h.
