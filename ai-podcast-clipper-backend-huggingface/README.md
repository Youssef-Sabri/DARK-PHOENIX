---
title: Dark Phoenix Backend
emoji: 🔥
colorFrom: red
colorTo: yellow
sdk: gradio
sdk_version: 5.20.0
app_file: app.py
pinned: false
---

# Dark Phoenix Clipper Backend (Pure CPU & ZeroGPU-Ready)

This Space hosts the production AI clipping backend for Dark Phoenix, optimized to run **100% free on CPU** with optional ZeroGPU acceleration.

### Key Features:
- **Local WhisperX (int8 on CPU)**: Fast, memory-efficient word-level speech transcription and phoneme alignment without consuming GPU quotas or external transcription API costs.
- **S3 Transcript Caching**: Automatically persists transcriptions to `<video>_transcript.json` in Supabase Storage, bypassing repeated transcriptions for previously processed videos.
- **Google Gemini 3.1 Flash Lite**: Intelligent highlight selection and structured viral moment extraction.
- **TalkNet ASD on CPU**: Active-speaker detection and continuous face tracking adapted for PyTorch CPU execution.
- **FFmpeg 9:16 Vertical Reframing**: Dynamic face tracking framing (1080×1920) centered on the active speaker.
- **Burned-in Anton Subtitles & LunarTech Watermark**: Styled ASS captions using the Anton font and authentic LunarTech logo image watermark (`assets/lunartech-logo.png`) baked directly into the video bytes.
- **S3 / Supabase Storage Integration**: Direct download and multipart upload via custom S3 API gateways.
- **ZeroGPU Probe**: Built-in startup probe satisfying Hugging Face ZeroGPU supervisor checks.

### Endpoints:
- `POST /process_video`: Authenticated webhook endpoint called by Inngest Cloud and Next.js (`Authorization: Bearer <TOKEN>`).
- `GET /health`: Health status probe.
- `GET /`: Interactive Gradio monitoring interface for real-time manual testing and live streaming milestone logs.
