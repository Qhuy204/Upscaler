# Upscale API v1

Upscale video quảng cáo lên 1080p (ffmpeg: khử block/nhiễu → lanczos → làm nét → NVENC H.264), ép đúng tỉ lệ khung hình mà TikTok/Meta chấp nhận.

- Base URL: `https://upscale.qhuy204.id.vn`
- Xác thực: header `Authorization: Bearer upk_...` (xin key từ admin; mỗi key đi kèm một `webhook_secret` dạng `whsec_...`)
- Body JSON, tối đa 256 KB. Mọi thời gian là Unix timestamp (giây).
- Video gốc và kết quả **tự xoá sau 24h** kể từ khi upload/tạo ra. Tải kết quả về trong thời gian đó.

## Luồng cơ bản

```bash
# 1. tạo job
curl -X POST https://upscale.qhuy204.id.vn/v1/jobs \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"source_url": "https://drive.google.com/file/d/<id>/view", "options": {"platform": "tiktok"}}'
# → 202 {"job_id": "...", "status": "queued", ...}

# 2. hỏi trạng thái (mỗi 2–5 s) hoặc dùng callback_url
curl -H "Authorization: Bearer $KEY" https://upscale.qhuy204.id.vn/v1/jobs/<job_id>

# 3. khi status = "done": tải output.url (link R2 có chữ ký, KHÔNG gửi header Authorization)
curl -o out.mp4 "<output.url>"
```

## Endpoints

| Method | Path | Mô tả |
|---|---|---|
| POST | `/v1/jobs` | Tạo 1 job (JSON hoặc multipart) |
| POST | `/v1/batches` | Tạo nhiều job: danh sách URL hoặc 1 folder Google Drive |
| POST | `/v1/uploads` | Lấy link PUT để upload file lớn thẳng lên R2 |
| GET | `/v1/jobs/{job_id}` | Trạng thái + link tải kết quả |
| GET | `/v1/jobs?limit=50&offset=0` | Các job của key này, mới nhất trước (limit ≤ 200) |
| DELETE | `/v1/jobs/{job_id}` | Huỷ job đang chờ / đang chạy |
| GET | `/v1/stats` | Độ dài hàng đợi, số job đang chạy, quota của bạn |
| GET | `/healthz` | Không cần key. `{"ok": true, "redis": true, "worker": true}` |

### POST /v1/jobs — từ URL hoặc upload_key

```json
{
  "source_url": "https://...",          // hoặc "upload_key": "inputs/..." (chọn đúng 1)
  "options": { ... },                   // xem bảng options, có thể bỏ trống
  "callback_url": "https://your.server/hook",   // tuỳ chọn
  "idempotency_key": "ad-123"           // tuỳ chọn, 1–128 ký tự
}
```

- `source_url`: http/https công khai. Hỗ trợ link file Google Drive (`/file/d/<id>/view`, `open?id=`, `uc?id=`) — file phải share "Anyone with the link". Link **folder** Drive phải dùng `/v1/batches`.
- Trả **202** với job mới, hoặc **200** với job cũ nếu `idempotency_key` đã dùng (gọi lại an toàn khi mất kết nối).

### POST /v1/jobs — upload trực tiếp (multipart, ≤ 95 MB)

```bash
curl -X POST https://upscale.qhuy204.id.vn/v1/jobs -H "Authorization: Bearer $KEY" \
  -F file=@ad.mp4 -F 'options={"platform":"tiktok"}' \
  -F callback_url=https://your.server/hook -F idempotency_key=ad-123
```

File > 95 MB (giới hạn của Cloudflare) dùng `/v1/uploads`.

### POST /v1/uploads — file lớn (≤ 300 MB)

```bash
curl -X POST https://upscale.qhuy204.id.vn/v1/uploads -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" -d '{"filename": "ad.mp4", "content_type": "video/mp4"}'
# → {"upload_key": "inputs/k_.../abc.mp4", "upload_url": "https://...r2...", "method": "PUT",
#    "headers": {"Content-Type": "video/mp4"}, "expires_in": 3600, "max_bytes": 314572800}

curl -X PUT -H "Content-Type: video/mp4" --data-binary @ad.mp4 "<upload_url>"
# rồi POST /v1/jobs với {"upload_key": "inputs/k_.../abc.mp4"}
```

