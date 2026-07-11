# Vercel Blob MVP Implementation Plan

## Muc tieu

Ban MVP nay bo upload file voice qua Vercel Function de tranh loi `413 Content Too Large`, nhung van giu kha nang chay local nhu hien tai.

Kien truc sau thay doi:

1. Frontend upload file voice truc tiep len Vercel Blob.
2. Frontend goi `/generate` voi `voice_sample_url`.
3. Backend tai file tu URL ve `job_dir` roi tiep tuc pipeline hien co.
4. Local dev van co the fallback sang `multipart/form-data` upload truc tiep.

## Quyet dinh ky thuat

1. Storage:
   - `Vercel Blob`
2. Giai doan trien khai:
   - `MVP`: backend download tu URL
3. Kha nang chay local:
   - Co
   - Ho tro ca `voice_sample_url` va upload/local fallback

## Pham vi

Hai mode input cho voice sample:

1. `Production/Vercel`
   - frontend upload thang len Blob
   - `/generate` nhan `voice_sample_url`
2. `Local/backward compatible`
   - frontend van co the gui `voice_sample` qua `multipart/form-data`

## Thiet ke API

### 1. `POST /api/blob/upload`

Route Node rieng cho Vercel Blob client upload.

Trach nhiem:

1. Sinh client upload token qua `handleUpload`
2. Gioi han content-type cho file audio
3. Nhan webhook `onUploadCompleted`

### 2. `GET /app-config`

Tra ve config runtime de frontend quyet dinh co dung Blob upload hay fallback multipart khong.

Response du kien:

```json
{
  "use_blob_upload": true,
  "blob_upload_url": "/api/blob/upload"
}
```

### 3. `POST /generate`

Ho tro 2 kieu request:

1. `application/json`
   - co `voice_sample_url`
2. `multipart/form-data`
   - co `voice_sample`

Rule validate:

1. Phai co `voice_sample_url` hoac `voice_sample`
2. Neu co ca hai, uu tien `voice_sample_url`
3. Render config van giu nguyen cac field hien tai

Request JSON du kien:

```json
{
  "youtube_url": "https://youtube.com/...",
  "voice_sample_url": "https://....public.blob.vercel-storage.com/voice.mp3",
  "voice_sample_filename": "voice.mp3",
  "xtts_segment_max_chars": 250,
  "xtts_segment_min_chars": 80,
  "gpu_backend": "modal",
  "modal_app_name": "tooltucode-gpu-v1",
  "modal_tts_gpu": "L4",
  "tts_concurrency": 4,
  "tts_parallel_backend": "process",
  "modal_xtts_dispatch": "spawn",
  "modal_xtts_artifact_volume": "tooltucode-xtts-artifacts",
  "modal_xtts_artifact_prefix": "xtts-jobs",
  "modal_xtts_download_workers": 8
}
```

## Cac thay doi backend

Trong `main.py`:

1. Them request model moi cho `/generate` dang JSON
2. Tach logic tao `render_config` ra helper rieng
3. Doi `/generate` thanh endpoint linh hoat:
   - `multipart/form-data`: giu flow cu
   - `application/json`: dung `voice_sample_url`
4. Them helper:
   - download voice sample tu URL ve `job_dir`
   - validate scheme `http/https`
   - timeout va xu ly loi ro rang

## Cac thay doi pipeline

Trong `pipeline_steps/orchestrator.py`:

1. Giu pipeline hien co o muc don gian nhat
2. Backend se chuan bi local file truoc khi submit background job
3. Step 3 van nhan `voice_sample_path` nhu cu

Dieu nay giup MVP it thay doi va it rui ro.

## Cac thay doi frontend

Trong `static/js/job-form.js`:

1. Neu `use_blob_upload=true`
   - lay file tu input
   - upload truc tiep len Vercel Blob
   - goi `/generate` bang JSON voi `voice_sample_url`
2. Neu `use_blob_upload=false`
   - dung flow cu `FormData -> /generate`

Can bo sung:

1. Trang thai UI:
   - `Uploading voice sample...`
   - `Voice upload complete. Creating job...`
2. Xu ly loi upload rieng biet voi loi tao job

## Cau hinh runtime

Them `GET /app-config` de frontend tu dong biet:

1. Vercel co bat Blob upload khong
2. Upload handle URL la gi

Khong can hardcode theo hostname.

## Bien moi truong

1. `BLOB_READ_WRITE_TOKEN`
   - duoc Vercel Blob tao cho project
2. `USE_BLOB_UPLOAD`
   - bat flow upload truc tiep tren Vercel

Khuyen nghi:

1. `USE_BLOB_UPLOAD=1` tren Vercel
2. local de mac dinh `0`
3. chi bat `use_blob_upload=true` neu `BLOB_READ_WRITE_TOKEN` ton tai trong runtime

## VerceI routing

Can bo sung route Node rieng cho Blob upload:

1. `api/blob/upload.js` dung `@vercel/node`
2. `api/index.py` tiep tuc dung `@vercel/python`
3. `vercel.json` uu tien route `/api/blob/upload` truoc route catch-all sang Python

## Ke hoach trien khai theo buoc

1. Them plan nay vao `docs/`
2. Them `GET /app-config`
3. Refactor `/generate` ho tro ca JSON va multipart
4. Them helper tai voice sample tu URL ve `job_dir`
5. Them Node route `api/blob/upload.js`
6. Cap nhat `vercel.json` de support ca Node route va Python route
7. Cap nhat frontend de tu chon Blob upload hoac multipart fallback
8. Kiem tra local syntax va diff

## Rui ro con lai sau MVP

MVP nay giai quyet dung loi `413` va giu local fallback, nhung chua bien toan bo pipeline thanh kien truc serverless-dung-nghia.

Van con cac rui ro:

1. Background thread/job state local khong ly tuong tren Vercel
2. File result va status dang dua vao filesystem local
3. Modal moi duoc dung cho XTTS step 3, chua la worker orchestration end-to-end

## Buoc tiep theo sau MVP

1. Chuyen backend thanh orchestration mong hon
2. Day viec download `voice_sample_url` vao Modal worker
3. Tach job state va result khoi local filesystem neu can production hoa tren Vercel
