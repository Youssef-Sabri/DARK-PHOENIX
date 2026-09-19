# Dark Phoenix: Production Deployment Guide (`DEPLOYMENT.md`)

> **Assignment**: LUNARTECH Advanced Research — DevOps & Full-Stack Deployment Task  
> **Source Repository**: [`https://github.com/LUNARTECH-X/DARK-PHOENIX`](https://github.com/LUNARTECH-X/DARK-PHOENIX)  
> **Assigned Video**: [`https://www.youtube.com/watch?v=YRvf00NooN8`](https://www.youtube.com/watch?v=YRvf00NooN8)  

---

## 1. Live Deployment URLs & Endpoints

| Service | Location / URL | Description |
|---|---|---|
| **Web Frontend** | [`https://dark-phoenix-snowy.vercel.app`](https://dark-phoenix-snowy.vercel.app) | Next.js 15 App with Auth, reviewer account, and Inngest trigger |
| **Backend (Hugging Face Space)** | [`https://y0sf-dark-phoenix-backend.hf.space`](https://y0sf-dark-phoenix-backend.hf.space) | Pure-CPU & ZeroGPU-Ready FastAPI webhook + Gradio UI |
| **Backend (Modal)** | `https://<workspace>--ai-podcast-clipper-process-video.modal.run` | Serverless NVIDIA L40S GPU endpoint |
| **PostgreSQL Database** | Supabase Postgres (`aws-0-us-east-1.pooler.supabase.com:6543`) | Managed Postgres storing users, accounts, uploaded files, and clips via IPv4 Supavisor pooler |
| **Object Storage** | Supabase Storage (`s3://dark-phoenix`) via S3 Gateway | Stores uploaded source videos and rendered clips |

---

## 2. Environment Variable Matrix

All environments share a single conceptual configuration schema.

| Variable Name | Required By | Secret? | Description / Example Value |
|---|---|---|---|
| `DATABASE_URL` | Frontend | Yes | Connection pooling Postgres string: `postgresql://postgres:[PASSWORD]@db.[PROJECT].supabase.co:5432/postgres` |
| `NEXTAUTH_SECRET` | Frontend | Yes | Random 32+ character string for NextAuth session encryption |
| `NEXTAUTH_URL` | Frontend | No | Production URL of deployed frontend (`https://...vercel.app`) |
| `BASE_URL` | Frontend | No | Base application URL for redirects and webhooks |
| `AUTH_DISCORD_ID` | Frontend | Yes | Discord OAuth application client ID |
| `AUTH_DISCORD_SECRET` | Frontend | Yes | Discord OAuth application client secret |
| `STRIPE_SECRET_KEY` | Frontend | Yes | Stripe secret API key (`sk_test_...` or `sk_live_...`) |
| `STRIPE_WEBHOOK_SECRET` | Frontend | Yes | Stripe webhook endpoint signing secret (`whsec_...`) |
| `INNGEST_EVENT_KEY` | Frontend | Yes | Inngest event emission key |
| `INNGEST_SIGNING_KEY` | Frontend | Yes | Inngest webhook signature validation key |
| `AWS_ACCESS_KEY_ID` | Frontend & Backend | Yes | S3 / Supabase Storage S3 Gateway access key |
| `AWS_SECRET_ACCESS_KEY` | Frontend & Backend | Yes | S3 / Supabase Storage S3 Gateway secret key |
| `AWS_REGION` | Frontend & Backend | No | Storage region (e.g., `us-east-1`) |
| `AWS_ENDPOINT_URL_S3` | Frontend & Backend | No | Custom S3 endpoint URL (`https://[PROJECT].supabase.co/storage/v1/s3`) |
| `S3_BUCKET_NAME` | Frontend & Backend | No | Name of the bucket (e.g., `dark-phoenix`) |
| `PROCESS_VIDEO_ENDPOINT` | Frontend | No | Backend endpoint URL (`https://y0sf-dark-phoenix-backend.hf.space/process_video` or Modal URL) |
| `PROCESS_VIDEO_ENDPOINT_AUTH` | Frontend & Backend | Yes | Shared secret token passed as `Authorization: Bearer <TOKEN>` |
| `GEMINI_API_KEY` | Backend | Yes | Google AI Studio API key for highlight moment selection |
| `GEMINI_MODEL` | Backend | No | Gemini model name: `gemini-3.1-flash-lite` |

---

## 3. Database Setup (Supabase PostgreSQL)

1. **Create Supabase Project**: Created project under Supabase dashboard with PostgreSQL 15+.
2. **Push Schema**:
   From `ai-podcast-clipper-frontend/`:
   ```bash
   npx prisma db push
   ```
   This generates tables:
   - `User`, `Account`, `Session`, `VerificationToken` (NextAuth)
   - `UploadedFile` (source video metadata, status, YouTube URL tracking)
   - `Clip` (detected moments, start/end timestamps, S3 keys, aspect ratios)

3. **Verify Connection**:
   ```bash
   npx prisma studio
   ```

---

## 4. Object Storage Setup (Supabase Storage S3 Gateway)

1. **Bucket Creation**: Create private bucket named `dark-phoenix`.
2. **S3 Gateway Credentials**: Generate S3 Access Key ID and Secret Access Key from Supabase Dashboard ➔ Project Settings ➔ Storage ➔ S3 Access Keys.
3. **CORS Policy Configuration**:
   ```json
   [
     {
       "AllowedHeaders": ["Content-Type", "Content-Length", "Authorization"],
       "AllowedMethods": ["PUT", "GET", "HEAD"],
       "AllowedOrigins": ["*"],
       "ExposeHeaders": ["ETag"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

---

## 5. Workflow Queue (Inngest Cloud)

1. **Sign in to Inngest**: Connect GitHub account at [inngest.com](https://www.inngest.com/).
2. **App Registration**: Register application pointing to `https://<YOUR_DEPLOYED_FRONTEND>/api/inngest`.
3. **Event Verification**:
   - Event `process.video` triggers the background clipping pipeline.
   - Webhook calls `POST $PROCESS_VIDEO_ENDPOINT` with Bearer auth.
   - Updates Prisma database with completed clip metadata upon return.

---

## 6. Reviewer Test Account Guide

To evaluate the system without requiring credit card transactions or Stripe payments:

- **Email**: `reviewer@lunartech.ai`
- **Password**: `LunarReviewer2026!`
- **Credit Balance**: 100 Credits (Pre-seeded directly in Postgres database `User.credits = 100`).
- **Bypass Mechanism**: For review evaluation, video submissions do not deduct real funds; the backend processes full-length files directly.

---

## 7. Burned-in LunarTech Logo Watermark Implementation

Per requirements, the watermark is **permanently rendered into the video frame stream** via FFmpeg overlay using the authentic LunarTech logo asset (`assets/lunartech-logo.png`):

```bash
ffmpeg -y -i input_clip.mp4 -i assets/lunartech-logo.png \
  -filter_complex "[0:v]ass=subtitles.ass[subtitled];[1:v]scale=360:-1,format=rgba,colorchannelmixer=aa=0.78,pad=iw+32:ih+24:16:12:color=black@0.32[watermark];[subtitled][watermark]overlay=W-w-40:40:format=auto[video]" \
  -map "[video]" -map "0:a?" \
  -c:v libx264 -crf 23 -preset fast -pix_fmt yuv420p \
  -c:a copy -movflags +faststart \
  output_clip.mp4
```

### Parameters:
- **Asset**: `assets/lunartech-logo.png` (authentic LunarTech branding logo).
- **Position**: Upper-right safe margin (`overlay=W-w-40:40`).
- **Scaling & Opacity**: Scaled to 360px width with 78% alpha opacity (`aa=0.78`).
- **Background Box**: Semi-transparent black box (`black@0.32`) with padding to guarantee readability across light and dark backgrounds.

---

## 8. Reproduction & Verification Guide

### Option A: Hugging Face Space (Live Cloud Webhook & Gradio UI)
The Space is live, operational, and publicly verifiable at [`https://y0sf-dark-phoenix-backend.hf.space`](https://y0sf-dark-phoenix-backend.hf.space).

#### 1-Clip Fast Pipeline Verification (Verified in 155s):
You can trigger a single-clip run via Python client to verify the pipeline with minimal compute consumption:
```python
from gradio_client import Client

client = Client("https://y0sf-dark-phoenix-backend.hf.space")
job = client.submit(
    s3_key="uploads/test-run/original.mp4",
    max_clips=1,
    api_name="/gradio_process_action"
)
print(job.result())
```
**Confirmed Test Run Results:**
- **Status**: 100% Completed (`Clips Selected: 1, Clips Processed: 1, Clips Failed: 0`)
- **Total Execution Time**: 155.7 seconds
- **Output Artifact**: Stored at `uploads/test-run/clip_0.mp4` in Supabase Storage with Anton subtitles and LunarTech logo watermark.
- **Transcript Cache**: Cached at `uploads/test-run/original_transcript.json` to accelerate future runs.

#### Webhook Ingestion (`POST /process_video`):
```bash
curl -X POST https://y0sf-dark-phoenix-backend.hf.space/process_video \
  -H "Authorization: Bearer $PROCESS_VIDEO_ENDPOINT_AUTH" \
  -H "Content-Type: application/json" \
  -d '{"s3_key": "uploads/test-run/original.mp4", "max_clips": 1}'
```

---

### Option B: Canonical Modal Backend CLI Deployment
```bash
cd ai-podcast-clipper-backend
modal setup
python setup_modal_secret.py
modal deploy main.py
```

---

## 9. 100% Free Pure-CPU Architecture & Zero Quota Constraints
To guarantee zero-failure execution and completely eliminate GPU quota exhaustion:
1. **Pure-CPU Execution**: TalkNet ASD (S3FD face detector + TalkNet model) and local WhisperX (int8) execute entirely on CPU (**0 GPU seconds consumed**), eliminating ZeroGPU daily limits.
2. **Smart Framing Fallback**: If no active speaker face is detected in a scene (e.g. B-roll footage), the engine seamlessly creates a 9:16 blurred-background vertical composition with styled Anton subtitles and LunarTech logo watermark.
3. **Hardware Encoder Fallback**: Automatically falls back from `VideoWriterNV` to CPU `VideoWriter` when running without an attached NVENC encoder.
4. **S3 Transcription Caching**: WhisperX transcriptions are saved to Supabase S3 (`<name>_transcript.json`), avoiding repeated audio transcription compute.
5. **Automated Keep-Alive**: [`.github/workflows/keep-alive.yml`](.github/workflows/keep-alive.yml) pings `GET /health` every 48 hours to prevent space sleep mode.