Gửi đúng `headers` được trả về; link PUT hết hạn sau 1 giờ.

### POST /v1/batches — nhiều video

```json
{ "drive_folder_url": "https://drive.google.com/drive/folders/<id>", "options": {...},
  "callback_url": "...", "idempotency_key": "campaign-7" }
```
hoặc `{"source_urls": ["https://...", "https://..."], ...}` (chọn đúng 1 trong 2).

- Folder Drive phải công khai; lấy mọi file video trong folder, kể cả folder con.
- Tối đa 100 video/batch. Mỗi video thành 1 job riêng (callback riêng); `idempotency_key` của batch được nối thêm id từng file.
- Trả `{"count": N, "jobs": [ <job>, ... ]}`.

## Options

| Field | Mặc định | Ý nghĩa |
|---|---|---|
| `platform` | `"tiktok"` | Ép tỉ lệ đầu ra. `tiktok`: chỉ 16:9, 9:16, 1:1. `meta`: 9:16, 4:5, 1:1, 16:9. `any`: giữ tỉ lệ gốc |
| `aspect` | – | Ép tỉ lệ đầu ra, ví dụ `"9:16"`, `"1:1"`, `"16:9"` (`meta` thêm `"4:5"`; `any` nhận mọi tỉ lệ từ 1:5 đến 5:1). Bỏ trống = tỉ lệ cho phép gần nhất. Tỉ lệ platform không nhận → 422. Không dùng chung với `to` |
| `short` | `1080` | Cạnh ngắn đầu ra (144–4320). 16:9 → 1920×1080, 9:16 → 1080×1920, 1:1 → 1080×1080 |
| `to` | – | Kích thước chính xác `"WxH"`, bỏ qua platform |
| `scale` | – | Hệ số phóng (0.1–4). Chỉ dùng 1 trong `short` / `to` / `scale` |
| `fit` | `"auto"` | Cách lấp khung khi tỉ lệ lệch: `auto`, `pad` (viền đen), `crop` (cắt), `stretch` (kéo giãn) |
| `quality` | `"delivery"` | `delivery`: file nhẹ cho ads. `master`: chất lượng cao, file nặng hơn nhiều |
| `crf` | – | Ghi đè mức nén (10–40, thấp = đẹp hơn/nặng hơn) |
| `strength` | `1.0` | Mức khử nhiễu + làm nét (0–2) |
| `remove_borders` | `true` | Cắt viền đen có sẵn trong video gốc trước khi xử lý |
| `snap_aspect` | `true` | Chỉ với `platform: "any"`: tỉ lệ gần chuẩn (lệch ≤ 8%) được làm tròn, ví dụ 1920×1033 → 1920×1080 |

Không có `aspect`, tỉ lệ đầu ra là tỉ lệ cho phép **gần nhất** với video gốc. Với `tiktok`: rộng/cao ≤ 3:4 → 9:16 (1080×1920); giữa 3:4 và 4:3 (gồm 4:5, 5:4) → 1:1 (1080×1080); ≥ 4:3 → 16:9 (1920×1080). Có `aspect` thì luôn ra đúng tỉ lệ đó, ví dụ `{"aspect": "9:16", "fit": "pad"}` với video 16:9 → 1080×1920 có viền đen trên dưới. `fit: "auto"`: lệch ≤ 12% thì vừa kéo giãn vừa cắt nhẹ (không viền đen, mắt thường khó thấy); lệch hơn thì thêm viền đen. Ví dụ với `tiktok`: 1920×1146 → 1920×1080, 1080×1350 → 1080×1080 (có viền), 640×360 → 1920×1080.

Với `tiktok`, bitrate tối thiểu 600 kbps (TikTok từ chối < 516 kbps); video quá tĩnh sẽ tự encode lại.

