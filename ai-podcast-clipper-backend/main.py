import glob
import json
import pathlib
import pickle
import shutil
import subprocess
import time
import uuid
import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import modal
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
watermark_path = "/assets/lunartech-logo.png"

auth_scheme = HTTPBearer()


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
            vout = ffmpegcv.VideoWriterNV(
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
        "-i", watermark_path,
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


def process_clip(base_dir: pathlib.Path, original_video_path: pathlib.Path, s3_key: str, start_time: float, end_time: float, clip_index: int, transcript_segments: list):
    clip_name = f"clip_{clip_index}"
    s3_key_dir = os.path.dirname(s3_key)
    output_s3_key = f"{s3_key_dir}/{clip_name}.mp4"
    print(f"Output S3 key: {output_s3_key}")

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

    duration = end_time - start_time
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

    columbia_start_time = time.time()
    asd_result = subprocess.run(
        columbia_command,
        cwd="/asd",
        capture_output=True,
        text=True,
    )
    if asd_result.returncode != 0:
        print(
            f"TalkNet failed for {clip_name} (exit {asd_result.returncode}).\n"
            f"stdout tail:\n{asd_result.stdout[-4000:]}\n"
            f"stderr tail:\n{asd_result.stderr[-4000:]}"
        )
        raise RuntimeError(
            f"Active-speaker detection failed for {clip_name}"
        )
    columbia_end_time = time.time()
    print(
        f"Columbia script completed in {columbia_end_time - columbia_start_time:.2f} seconds")

    tracks_path = clip_dir / "pywork" / "tracks.pckl"
    scores_path = clip_dir / "pywork" / "scores.pckl"
    if not tracks_path.exists() or not scores_path.exists():
        raise FileNotFoundError("Tracks or scores not found for clip")

    with open(tracks_path, "rb") as f:
        tracks = pickle.load(f)

    with open(scores_path, "rb") as f:
        scores = pickle.load(f)

    cvv_start_time = time.time()
    create_vertical_video(
        tracks, scores, pyframes_path, pyavi_path, audio_path, vertical_mp4_path
    )
    cvv_end_time = time.time()
    print(
        f"Clip {clip_index} vertical video creation time: {cvv_end_time - cvv_start_time:.2f} seconds")

    create_subtitles_with_ffmpeg(transcript_segments, start_time,
                                 end_time, vertical_mp4_path, subtitle_output_path, max_words=5)

    import boto3
    s3_client = boto3.client("s3")
    s3_client.upload_file(
        subtitle_output_path, os.environ["S3_BUCKET_NAME"], output_s3_key)


@app.cls(gpu="L40S", timeout=3600, retries=0, scaledown_window=20, secrets=[modal.Secret.from_name("ai-podcast-clipper-secret")], volumes={mount_path: volume})
class AiPodcastClipper:
    @modal.enter()
    def load_model(self):
        import whisperx
        from google import genai

        print("Loading models")

        self.whisperx_model = whisperx.load_model(
            "large-v2", device="cuda", compute_type="float16")

        self.alignment_models = {}

        print("Transcription models loaded...")

        print("Creating gemini client...")
        self.gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        print("Created gemini client...")

    def transcribe_video(self, base_dir: str, video_path: str) -> str:
        import whisperx

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

        print("Starting transcription with WhisperX...")
        start_time = time.time()

        audio = whisperx.load_audio(str(audio_path))
        result = self.whisperx_model.transcribe(audio, batch_size=16)

        language_code = result.get("language", "en")
        if language_code not in self.alignment_models:
            self.alignment_models[language_code] = whisperx.load_align_model(
                language_code=language_code,
                device="cuda",
            )
        alignment_model, metadata = self.alignment_models[language_code]

        result = whisperx.align(
            result["segments"],
            alignment_model,
            metadata,
            audio,
            device="cuda",
            return_char_alignments=False
        )

        duration = time.time() - start_time
        print("Transcription and alignment took " + str(duration) + " seconds")

        segments = []

        if "word_segments" in result:
            for word_segment in result["word_segments"]:
                # Skip words without timing data (common with punctuation or alignment failures)
                if "start" not in word_segment or "end" not in word_segment:
                    continue
                segments.append({
                    "start": word_segment["start"],
                    "end": word_segment["end"],
                    "word": word_segment.get("word", ""),
                })

        return json.dumps(segments)

    def identify_moments(self, transcript: list) -> str:
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
                print(
                    f"Gemini request failed (attempt {attempt + 1}/5); "
                    f"retrying in {wait_seconds}s: {last_error}"
                )
                time.sleep(wait_seconds)

        raise RuntimeError("Gemini moment selection failed after 5 attempts") from last_error

    def _identify_moments_once(self, transcript: str) -> str:
        model_name = os.environ.get(
            "GEMINI_MODEL", "gemini-3-flash-preview"
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
        print(f"Identified moments response: ${response.text}")
        if not response.text:
            raise RuntimeError("Gemini returned an empty response")
        return response.text

    @modal.fastapi_endpoint(method="POST")
    def process_video(self, request: ProcessVideoRequest, token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
        s3_key = request.s3_key

        if token.credentials != os.environ["AUTH_TOKEN"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Incorrect bearer token", headers={"WWW-Authenticate": "Bearer"})

        run_id = str(uuid.uuid4())
        base_dir = pathlib.Path("/tmp") / run_id
        base_dir.mkdir(parents=True, exist_ok=True)

        try:
            video_path = base_dir / "input.mp4"
            import boto3
            s3_client = boto3.client("s3")
            s3_client.download_file(
                os.environ["S3_BUCKET_NAME"], s3_key, str(video_path)
            )

            transcript_segments_json = self.transcribe_video(
                base_dir, video_path
            )
            transcript_segments = json.loads(transcript_segments_json)

            print("Identifying clip moments")
            identified_moments_raw = self.identify_moments(
                transcript_segments
            )
            clip_moments = parse_clip_moments(
                identified_moments_raw, transcript_segments
            )[:request.max_clips]
            print(f"Validated clip moments: {clip_moments}")

            processed_clips = 0
            failures = []
            for index, moment in enumerate(clip_moments):
                print(
                    f"Processing clip {index} from {moment['start']} "
                    f"to {moment['end']}"
                )
                try:
                    process_clip(
                        base_dir,
                        video_path,
                        s3_key,
                        moment["start"],
                        moment["end"],
                        index,
                        transcript_segments,
                    )
                    processed_clips += 1
                except Exception as error:
                    failures.append(f"clip {index}: {error}")
                    print(f"Failed to process clip {index}: {error}")

            if failures and processed_clips == 0:
                raise RuntimeError(
                    "All selected clips failed: " + "; ".join(failures)
                )

            return {
                "clips_selected": len(clip_moments),
                "clips_processed": processed_clips,
                "clips_failed": len(failures),
            }
        finally:
            if base_dir.exists():
                print(f"Cleaning up temp dir after {base_dir}")
                shutil.rmtree(base_dir, ignore_errors=True)


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
