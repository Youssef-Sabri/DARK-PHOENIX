# 1. ZeroGPU MUST be imported FIRST before any other library
try:
    import spaces
    has_spaces = True
except ImportError:
    has_spaces = False
    class spaces:
        @staticmethod
        def GPU(duration=180):
            def decorator(fn):
                return fn
            return decorator

if has_spaces:
    @spaces.GPU(duration=1)
    def _zerogpu_startup_probe():
        return True

import os
import sys
import time
import uuid
import json
import shutil
import pathlib
import subprocess
import urllib.request
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
import gradio as gr

# Ensure local backend dir is in sys.path
BASE_DIR = pathlib.Path(__file__).parent.resolve()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Ensure nvidia CUDA & cuDNN shared libraries in site-packages are on LD_LIBRARY_PATH and preloaded
import glob
import ctypes
for p in list(sys.path):
    nvidia_path = os.path.join(p, "nvidia")
    if os.path.isdir(nvidia_path):
        for sub in ("cudnn", "cublas", "cuda_runtime", "cufft", "curand", "cusolver", "cusparse"):
            lib_dir = os.path.join(nvidia_path, sub, "lib")
            if os.path.isdir(lib_dir):
                curr_ld = os.environ.get("LD_LIBRARY_PATH", "")
                if lib_dir not in curr_ld:
                    os.environ["LD_LIBRARY_PATH"] = f"{lib_dir}:{curr_ld}" if curr_ld else lib_dir
                for so in sorted(glob.glob(os.path.join(lib_dir, "*.so*"))):
                    try:
                        ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                    except Exception:
                        pass

import types
import torch
import torch.serialization
_orig_torch_load = torch.load
def _compat_torch_load(*args, **kwargs):
    kwargs["weights_only"] = False
    return _orig_torch_load(*args, **kwargs)
torch.load = _compat_torch_load
torch.serialization.load = _compat_torch_load

try:
    import omegaconf.listconfig
    import omegaconf.dictconfig
    if hasattr(torch.serialization, "add_safe_globals"):
        torch.serialization.add_safe_globals([
            omegaconf.listconfig.ListConfig,
            omegaconf.dictconfig.DictConfig
        ])
except Exception:
    pass

import torchaudio
if not hasattr(torchaudio, "set_audio_backend"):
    torchaudio.set_audio_backend = lambda *args, **kwargs: None
if not hasattr(torchaudio, "get_audio_backend"):
    torchaudio.get_audio_backend = lambda *args, **kwargs: "soundfile"
if not hasattr(torchaudio, "list_audio_backends"):
    torchaudio.list_audio_backends = lambda *args, **kwargs: ["soundfile"]

if "torchaudio.backend" not in sys.modules:
    backend_mod = types.ModuleType("torchaudio.backend")
    backend_common_mod = types.ModuleType("torchaudio.backend.common")
    backend_common_mod.AudioMetaData = getattr(torchaudio, "AudioMetaData", None)
    backend_mod.common = backend_common_mod
    torchaudio.backend = backend_mod
    sys.modules["torchaudio.backend"] = backend_mod
    sys.modules["torchaudio.backend.common"] = backend_common_mod

from main import (
    AiPodcastClipper,
    ProcessVideoRequest,
    auth_scheme
)
from download_model_assets import MODEL_ASSETS, ensure_model_asset

# ---------------------------------------------------------------------------
# Setup & Initialization
# ---------------------------------------------------------------------------

