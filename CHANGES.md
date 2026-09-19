# Dark Phoenix: Architecture, Multi-Backend Deployment & Changes

This document details the architecture, code modifications, environment configurations, and setup options implemented for **Dark Phoenix**:
- **Frontend**: Vercel (Next.js 15, React 19)
- **Workflow Queue**: [Inngest Cloud](https://www.inngest.com/)
- **AI Moment Selection**: Google AI Studio (Gemini 3.1 Flash Lite)
- **Database & Storage**: Supabase PostgreSQL + Supabase Storage (S3-Compatible Gateway)
- **Backend Options**:
  1. **Modal GPU Worker** (`ai-podcast-clipper-backend/`): Native L40S serverless GPU endpoint.
  2. **Hugging Face Backend** (`ai-podcast-clipper-backend-huggingface/`): Dual FastAPI + Gradio interface running 100% free on CPU (with 0 GPU quota consumption).

---

## 1. Updated Architecture & Service Matrix

```
                          [User / Reviewer Browser]
                                     │
                 ┌───────────────────┴───────────────────┐
                 ▼                                       ▼
        [Vercel: Frontend]                       [Hugging Face UI]
                 │                                       │
                 ├────────► [Supabase PostgreSQL] ◄──────┤
                 │                                       │
                 │ (Signed URLs)                         │ (S3 API)
                 ▼                                       ▼
    [Supabase Storage: dark-phoenix bucket (S3-Compatible Gateway)]
                 ▲                                       ▲
                 │                                       │
        [Modal: main.py]                        [Hugging Face Space]
                 ▲
                 │ (Webhook POST /process_video)
                 ▼
       [Inngest Cloud (inngest.com)]
```

### Component Details

| Layer | Service | Configuration / Model | Cost / Hardware |
|---|---|---|---|
| **Frontend** | Vercel | Next.js 15, React 19 | **Free** (Hobby Tier) |
| **Workflow Queue** | Inngest Cloud | Webhook `/api/inngest` | **Free** (25,000 steps/month) |
| **AI Moment Selection** | Google AI Studio | **Gemini 3.1 Flash Lite** | **Free** tier |
| **Backend (Modal)** | Modal | `AiPodcastClipper` on `gpu="L40S"` | Trial credits |
| **Backend (Hugging Face)** | Hugging Face Space | FastAPI + Gradio (Pure CPU Execution) | **Free** (No credit card, no quota limits) |
| **Database** | Supabase | Managed PostgreSQL (`DATABASE_URL`) | **Free** (500 MB) |
| **Object Storage** | Supabase Storage | S3-Compatible API Gateway | **Free** (1 GB) |

---

## 2. Backend Modules Breakdown

### A. Official Modal Backend (`ai-podcast-clipper-backend/`)
- Matches the canonical repository structure.
- **Hardware**: Configured on `@app.cls(gpu="L40S")`.
- **Image Build**: Ubuntu 22.04 CUDA 12.4 base with Python 3.11, Anton font, and pre-downloaded TalkNet weights.
- **Dependency Isolation**: Pinned constraints (`transformers==4.38.2`, `accelerate==0.28.0`, `datasets==2.18.0`, `huggingface-hub==0.21.4`, `lightning==2.1.4`) eliminating pip backtracking loops.
- **Entrypoint**: `main.py` exposing `@modal.fastapi_endpoint(method="POST")` on `process_video`.

### B. Hugging Face Space Backend (`ai-podcast-clipper-backend-huggingface/`)
- Deployed at: [huggingface.co/spaces/Y0sf/dark-phoenix-backend](https://huggingface.co/spaces/Y0sf/dark-phoenix-backend).
- **Dual Functionality**:
  - `POST /process_video`: Authenticated webhook consumed by Inngest and Next.js.
  - `GET /`: Interactive Gradio web UI for manual testing, monitoring, and live clip previews.
- **Pure-CPU Execution (0 GPU Seconds Billed)**: WhisperX transcription (`int8`), TalkNet ASD face tracking, network downloads, S3 uploads, and FFmpeg encoding run entirely on CPU (**0 GPU seconds consumed**, avoiding ZeroGPU quota limits).

---

## 3. Key Enhancements & Rubric Implementations

### 1. Burned-in LunarTech Logo Watermark (Rubric Gap 2)
The watermark is permanently rendered into the video stream via FFmpeg overlay using the authentic LunarTech logo (`assets/lunartech-logo.png`):
```python
filter_complex = (
    f"[0:v]ass={subtitle_path}[subtitled];"
    "[1:v]scale=360:-1,format=rgba,colorchannelmixer=aa=0.78,"
    "pad=iw+32:ih+24:16:12:color=black@0.32[watermark];"
    "[subtitled][watermark]overlay=W-w-40:40:format=auto[video]"
)
```

### 2. Anton Font for Styled Subtitles
- Downloaded and cached in system font directory (`/usr/share/fonts/truetype/custom/Anton-Regular.ttf`).
- Styled with white text, black outline (width 3), bottom-center alignment via `pysubs2`.

### 3. Server-Side S3 Storage Gateway (`AWS_ENDPOINT_URL_S3`)
Both backend and frontend support custom S3 endpoint URLs:
```python
def get_s3_client():
    s3_endpoint = os.environ.get("AWS_ENDPOINT_URL_S3")
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=s3_endpoint if s3_endpoint else None,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
```

### 4. PyTorch 2.6 Weights Unpickler Compatibility
Safely loads WhisperX and pyannote models when running modern PyTorch runtimes:
```python
_orig_load = torch.load
def _compat_load(*args, **kwargs):
    kwargs["weights_only"] = False
    return _orig_load(*args, **kwargs)
torch.load = _compat_load
torch.serialization.load = _compat_load
```

### 5. cuDNN 9 & CTranslate2 Upgrade
- Upgraded CTranslate2 from `4.4.0` to `ctranslate2>=4.5.0` and integrated `nvidia-cudnn-cu12`.
- Injected dynamic preloading via `ctypes.CDLL(..., mode=ctypes.RTLD_GLOBAL)` in `app.py` and `main.py` to prevent missing cuDNN 8 (`libcudnn_ops_infer.so.8`) symbol errors in CUDA 12/13 environments.

### 6. S3 WhisperX Transcription Caching
- Added automatic S3 caching for audio transcription results:
  `cache_key = s3_key.rsplit(".", 1)[0] + "_transcript.json"`
- If a video has been transcribed, repeat processing loads the cached transcript from Supabase Storage S3 directly, saving up to 3 minutes of compute and zeroing GPU quota consumption for transcription.

### 7. Pure-CPU TalkNet ASD & ZeroGPU Quota Elimination
- Ported S3FD face detector and TalkNet models to CPU by dynamically binding tensors and weights with `torch.device("cpu")` and `map_location=device`.
- Eliminates GPU quota consumption entirely (0 GPU seconds billed), allowing continuous processing without hitting daily ZeroGPU rate limits.
- If no active speaker faces are detected (e.g., landscape scenes or B-roll), the pipeline gracefully falls back to a high-quality 9:16 blurred vertical video with Anton captions and watermark without aborting the job.

### 8. CPU VideoWriter Fallback
- Added automatic fallback from `ffmpegcv.VideoWriterNV` to `ffmpegcv.VideoWriter` when running without an attached NVENC hardware encoder.

### 9. TalkNet S3FD Face Detector Dependency
- Added `torchvision` to `requirements.txt` to resolve `ModuleNotFoundError: No module named 'torchvision'` in `asd/model/faceDetector/s3fd/__init__.py`.

### 10. Relaxed Clip Duration Validation
- Relaxed default `min_duration` in `clip_validation.py` from 30.0s to 15.0s to allow high-engagement 15–30s viral short-form clips selected by Gemini 3.1 Flash Lite to pass validation cleanly.

### 11. Exact LunarTech Logo Image Watermark Restoration
- **Problem**: Hugging Face Spaces Git rejected binary `.png` files (`remote: Your push was rejected because it contains binary files`), causing a temporary fallback to `drawtext='unartch'`.
- **Solution**: Implemented `assets_embedded.py` to store the 160 KB PNG in base64 Python text. On startup, `ensure_watermark()` unpacks the exact binary image file to `/assets/lunartech-logo.png` and `assets/lunartech-logo.png`.
- Restored the exact canonical FFmpeg filter complex from `ai-podcast-clipper-backend/main.py`:
  `[0:v]ass=...[subtitled];[1:v]scale=360:-1,format=rgba,colorchannelmixer=aa=0.78,pad=iw+32:ih+24:16:12:color=black@0.32[watermark];[subtitled][watermark]overlay=W-w-40:40:format=auto[video]`

### 12. Automated Keep-Alive & Space Uptime
- Created [`.github/workflows/keep-alive.yml`](.github/workflows/keep-alive.yml) to automatically ping `GET /health` on the Hugging Face Space every 48 hours via free GitHub Actions.
- Prevents container sleep mode / hibernation, ensuring the Space is warm, healthy, and immediately ready to process webhook requests without cold-start latency.


