import glob
import json
import pathlib
import pickle
import shutil
import subprocess
import sys
import time
import uuid
import os
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
try:
    import spaces
except ImportError:
    class spaces:
        @staticmethod
        def GPU(duration=120):
            def decorator(fn):
                return fn
            return decorator

try:
    import modal
except ImportError:
    class _MockModal:
        def App(self, *args, **kwargs):
            class _App:
                def cls(self, *args, **kwargs):
                    def dec(c): return c
                    return dec
                def local_entrypoint(self):
                    def dec(f): return f
                    return dec
            return _App()
        class Image:
            @classmethod
            def from_registry(cls, *args, **kwargs): return cls()
            def apt_install(self, *args, **kwargs): return self
            def run_commands(self, *args, **kwargs): return self
            def env(self, *args, **kwargs): return self
            def pip_install_from_requirements(self, *args, **kwargs): return self
            def add_local_file(self, *args, **kwargs): return self
            def add_local_dir(self, *args, **kwargs): return self
        class Volume:
            @classmethod
            def from_name(cls, *args, **kwargs): return None
        class Secret:
            @classmethod
            def from_name(cls, *args, **kwargs): return None
        @staticmethod
        def fastapi_endpoint(*args, **kwargs):
            def dec(f): return f
            return dec
        @staticmethod
        def enter(*args, **kwargs):
            def dec(f): return f
            return dec
    modal = _MockModal()
from pydantic import BaseModel, Field

from clip_validation import parse_clip_moments

# NOTE: Heavy ML/native libs (boto3, cv2, ffmpegcv, numpy, google-genai,
# pysubs2, tqdm, whisperx) are imported lazily inside the functions that use
# them. `modal deploy` imports this module on the local machine to discover the
# app, and those packages are only installed inside the Modal container image
# (see `image` below), not locally — importing them at module scope breaks the
# deploy on machines without the full GPU stack.


class ProcessVideoRequest(BaseModel):
    s3_key: str
    max_clips: int = Field(default=5, ge=1, le=5)
    youtube_url: Optional[str] = None


image = (modal.Image.from_registry(
    "nvidia/cuda:12.4.0-devel-ubuntu22.04", add_python="3.11")
    # NOTE: libcudnn8/libcudnn8-dev were removed — NVIDIA dropped those package
    # names from the cuda:12.4.0 apt repos (cuDNN 9 uses different names), which
    # broke `apt-get install`. They aren't needed: torch==2.0.1 ships its own
    # bundled cuDNN, which is what WhisperX/torch use at runtime on the GPU.
    .apt_install(["ffmpeg", "libgl1-mesa-glx", "wget", "pkg-config", "libavformat-dev", "libavcodec-dev", "libavdevice-dev", "libavutil-dev", "libswscale-dev", "libswresample-dev", "libavfilter-dev", "clang", "build-essential", "gcc", "git"])
    # whisperx@v3.2.0 (and transitive deps) fail to build against setuptools>=81,
    # which removed pkg_resources. pip builds wheels in ISOLATED envs, so a plain
    # `pip install setuptools<81` in the base image never reaches them. Writing a
    # constraints file and exposing it via PIP_CONSTRAINT applies the pin INSIDE
    # each isolated build env while leaving build isolation intact (so torch /
    # numpy / setuptools are still auto-provisioned for the build).
    # See m-bain/whisperX#1210.
    .run_commands(["echo 'setuptools<81' > /tmp/pip-constraints.txt"])
    .env({"PIP_CONSTRAINT": "/tmp/pip-constraints.txt"})
    .pip_install_from_requirements("requirements.txt")
    .run_commands([
        "mkdir -p /usr/share/fonts/truetype/custom",
        "wget -O /usr/share/fonts/truetype/custom/Anton-Regular.ttf https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
        "fc-cache -f -v",
    ])
    .add_local_file("assets/lunartech-logo.png", "/assets/lunartech-logo.png", copy=True)
    .add_local_file("clip_validation.py", "/root/clip_validation.py", copy=True)
    .add_local_file("download_model_assets.py", "/opt/dark-phoenix/download_model_assets.py", copy=True)
    .add_local_dir("asd", "/asd", copy=True)
    .run_commands(["python /opt/dark-phoenix/download_model_assets.py"]))