def ensure_environment():
    """Ensure fonts, whisperx, and offline model assets are downloaded and ready."""
    # 0. Ensure whisperx is installed with --no-build-isolation
    try:
        import whisperx
    except ImportError:
        print("Installing whisperx...")
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--no-cache-dir", "--no-build-isolation", "--no-deps",
            "-q", "--root-user-action=ignore", "git+https://github.com/m-bain/whisperx.git@v3.2.0"
        ], check=True)

    # 1. Ensure Anton Font for styled captions
    font_dir = pathlib.Path("/usr/share/fonts/truetype/custom")
    font_path = font_dir / "Anton-Regular.ttf"
    if not font_path.exists():
        try:
            font_dir.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(
                "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
                str(font_path)
            )
            subprocess.run(["fc-cache", "-f"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            print("Anton font cached.")
        except Exception as e:
            print(f"Warning: Anton font setup skipped: {e}")

    # 2. Ensure TalkNet & Face Detector weights exist
    for asset in MODEL_ASSETS:
        try:
            ensure_model_asset(*asset)
        except Exception as e:
            print(f"Warning: Could not pre-download model asset {asset[1]}: {e}")

    # 3. Ensure LunarTech logo watermark asset
    try:
        from assets_embedded import ensure_watermark
        ensure_watermark()
        print("LunarTech watermark asset verified.")
    except Exception as e:
        print(f"Warning: Watermark setup skipped: {e}")

ensure_environment()

# Initialize Clipper singleton
_clipper_instance: Optional[AiPodcastClipper] = None

def get_clipper() -> AiPodcastClipper:
    global _clipper_instance
    if _clipper_instance is None:
        _clipper_instance = AiPodcastClipper()
        _clipper_instance.load_model()
    return _clipper_instance

# ---------------------------------------------------------------------------
# Gradio Interface & Execution Handler
# ---------------------------------------------------------------------------

import queue
import threading

def gradio_process_action(s3_key: str, max_clips: int, youtube_url: str = ""):
    s3_key = s3_key.strip() if s3_key else ""
    youtube_url = youtube_url.strip() if youtube_url else ""

    if not s3_key and not youtube_url:
        yield "⚠️ Please enter an S3 Key or a YouTube Video URL", None, None
        return

    if not s3_key and youtube_url:
        import re
        vid_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", youtube_url)
        vid_id = vid_match.group(1) if vid_match else "custom"
        s3_key = f"uploads/{vid_id}/original.mp4"

    clipper = get_clipper()
    req = ProcessVideoRequest(s3_key=s3_key, max_clips=int(max_clips), youtube_url=youtube_url or None)
    token = HTTPAuthorizationCredentials(scheme="Bearer", credentials=os.environ.get("AUTH_TOKEN", ""))

    log_queue = queue.Queue()
    start_time = time.time()

    def on_status(stage: str, msg: str, level: str = "INFO"):
        t = time.strftime("%H:%M:%S")
        icon = "ℹ️"
        if level == "WARN":
            icon = "⚠️"
        elif level == "ERROR":
            icon = "❌"
        elif level == "SUCCESS":
            icon = "✅"
        line = f"[{t}] {icon} [{stage}] {msg}"
        log_queue.put(("log", line))

    result_holder = {}

    def worker():
        try:
            res = clipper.process_video(req, token=token, status_callback=on_status)
            result_holder["result"] = res
        except Exception as err:
            result_holder["error"] = err
        finally:
            log_queue.put(("done", None))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    accumulated_logs = [f"[{time.strftime('%H:%M:%S')}] 🔥 [Pipeline] Initializing Dark Phoenix processing job..."]
    yield "\n".join(accumulated_logs), None, None

    while True:
        try:
            msg_type, payload = log_queue.get(timeout=0.25)
            if msg_type == "done":
                break
            elif msg_type == "log":
                accumulated_logs.append(payload)
                yield "\n".join(accumulated_logs), None, None
        except queue.Empty:
            if not thread.is_alive() and log_queue.empty():
                break
            continue

    thread.join(timeout=1.0)
    elapsed = time.time() - start_time

    if "error" in result_holder:
        err = result_holder["error"]
        err_msg = str(err)
        if "ZeroGPU quota" in err_msg or "exceeded your free ZeroGPU quota" in err_msg:
            accumulated_logs.append(
                "\n" + "=" * 60 + "\n"
                "⚠️ ZeroGPU Daily Quota Limit Reached\n"
                f"{err_msg}\n" + "=" * 60
            )
        else:
            accumulated_logs.append(
                f"\n❌ Pipeline failed: {err_msg}"
            )
        yield "\n".join(accumulated_logs), None, None
        return

    res = result_holder.get("result", {})
    manifest = res.get("manifest")
    manifest_path = None

    if manifest:
        try:
            manifest_path = "/tmp/clips_manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
        except Exception as e:
            accumulated_logs.append(f"⚠️ Could not write local manifest file: {e}")
            manifest_path = None

    summary = (
        "\n" + "=" * 60 + "\n"
        f"🎉 PIPELINE FINISHED IN {elapsed:.1f}s\n"
        f"• Clips Selected  : {res.get('clips_selected', 0)}\n"
        f"• Clips Processed : {res.get('clips_processed', 0)}\n"
        f"• Clips Failed    : {res.get('clips_failed', 0)}\n"
        + "=" * 60
    )
    accumulated_logs.append(summary)
    if manifest_path:
        accumulated_logs.append(f"\n✅ clips_manifest.json created successfully! Download it below or view the preview.")

    yield "\n".join(accumulated_logs), manifest_path, manifest

with gr.Blocks(title="Dark Phoenix Backend", theme=gr.themes.Soft(primary_hue="purple")) as demo:
    gr.Markdown("# 🔥 Dark Phoenix AI Video Clipper (ZeroGPU)")
    gr.Markdown(
        "Backend worker powered by **WhisperX**, **Gemini 3.1 Flash Lite**, **TalkNet ASD**, "
        "and **ffmpeg** with burned-in **`LunarTech logo`** watermark."
    )
    
    with gr.Row():
        with gr.Column(scale=1):
            input_youtube_url = gr.Textbox(
                label="YouTube Video URL (Server-Side Ingestion)",
                placeholder="https://www.youtube.com/watch?v=YRvf00NooN8",
                info="Optional: Paste a YouTube link to download and ingest directly server-side in the cloud."
            )
            input_s3_key = gr.Textbox(
                label="Source S3 Key",
                placeholder="uploads/.../original.mp4",
                info="Or provide an existing S3 Key in your Supabase S3 bucket."
            )
            input_max_clips = gr.Slider(
                minimum=1, maximum=5, value=3, step=1,
                label="Maximum Clips to Generate"
            )
            process_btn = gr.Button("🚀 Process Video", variant="primary")
            
        with gr.Column(scale=1):
            output_status = gr.Textbox(
                label="Execution Status & Live Logs",
                lines=12,
                interactive=False
            )
            output_manifest_file = gr.File(
                label="📥 Download clips_manifest.json"
            )
            output_manifest_json = gr.JSON(
                label="📋 clips_manifest.json Preview"
            )

    process_btn.click(
        fn=gradio_process_action,
        inputs=[input_s3_key, input_max_clips, input_youtube_url],
        outputs=[output_status, output_manifest_file, output_manifest_json]
    )

    gr.Markdown("---")
    gr.Markdown(
        "**API Endpoint**: `POST /process_video` is active and ready to receive requests "
        "from Inngest Cloud and your deployed Next.js frontend."
    )

demo.queue()

# ---------------------------------------------------------------------------
# Attach FastAPI Endpoints to Gradio Server (Consumed by Inngest Cloud & Vercel)
# ---------------------------------------------------------------------------
import gradio.routes
_orig_create_app = gradio.routes.App.create_app

def _custom_create_app(*args, **kwargs):
    app = _orig_create_app(*args, **kwargs)

    @app.get("/health")
    def health_check():
        return {
            "status": "online",
            "service": "Dark Phoenix Backend",
            "zerogpu_available": has_spaces,
            "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
        }

    @app.post("/process_video")
    def process_video_endpoint(
        request: ProcessVideoRequest,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(auth_scheme)
    ):
        clipper = get_clipper()
        return clipper.process_video(request, token=credentials)

    return app

gradio.routes.App.create_app = _custom_create_app

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860, ssr_mode=False)