## Job

```json
{
  "job_id": "j_...",
  "source_type": "url",                 // url | upload | local (multipart)
  "source_url": "https://...",
  "status": "done",
  "progress": 100,
  "filename": "ad.mp4",
  "options": {"platform": "tiktok"},
  "input":  {"width": 640, "height": 360, "duration": 32.1, "fps": 25, "has_audio": true,
             "bytes": 1058502, "borders_removed": null},
  "output": {
    "url": "https://...r2.cloudflarestorage.com/...",   // tải trực tiếp, không cần key
    "expires_in": 86000,                 // giây link còn hiệu lực
    "deleted_at": 1794280000,            // lúc file bị xoá
    "filename": "ad_1080p.mp4",
    "width": 1920, "height": 1080, "bytes": 8245600, "kbps": 2056,
    "encoder": "nvenc", "fit": "blend"
  },
  "attempts": 1, "error": null,
  "created_at": ..., "started_at": ..., "finished_at": ...,
  "webhook_status": "delivered"
}
```

- `status`: `queued` → `downloading` → `processing` → `uploading` → `done` | `failed` | `canceled`.
- `output` chỉ có khi `done`. Mỗi lần GET tạo link mới; hết 24h thì `output.url = null`, `output.expired = true`.
- `failed`: xem `error` (ví dụ không phải video, dài hơn 10 phút, cạnh > 4096, link không tải được). Lỗi tạm (mạng, R2) được tự thử lại tối đa 2 lần trước khi báo failed.
- Thông tin job được giữ 7 ngày.

## Webhook

Khi job kết thúc (`done` / `failed` / `canceled`), server `POST` tới `callback_url`:

- Body: đúng JSON của job như trên.
- Headers: `X-Upscale-Event: job.done`, `X-Upscale-Timestamp: <unix>`, `X-Upscale-Signature: sha256=<hex>`
- Chữ ký = `HMAC-SHA256(webhook_secret, "<timestamp>." + raw_body)`. Kiểm tra trên body gốc (chưa parse) và từ chối nếu timestamp lệch > 5 phút.
- Trả 2xx trong 15 s. Nếu không, thử lại sau 10 s, 30 s, 2 phút, 10 phút, 30 phút.
- `callback_url` phải là địa chỉ công khai (IP nội bộ bị từ chối).

```python
import hashlib, hmac, time

def verify(secret: str, ts: str, body: bytes, sig: str) -> bool:
    if abs(time.time() - int(ts)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
```

## Giới hạn

| | |
|---|---|
| Video | ≤ 300 MB, ≤ 10 phút, cạnh ≤ 4096 px, có stream video |
| Multipart | ≤ 95 MB |
| Batch | ≤ 100 video |
| Job chưa xong mỗi key | 200 (vượt → 429, header `Retry-After`) |
| Job chạy cùng lúc mỗi key | 8 (phần còn lại xếp hàng, không lỗi) |
| Request mỗi key | 600 / phút (vượt → 429) |
| Sai key | 20 lần / 10 phút từ 1 IP → IP bị chặn 15 phút |
| Throughput toàn hệ thống | ~690 ads/giờ (ad 30–50 s) |

## Mã lỗi

| HTTP | Khi nào |
|---|---|
| 401 | Thiếu / sai API key |
| 403 | Key bị giới hạn IP và IP của bạn không nằm trong danh sách |
| 404 | Job không tồn tại hoặc thuộc key khác; `upload_key` không phải của bạn |
| 409 | Huỷ job đã kết thúc |
| 413 | File quá lớn |
| 422 | Body sai (field lạ, thiếu nguồn, `source_url` bị chặn / là folder Drive, folder rỗng) |
| 429 | Vượt quota hoặc rate limit; đợi theo `Retry-After` |
| 502 | Không đọc được folder Drive |

Body lỗi: `{"detail": "<thông báo>"}` (422 do validate: `detail` là danh sách lỗi theo field).