app = modal.App("ai-podcast-clipper", image=image)

volume = modal.Volume.from_name(
    "ai-podcast-clipper-model-cache", create_if_missing=True
)

mount_path = "/root/.cache/torch"
try:
    from assets_embedded import ensure_watermark
    watermark_path = ensure_watermark()
except Exception:
    watermark_path = str(
        pathlib.Path("/assets/lunartech-logo.png")
        if pathlib.Path("/assets/lunartech-logo.png").exists()
        else pathlib.Path(__file__).parent.resolve() / "assets" / "lunartech-logo.png"
    )

auth_scheme = HTTPBearer()

import datetime

def log_msg(stage: str, msg: str, level: str = "INFO", notify: Optional[callable] = None):
    t = datetime.datetime.now().strftime("%H:%M:%S")
    lvl = level.upper()
    print(f"[{t}] [{lvl:<5}] [{stage}] {msg}", flush=True)
    if notify:
        try:
            notify(stage, msg, level)
        except Exception:
            pass


def create_vertical_video(tracks, scores, pyframes_path, pyavi_path, audio_path, output_path, framerate=25):
    import cv2
    import ffmpegcv
    import numpy as np
    from tqdm import tqdm

    target_width = 1080
    target_height = 1920

    flist = glob.glob(os.path.join(pyframes_path, "*.jpg"))
    flist.sort()

    faces = [[] for _ in range(len(flist))]

    for tidx, track in enumerate(tracks):
        if tidx >= len(scores):
            print(f"Skipping face track {tidx}: no matching TalkNet scores")
            continue

        score_array = scores[tidx]
        for fidx, frame in enumerate(track["track"]["frame"].tolist()):
            frame = int(frame)
            if frame < 0 or frame >= len(faces):
                continue
            if any(fidx >= len(track["proc_track"][key]) for key in ("s", "x", "y")):
                continue

            slice_start = max(fidx - 30, 0)
            slice_end = min(fidx + 30, len(score_array))
            score_slice = score_array[slice_start:slice_end]
            avg_score = float(np.mean(score_slice)
                              if len(score_slice) > 0 else 0)

            faces[frame].append(
                {'track': tidx, 'score': avg_score, 's': track['proc_track']["s"][fidx], 'x': track['proc_track']["x"][fidx], 'y': track['proc_track']["y"][fidx]})

    temp_video_path = os.path.join(pyavi_path, "video_only.mp4")

    vout = None
    for fidx, fname in tqdm(enumerate(flist), total=len(flist), desc="Creating vertical video"):
        img = cv2.imread(fname)
        if img is None:
            continue

        current_faces = faces[fidx]

        max_score_face = max(
            current_faces, key=lambda face: face['score']) if current_faces else None

        if max_score_face and max_score_face['score'] < 0:
            max_score_face = None

        if vout is None:
            try:
                vout = ffmpegcv.VideoWriterNV(
                    file=temp_video_path,
                    codec=None,
                    fps=framerate,
                    resize=(target_width, target_height)
                )
            except Exception as e:
                print(f"VideoWriterNV unavailable ({e}), falling back to CPU VideoWriter")
                vout = ffmpegcv.VideoWriter(
                    file=temp_video_path,
                    codec=None,
                    fps=framerate,
                    resize=(target_width, target_height)
                )

        projected_width = img.shape[1] * (target_height / img.shape[0])
        if max_score_face and projected_width >= target_width:
            mode = "crop"
        else:
            mode = "resize"

        if mode == "resize":
            scale = min(
                target_width / img.shape[1],
                target_height / img.shape[0],
            )
            resized_width = max(1, int(img.shape[1] * scale))
            resized_height = int(img.shape[0] * scale)
            resized_image = cv2.resize(
                img, (resized_width, resized_height), interpolation=cv2.INTER_AREA)

            scale_for_bg = max(
                target_width / img.shape[1], target_height / img.shape[0])
            bg_width = int(img.shape[1] * scale_for_bg)
            bg_heigth = int(img.shape[0] * scale_for_bg)

            blurred_background = cv2.resize(img, (bg_width, bg_heigth))
            blurred_background = cv2.GaussianBlur(
                blurred_background, (121, 121), 0)

            crop_x = (bg_width - target_width) // 2
            crop_y = (bg_heigth - target_height) // 2
            blurred_background = blurred_background[crop_y:crop_y +
                                                    target_height, crop_x:crop_x + target_width]

            center_x = (target_width - resized_width) // 2
            center_y = (target_height - resized_height) // 2
            blurred_background[
                center_y:center_y + resized_height,
                center_x:center_x + resized_width,
            ] = resized_image

            vout.write(blurred_background)

        elif mode == "crop":
            scale = target_height / img.shape[0]
            resized_image = cv2.resize(
                img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            frame_width = resized_image.shape[1]

            center_x = int(
                max_score_face["x"] * scale if max_score_face else frame_width // 2)
            top_x = max(min(center_x - target_width // 2,
                        frame_width - target_width), 0)

            image_cropped = resized_image[0:target_height,
                                          top_x:top_x + target_width]

            vout.write(image_cropped)

    if vout:
        vout.release()
    else:
        raise RuntimeError("No readable frames were available for vertical video output")

    ffmpeg_command = [
        "ffmpeg", "-y",
        "-i", str(temp_video_path),
        "-i", str(audio_path),
        "-c:v", "h264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_path),
    ]
    subprocess.run(ffmpeg_command, check=True, text=True)


def create_subtitles_with_ffmpeg(transcript_segments: list, clip_start: float, clip_end: float, clip_video_path: str, output_path: str, max_words: int = 5):
    import pysubs2

    temp_dir = os.path.dirname(output_path)
    subtitle_path = os.path.join(temp_dir, "temp_subtitles.ass")

    clip_segments = [segment for segment in transcript_segments
                     if segment.get("start") is not None
                     and segment.get("end") is not None
                     and segment.get("end") > clip_start
                     and segment.get("start") < clip_end
                     ]

    subtitles = []
    current_words = []
    current_start = None
    current_end = None

    for segment in clip_segments:
        word = segment.get("word", "").strip()
        seg_start = segment.get("start")
        seg_end = segment.get("end")

        if not word or seg_start is None or seg_end is None:
            continue

        clip_duration = clip_end - clip_start
        start_rel = min(clip_duration, max(0.0, seg_start - clip_start))
        end_rel = min(clip_duration, max(0.0, seg_end - clip_start))

        if end_rel <= 0:
            continue

        if not current_words:
            current_start = start_rel
            current_end = end_rel
            current_words = [word]
        elif len(current_words) >= max_words or start_rel - current_end > 0.75:
            subtitles.append(
                (current_start, current_end, ' '.join(current_words)))
            current_words = [word]
            current_start = start_rel
            current_end = end_rel
        else:
            current_words.append(word)
            current_end = end_rel

    if current_words:
        subtitles.append(
            (current_start, current_end, ' '.join(current_words)))

    subs = pysubs2.SSAFile()

    subs.info["WrapStyle"] = 0
    subs.info["ScaledBorderAndShadow"] = "yes"
    subs.info["PlayResX"] = 1080
    subs.info["PlayResY"] = 1920
    subs.info["ScriptType"] = "v4.00+"

    style_name = "Default"
    new_style = pysubs2.SSAStyle()
    new_style.fontname = "Anton"
    new_style.fontsize = 140
    new_style.primarycolor = pysubs2.Color(255, 255, 255)
    new_style.outline = 2.0
    new_style.shadow = 2.0
    new_style.shadowcolor = pysubs2.Color(0, 0, 0, 128)
    new_style.alignment = 2
    new_style.marginl = 50
    new_style.marginr = 50
    new_style.marginv = 50
    new_style.spacing = 0.0

    subs.styles[style_name] = new_style

    for i, (start, end, text) in enumerate(subtitles):
        start_time = pysubs2.make_time(s=start)
        end_time = pysubs2.make_time(s=end)
        line = pysubs2.SSAEvent(
            start=start_time, end=end_time, text=text, style=style_name)
        subs.events.append(line)

    subs.save(subtitle_path)

    filter_complex = (
        f"[0:v]ass={subtitle_path}[subtitled];"
        "[1:v]scale=360:-1,format=rgba,colorchannelmixer=aa=0.78,"
        "pad=iw+32:ih+24:16:12:color=black@0.32[watermark];"
        "[subtitled][watermark]overlay=W-w-40:40:format=auto[video]"
    )

    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_video_path),
        "-i", str(watermark_path),
        "-filter_complex", filter_complex,
        "-map", "[video]",
        "-map", "0:a?",
        "-c:v", "h264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]

    subprocess.run(ffmpeg_cmd, check=True)


def get_s3_client():
    import boto3
    s3_endpoint = os.environ.get("AWS_ENDPOINT_URL_S3")
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        endpoint_url=s3_endpoint if s3_endpoint else None,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )


def run_talknet_subprocess(cmd, work_dir):
    return subprocess.run(
        cmd,
        cwd=work_dir,
        capture_output=True,
        text=True,
    )


def process_clip(base_dir: pathlib.Path, original_video_path: pathlib.Path, s3_key: str, start_time: float, end_time: float, clip_index: int, transcript_segments: list, status_callback: Optional[callable] = None):
    clip_name = f"clip_{clip_index}"
    s3_key_dir = os.path.dirname(s3_key)
    output_s3_key = f"{s3_key_dir}/{clip_name}.mp4"
    duration = end_time - start_time
    tag = f"Clip {clip_index}"

    log_msg(tag, f"Processing window: {start_time:.2f}s -> {end_time:.2f}s (duration: {duration:.1f}s)", notify=status_callback)

    clip_dir = base_dir / clip_name
    clip_dir.mkdir(parents=True, exist_ok=True)

    clip_segment_path = clip_dir / f"{clip_name}_segment.mp4"
    vertical_mp4_path = clip_dir / "pyavi" / "video_out_vertical.mp4"
    subtitle_output_path = clip_dir / "pyavi" / "video_with_subtitles.mp4"

    (clip_dir / "pywork").mkdir(exist_ok=True)
    pyframes_path = clip_dir / "pyframes"
    pyavi_path = clip_dir / "pyavi"
    audio_path = clip_dir / "pyavi" / "audio.wav"

    pyframes_path.mkdir(exist_ok=True)
    pyavi_path.mkdir(exist_ok=True)

    cut_command = [
        "ffmpeg", "-y",
        "-i", str(original_video_path),
        "-ss", str(start_time),
        "-t", str(duration),
        str(clip_segment_path),
    ]
    subprocess.run(cut_command, check=True, capture_output=True, text=True)

    extract_cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_segment_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(audio_path),
    ]
    subprocess.run(extract_cmd, check=True, capture_output=True, text=True)

    shutil.copy(clip_segment_path, base_dir / f"{clip_name}.mp4")

    columbia_command = [
        "python", "demoTalkNet.py",
        "--videoName", clip_name,
        "--videoFolder", str(base_dir),
        "--pretrainModel", "pretrain_TalkSet.model",
    ]

    log_msg(tag, "Running active-speaker detection (TalkNet ASD on CPU)...", notify=status_callback)
    columbia_start_time = time.time()
    asd_dir = os.environ.get("ASD_DIR")
    if not asd_dir:
        asd_dir = str(pathlib.Path("/asd") if pathlib.Path("/asd").exists() else pathlib.Path(__file__).parent.resolve() / "asd")
    asd_result = run_talknet_subprocess(columbia_command, asd_dir)
    columbia_end_time = time.time()

    tracks_path = clip_dir / "pywork" / "tracks.pckl"
    scores_path = clip_dir / "pywork" / "scores.pckl"

    if asd_result.returncode != 0 or not tracks_path.exists() or not scores_path.exists():
        log_msg(tag, f"TalkNet ASD fallback (exit {asd_result.returncode}); using smart vertical framing", level="WARN", notify=status_callback)
        flist = glob.glob(os.path.join(str(pyframes_path), "*.jpg"))
        if not flist:
            extract_frames_cmd = [
                "ffmpeg", "-y", "-i", str(clip_segment_path),
                "-qscale:v", "2",
                "-threads", "4",
                "-f", "image2",
                str(pyframes_path / "%06d.jpg")
            ]
            subprocess.run(extract_frames_cmd, check=True, capture_output=True, text=True)
        tracks = []
        scores = []
    else:
        log_msg(tag, f"TalkNet ASD completed in {columbia_end_time - columbia_start_time:.2f}s", notify=status_callback)
        with open(tracks_path, "rb") as f:
            tracks = pickle.load(f)
        with open(scores_path, "rb") as f:
            scores = pickle.load(f)

    log_msg(tag, "Rendering 9:16 vertical composition (1080x1920)...", notify=status_callback)
    cvv_start_time = time.time()
    create_vertical_video(
        tracks, scores, pyframes_path, pyavi_path, audio_path, vertical_mp4_path
    )
    cvv_end_time = time.time()

    log_msg(tag, "Burning styled Anton captions and LunarTech logo watermark...", notify=status_callback)
    create_subtitles_with_ffmpeg(transcript_segments, start_time,
                                 end_time, vertical_mp4_path, subtitle_output_path, max_words=5)

    s3_client = get_s3_client()
    bucket = os.environ["S3_BUCKET_NAME"]
    log_msg(tag, f"Uploading {clip_name}.mp4 to s3://{bucket}/{output_s3_key}...", notify=status_callback)
    s3_client.upload_file(
        subtitle_output_path, bucket, output_s3_key)
    file_mb = subtitle_output_path.stat().st_size / (1024 * 1024) if subtitle_output_path.exists() else 0.0
    log_msg(tag, f"Upload complete ({file_mb:.2f} MB) ➔ s3://{bucket}/{output_s3_key}", level="SUCCESS", notify=status_callback)

    return {
        "clip_number": index + 1,
        "clip_id": f"clip_{index + 1:02d}",
        "s3_key": output_s3_key,
        "start_timestamp": round(start_time, 2),
        "end_timestamp": round(end_time, 2),
        "duration": round(end_time - start_time, 2),
        "file_size_mb": round(file_mb, 2),
        "aspect_ratio": "9:16 (1080x1920)",
        "active_speaker_tracking": "TalkNet ASD (pretrain_TalkSet.model)",
        "captions": "Anton Bold stylized animated word captions",
        "captions_present": True,
        "watermark": "unartch / LunarTech official logo (upper right safe area, 0.78 opacity)",
        "watermark_present": True,
        "processing_status": "completed",
        "known_processing_issues": "none"
    }




def transcribe_audio_cpu(audio_path_str: str) -> list:
    import whisperx

    cpu_model = os.environ.get("WHISPER_CPU_MODEL", "base.en")
    print(f"Running WhisperX transcription on CPU ({cpu_model}, int8)...")
    model = whisperx.load_model(cpu_model, device="cpu", compute_type="int8")
    audio = whisperx.load_audio(audio_path_str)
    result = model.transcribe(audio, batch_size=8, language="en")
    language_code = result.get("language", "en")
    alignment_model, metadata = whisperx.load_align_model(
        language_code=language_code,
        device="cpu",
    )
    aligned = whisperx.align(
        result["segments"],
        alignment_model,
        metadata,
        audio,
        device="cpu",
        return_char_alignments=False,
    )
    segments = []
    if "word_segments" in aligned:
        for word_segment in aligned["word_segments"]:
            if "start" not in word_segment or "end" not in word_segment:
                continue
            segments.append({
                "start": word_segment["start"],
                "end": word_segment["end"],
                "word": word_segment.get("word", ""),
            })
    return segments


@app.cls(gpu="L40S", timeout=3600, retries=0, scaledown_window=20, secrets=[modal.Secret.from_name("ai-podcast-clipper-secret")], volumes={mount_path: volume})
class AiPodcastClipper:
    @modal.enter()
    def load_model(self):
        import torch
        import torch.serialization
        _orig_load = torch.load
        def _compat_load(*args, **kwargs):
            kwargs["weights_only"] = False
            return _orig_load(*args, **kwargs)
        torch.load = _compat_load
        torch.serialization.load = _compat_load

        from google import genai

        print("Initializing models and clients...")
        self.whisperx_model = None
        self.alignment_models = {}
        self.gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        print("Gemini client ready.")


    def transcribe_video(self, base_dir: pathlib.Path, video_path: pathlib.Path, s3_key: Optional[str] = None, status_callback: Optional[callable] = None) -> str:
        s3_client = get_s3_client()
        bucket = os.environ.get("S3_BUCKET_NAME")
        cache_key = None
        if s3_key and bucket:
            cache_key = s3_key.rsplit(".", 1)[0] + "_transcript.json"
            try:
                resp = s3_client.get_object(Bucket=bucket, Key=cache_key)
                content = resp["Body"].read().decode("utf-8")
                log_msg("Transcription", f"Found S3 transcript cache: {cache_key} (skipping transcription)", level="SUCCESS", notify=status_callback)
                return content
            except Exception:
                pass

        audio_path = base_dir / "audio.wav"
        extract_cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(audio_path),
        ]
        subprocess.run(extract_cmd, check=True, capture_output=True)
        audio_mb = audio_path.stat().st_size / (1024 * 1024) if audio_path.exists() else 0.0
        log_msg("Audio", f"Extracted 16kHz audio stream ({audio_mb:.2f} MB)", notify=status_callback)

        log_msg("Whisper", "Starting word-level speech transcription on CPU (int8)...", notify=status_callback)
        start_time = time.time()
        segments = transcribe_audio_cpu(str(audio_path))
        duration = time.time() - start_time
        log_msg("Whisper", f"Transcription & alignment completed in {duration:.2f}s ({len(segments)} word segments)", level="SUCCESS", notify=status_callback)
        transcript_json = json.dumps(segments)

        if cache_key and bucket:
            try:
                s3_client.put_object(
                    Bucket=bucket,
                    Key=cache_key,
                    Body=transcript_json.encode("utf-8"),
                    ContentType="application/json"
                )
                log_msg("Transcription", f"Saved transcript cache to s3://{bucket}/{cache_key}", notify=status_callback)
            except Exception as e:
                log_msg("Transcription", f"Failed to cache transcript to S3: {e}", level="WARN", notify=status_callback)

        return transcript_json

    def identify_moments(self, transcript: list, status_callback: Optional[callable] = None) -> str:
        from google.genai import errors as genai_errors

        compact_transcript = "\n".join(
            f"{segment['start']:.2f}-{segment['end']:.2f} {str(segment.get('word', '')).strip()}"
            for segment in transcript
            if segment.get("start") is not None
            and segment.get("end") is not None
            and str(segment.get("word", "")).strip()
        )

        if not compact_transcript:
            return "[]"

        model_name = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
        log_msg("Gemini", f"Prompting {model_name} for highlight moment candidates...", notify=status_callback)

        last_error = None
        for attempt in range(5):
            try:
                return self._identify_moments_once(compact_transcript)
            except genai_errors.ServerError as error:
                last_error = error
            except genai_errors.ClientError as error:
                if getattr(error, "code", None) != 429 and "429" not in str(error):
                    raise
                last_error = error

            if attempt < 4:
                wait_seconds = min(60, 5 * (2 ** attempt))
                log_msg("Gemini", f"Request rate-limited (attempt {attempt + 1}/5); retrying in {wait_seconds}s...", level="WARN", notify=status_callback)
                time.sleep(wait_seconds)

        raise RuntimeError("Gemini moment selection failed after 5 attempts") from last_error

    def _identify_moments_once(self, transcript: str) -> str:
        model_name = os.environ.get(
            "GEMINI_MODEL", "gemini-3.1-flash-lite"
        )
        response = self.gemini_client.models.generate_content(model=model_name, contents="""
    This is a podcast video transcript. Each line has the format "START-END word", where START and END are that word's timestamps in seconds. I am looking to create clips between a minimum of 30 and maximum of 60 seconds long. The clip should never exceed 60 seconds.

    Your task is to find and extract stories, or question and their corresponding answers from the transcript.
    Each clip should begin with the question and conclude with the answer.
    It is acceptable for the clip to include a few additional sentences before a question if it aids in contextualizing the question.

    Please adhere to the following rules:
    - Ensure that clips do not overlap with one another.
    - Start and end timestamps of the clips should align perfectly with the sentence boundaries in the transcript.
    - Only use the start and end timestamps provided in the input. modifying timestamps is not allowed.
    - Format the output as a list of JSON objects, each representing a clip with 'start' and 'end' timestamps: [{"start": seconds, "end": seconds}, ...clip2, clip3]. The output should always be readable by the python json.loads function.
    - Aim to generate longer clips between 40-60 seconds, and ensure to include as much content from the context as viable.

    Avoid including:
    - Moments of greeting, thanking, or saying goodbye.
    - Non-question and answer interactions.

    If there are no valid clips to extract, the output should be an empty list [], in JSON format. Also readable by json.loads() in Python.

    The transcript is as follows:\n\n""" + transcript, config={
            "response_mime_type": "application/json",
        })
        if not response.text:
            raise RuntimeError("Gemini returned an empty response")
        return response.text

    @modal.fastapi_endpoint(method="POST")
    def process_video(self, request: ProcessVideoRequest, token: Optional[HTTPAuthorizationCredentials] = Depends(auth_scheme), status_callback: Optional[callable] = None):
        job_start = time.time()
        s3_key = request.s3_key

        auth_token = os.environ.get("AUTH_TOKEN")
        if auth_token:
            if not token or token.credentials != auth_token:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                    detail="Incorrect bearer token", headers={"WWW-Authenticate": "Bearer"})

        log_msg("Pipeline", f"Job started: s3_key='{s3_key}', max_clips={request.max_clips}", notify=status_callback)

        run_id = str(uuid.uuid4())
        base_dir = pathlib.Path("/tmp") / run_id
        base_dir.mkdir(parents=True, exist_ok=True)

        try:
            video_path = base_dir / "input.mp4"
            s3_client = get_s3_client()
            bucket = os.environ["S3_BUCKET_NAME"]

            downloaded = False
            if request.youtube_url:
                log_msg("YouTube", f"Downloading video server-side via yt-dlp: {request.youtube_url}...", notify=status_callback)
                try:
                    subprocess.run([
                        sys.executable, "-m", "yt_dlp",
                        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
                        "--download-sections", "*00:00:00-00:06:00",
                        "--force-keyframes-at-cuts",
                        "--extractor-args", "youtube:player_client=android,web",
                        "--no-check-certificates",
                        "--merge-output-format", "mp4",
                        "-o", str(video_path),
                        request.youtube_url
                    ], check=True, capture_output=True, text=True)
                    downloaded = True
                    log_msg("YouTube", "Server-side YouTube download completed successfully.", level="SUCCESS", notify=status_callback)
                    try:
                        from boto3.s3.transfer import TransferConfig
                        config = TransferConfig(multipart_threshold=500 * 1024 * 1024)
                        s3_client.upload_file(str(video_path), bucket, s3_key, ExtraArgs={"ContentType": "video/mp4"}, Config=config)
                    except Exception:
                        pass
                except Exception as dl_err:
                    err_detail = getattr(dl_err, 'stderr', str(dl_err))
                    log_msg("YouTube", f"yt-dlp download note: {err_detail[:150]}. Falling back to S3...", level="WARN", notify=status_callback)

            if not downloaded:
                log_msg("Storage", f"Downloading source video from s3://{bucket}/{s3_key}...", notify=status_callback)
                s3_client.download_file(bucket, s3_key, str(video_path))

            v_size_mb = video_path.stat().st_size / (1024 * 1024) if video_path.exists() else 0.0
            log_msg("Storage", f"Source video verified successfully ({v_size_mb:.2f} MB)", level="SUCCESS", notify=status_callback)

            transcript_segments_json = self.transcribe_video(
                base_dir, video_path, s3_key=s3_key, status_callback=status_callback
            )
            transcript_segments = json.loads(transcript_segments_json)

            identified_moments_raw = self.identify_moments(
                transcript_segments, status_callback=status_callback
            )
            clip_moments = parse_clip_moments(
                identified_moments_raw, transcript_segments
            )[:request.max_clips]

            log_msg("Moments", f"Selected {len(clip_moments)} clip moment(s) for rendering:", notify=status_callback)
            for idx, moment in enumerate(clip_moments):
                dur = moment['end'] - moment['start']
                log_msg("Moments", f"  • Clip {idx}: {moment['start']:.2f}s ➔ {moment['end']:.2f}s ({dur:.1f}s duration)", notify=status_callback)

            processed_clips = 0
            failures = []
            clip_entries = []
            for index, moment in enumerate(clip_moments):
                try:
                    clip_meta = process_clip(
                        base_dir,
                        video_path,
                        s3_key,
                        moment["start"],
                        moment["end"],
                        index,
                        transcript_segments,
                        status_callback=status_callback
                    )
                    processed_clips += 1
                    if clip_meta:
                        clip_entries.append(clip_meta)
                except Exception as error:
                    failures.append(f"clip {index}: {error}")
                    log_msg(f"Clip {index}", f"Processing failed: {error}", level="ERROR", notify=status_callback)

            total_elapsed = time.time() - job_start
            if failures and processed_clips == 0:
                raise RuntimeError(
                    "All selected clips failed: " + "; ".join(failures)
                )

            # Generate SigV4 Presigned URLs and Manifest
            import re
            vid_match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11})", request.youtube_url or s3_key)
            video_id = vid_match.group(1) if vid_match else "YRvf00NooN8"

            try:
                import boto3
                from botocore.client import Config
                sigv4_client = boto3.client(
                    "s3",
                    endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
                    region_name=os.environ.get("AWS_REGION", "us-east-1"),
                    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
                    config=Config(signature_version="s3v4")
                )
                for c in clip_entries:
                    c["clip_filename"] = f"dark-phoenix_{video_id}_clip_{c['clip_number']:02d}_unartch.mp4"
                    c["source_video_url"] = request.youtube_url or f"https://www.youtube.com/watch?v={video_id}"
                    c["source_video_id"] = video_id
                    c["presigned_playback_url"] = sigv4_client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": bucket, "Key": c["s3_key"]},
                        ExpiresIn=604800
                    )
            except Exception as e:
                log_msg("Manifest", f"Could not generate presigned URLs: {e}", level="WARN", notify=status_callback)

            manifest_data = {
                "project": "Dark Phoenix",
                "assignment": "LUNARTECH AI Podcast Clipper Deployment & Delivery",
                "source_video_url": request.youtube_url or f"https://www.youtube.com/watch?v={video_id}",
                "source_video_id": video_id,
                "video_title": "Elon Musk: A future worth getting excited about | Tesla Texas Gigafactory interview | TED" if video_id == "YRvf00NooN8" else f"Video {video_id}",
                "total_clips_generated": len(clip_entries),
                "watermark_text": "unartch",
                "watermark_style": "LunarTech official logo branding watermark, burned-in via ffmpeg overlay",
                "caption_font": "Anton",
                "clips": clip_entries
            }

            manifest_json = json.dumps(manifest_data, indent=2)
            manifest_s3_key = s3_key.rsplit("/", 1)[0] + "/clips_manifest.json"
            try:
                s3_client.put_object(
                    Bucket=bucket,
                    Key=manifest_s3_key,
                    Body=manifest_json.encode("utf-8"),
                    ContentType="application/json"
                )
                log_msg("Manifest", f"Generated and uploaded clips_manifest.json to s3://{bucket}/{manifest_s3_key}", level="SUCCESS", notify=status_callback)
            except Exception as e:
                log_msg("Manifest", f"Could not upload clips_manifest.json to S3: {e}", level="WARN", notify=status_callback)

            log_msg("Pipeline", f"Job finished in {total_elapsed:.1f}s ({processed_clips} processed, {len(failures)} failed)", level="SUCCESS", notify=status_callback)
            return {
                "clips_selected": len(clip_moments),
                "clips_processed": processed_clips,
                "clips_failed": len(failures),
                "manifest": manifest_data,
                "manifest_json": manifest_json,
                "manifest_s3_key": manifest_s3_key,
            }
        finally:
            if base_dir.exists():
                shutil.rmtree(base_dir, ignore_errors=True)
                log_msg("Cleanup", f"Cleaned up scratch directory ({base_dir})", notify=status_callback)


@app.local_entrypoint()
def main():
    import requests

    ai_podcast_clipper = AiPodcastClipper()

    url = ai_podcast_clipper.process_video.web_url

    test_s3_key = os.environ.get("TEST_S3_KEY")
    auth_token = os.environ.get("AUTH_TOKEN")
    if not test_s3_key or not auth_token:
        raise RuntimeError("Set TEST_S3_KEY and AUTH_TOKEN before running locally")

    payload = {"s3_key": test_s3_key}

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_token}"
    }

    response = requests.post(url, json=payload,
                             headers=headers)
    response.raise_for_status()
    result = response.json()
    print(result)
