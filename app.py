import json
import os
import re
import time
import io
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import numpy as np
import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ----------------------------
# MoviePy (works across versions)
# ----------------------------
try:
    # MoviePy v1.x
    from moviepy.editor import (
        ImageClip,
        AudioFileClip,
        CompositeVideoClip,
        concatenate_videoclips,
        concatenate_audioclips,
        AudioClip,
    )
except Exception:
    # MoviePy v2+ (no moviepy.editor)
    from moviepy.video.VideoClip import ImageClip
    from moviepy.audio.io.AudioFileClip import AudioFileClip
    from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip
    from moviepy.video.compositing.concatenate import concatenate_videoclips
    from moviepy.audio.AudioClip import concatenate_audioclips, AudioClip

# FFmpeg (Streamlit Cloud)
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# ----------------------------
# Google GenAI SDK (google-genai)
# ----------------------------
from google import genai
from google.genai import types

# =========================
# App constants
# =========================
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE  # 5s each
CROSSFADE = 0.6  # smooth crossfade (no black gap)
AUDIO_FPS = 44100

SUB_FONT_SIZE = 64
SUB_MARGIN_BOTTOM = 140
SUB_BOX_PAD_X = 60
SUB_BOX_PAD_Y = 36
SUB_BOX_RADIUS = 36

TEXT_MODEL = "models/gemini-2.5-flash"

# =========================
# Paths / Storage
# =========================
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE_DIR = BASE / "cache"

for d in (IMG_DIR, AUD_DIR, VID_DIR, CACHE_DIR):
    d.mkdir(exist_ok=True)

TOPIC_HISTORY_FILE = CACHE_DIR / "topics_history.json"
REELS_DB_FILE = CACHE_DIR / "reels_db.json"

# =========================
# Helpers
# =========================
def slugify(t: str) -> str:
    t = (t or "").lower().strip()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    return t.strip("_")[:70] or "reel"

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def ensure_history() -> List[str]:
    return load_json(TOPIC_HISTORY_FILE, [])

def ensure_db() -> Dict[str, Any]:
    return load_json(REELS_DB_FILE, {"reels": {}})

def now_ts() -> int:
    return int(time.time())

def safe_json_parse(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        # try extract between first { and last }
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j != -1 and j > i:
            return json.loads(raw[i : j + 1])
        raise

def clip_with_duration(clip, d: float):
    if hasattr(clip, "set_duration"):
        return clip.set_duration(d)
    if hasattr(clip, "with_duration"):
        return clip.with_duration(d)
    return clip

def clip_with_audio(clip, audio):
    if hasattr(clip, "set_audio"):
        return clip.set_audio(audio)
    if hasattr(clip, "with_audio"):
        return clip.with_audio(audio)
    return clip

def audio_subclip(audio, t0: float, t1: float):
    if hasattr(audio, "subclip"):
        return audio.subclip(t0, t1)
    if hasattr(audio, "subclipped"):
        return audio.subclipped(t0, t1)
    return audio

# =========================
# Pexels
# =========================
def pexels_search(query: str, per_page: int, api_key: str) -> List[Dict[str, Any]]:
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": api_key},
        params={"query": query, "per_page": per_page, "orientation": "portrait"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("photos", [])

def download_image(url: str, out_path: Path) -> Path:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return out_path

def open_and_fit(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((W, H))
    return img

def make_placeholder(out_path: Path, text: str) -> Path:
    img = Image.new("RGB", (W, H), (20, 20, 26))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.text((60, 80), "PLACEHOLDER", fill=(220, 220, 220), font=font)
    draw.text((60, 140), (text or "")[:220], fill=(230, 230, 230), font=font)
    img.save(out_path, quality=95)
    return out_path

# =========================
# Subtitles overlay (PNG with alpha)
# =========================
def load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
        "DejaVuSans.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

SUB_FONT = load_font(SUB_FONT_SIZE)

def wrap_lines(text: str, max_chars: int = 28) -> List[str]:
    words = (text or "").strip().split()
    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        if len(test) <= max_chars:
            cur.append(w)
        else:
            if cur:
                lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return lines[:2] or [""]

def make_subtitle_png(text: str, out_path: Path) -> Path:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    lines = wrap_lines(text, max_chars=28)
    spacing = 10

    widths, heights = [], []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=SUB_FONT)
        widths.append(bbox[2] - bbox[0])
        heights.append(bbox[3] - bbox[1])

    text_w = max(widths) if widths else 0
    text_h = sum(heights) + spacing * (len(lines) - 1)

    box_w = text_w + 2 * SUB_BOX_PAD_X
    box_h = text_h + 2 * SUB_BOX_PAD_Y

    x1 = (W - box_w) // 2
    y2 = H - SUB_MARGIN_BOTTOM
    y1 = y2 - box_h
    x2 = x1 + box_w

    draw.rounded_rectangle((x1, y1, x2, y2), radius=SUB_BOX_RADIUS, fill=(10, 10, 12, 220))

    y = y1 + SUB_BOX_PAD_Y
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=SUB_FONT)
        lw = bbox[2] - bbox[0]
        lx = (W - lw) // 2
        draw.text((lx, y), line, font=SUB_FONT, fill=(255, 255, 255, 255))
        y += heights[i] + spacing

    img.save(out_path)
    return out_path

# =========================
# Audio utilities (force exact duration)
# =========================
def silence_audio(duration: float) -> AudioClip:
    def make_frame(t):
        return np.zeros((1,), dtype=np.float32)
    return AudioClip(make_frame, duration=duration, fps=AUDIO_FPS)

def fit_audio_to_duration(audio: AudioFileClip, duration: float) -> AudioClip:
    dur = float(getattr(audio, "duration", 0) or 0)
    if dur > duration:
        return audio_subclip(audio, 0, duration)
    if dur < duration:
        pad = silence_audio(duration - dur)
        a = concatenate_audioclips([audio, pad])
        return clip_with_duration(a, duration)
    return audio

def make_scene_audio(scene_text: str, out_mp3: Path, duration: float) -> AudioClip:
    gTTS(scene_text).save(str(out_mp3))
    a = AudioFileClip(str(out_mp3))
    return fit_audio_to_duration(a, duration)

# =========================
# Gemini
# =========================
def gemini_client(api_key: str):
    return genai.Client(api_key=api_key)

def gemini_generate_topics(client, existing: List[str], n: int = 20) -> List[str]:
    existing_lower = {e.strip().lower() for e in existing if e.strip()}
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Generate {n} unique YouTube Shorts science/curiosity topics.
Constraints:
- Each topic is a short question (max 12 words).
- Avoid duplicates and near-duplicates.
- Avoid any topic that matches this existing list (case-insensitive):
{list(existing_lower)[:300]}

JSON format exactly:
{{"topics": ["topic 1", "topic 2", "..."]}}
"""
    resp = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = safe_json_parse(getattr(resp, "text", "") or "")
    topics = []
    for t in data.get("topics", []):
        t = (t or "").strip()
        if not t:
            continue
        if t.lower() in existing_lower:
            continue
        topics.append(t)
        existing_lower.add(t.lower())
        if len(topics) >= n:
            break
    return topics

def gemini_script(client, topic: str, scenes: int) -> Dict[str, Any]:
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Create a YouTube Short script.
Topic: "{topic}"

Rules:
- Exactly {scenes} scenes.
- Each scene is spoken in ~10 seconds.
- Each scene must have:
  - "subtitle": spoken line(s) for the 10s scene (1–2 sentences)
  - "image_query": a Pexels search phrase for that scene
- Keep accurate and simple.
- Do NOT mention any other topics.

JSON format exactly:
{{
  "title": "{topic}",
  "scenes": [
    {{"subtitle": " ... ", "image_query": " ... "}},
    ...
  ]
}}
"""
    resp = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = safe_json_parse(getattr(resp, "text", "") or "")

    scenes_list = data.get("scenes", [])
    if not isinstance(scenes_list, list):
        scenes_list = []
    scenes_list = scenes_list[:scenes]
    while len(scenes_list) < scenes:
        scenes_list.append({"subtitle": "Here is the key idea in simple terms.", "image_query": topic})

    cleaned = []
    for s in scenes_list:
        subtitle = (s.get("subtitle") or "").strip()
        image_query = (s.get("image_query") or topic).strip()
        if not subtitle:
            subtitle = "Here is the key idea in simple terms."
        if not image_query:
            image_query = topic
        cleaned.append({"subtitle": subtitle, "image_query": image_query})

    return {"title": topic, "scenes": cleaned}

# =========================
# Build one reel
# =========================
def build_reel(
    topic: str,
    script: Dict[str, Any],
    reel_index: int,
    pexels_key: str,
    progress_cb=None,
) -> Path:
    t0 = time.time()

    def cb(p: float, msg: str):
        if progress_cb:
            progress_cb(p, msg, time.time() - t0)

    scenes = script["scenes"]
    scenes_count = len(scenes)
    total_seconds = scenes_count * SCENE_SECONDS

    reel_id = f"reel{reel_index:02d}_{slugify(topic)}_{now_ts()}"

    # 1) Images
    cb(0.05, "Fetching images...")
    all_scene_images: List[List[Path]] = []
    for i, sc in enumerate(scenes, start=1):
        q = sc["image_query"]
        paths: List[Path] = []
        try:
            photos = pexels_search(q, per_page=25, api_key=pexels_key)
        except Exception:
            photos = []

        picked = photos[:IMAGES_PER_SCENE] if photos else []
        for j in range(1, IMAGES_PER_SCENE + 1):
            out = IMG_DIR / f"{reel_id}_s{i:02d}_i{j:02d}.jpg"
            ok = False
            if j <= len(picked):
                src = picked[j - 1].get("src", {})
                url = src.get("portrait") or src.get("large2x") or src.get("large")
                if url:
                    try:
                        download_image(url, out)
                        img = open_and_fit(out)
                        img.save(out, quality=92)
                        ok = True
                    except Exception:
                        ok = False
            if not ok:
                make_placeholder(out, f"{topic} / scene {i}")
            paths.append(out)
        all_scene_images.append(paths)
        cb(0.05 + 0.35 * (i / scenes_count), f"Images {i}/{scenes_count}")
        time.sleep(0.12)

    # 2) Audio per scene (exact 10s), then concatenate
    cb(0.45, "Generating voiceover (scene-synced)...")
    audio_clips = []
    for i, sc in enumerate(scenes, start=1):
        mp3 = AUD_DIR / f"{reel_id}_scene_{i:02d}.mp3"
        a = make_scene_audio(sc["subtitle"], mp3, duration=SCENE_SECONDS)
        audio_clips.append(a)
        cb(0.45 + 0.20 * (i / scenes_count), f"Voiceover {i}/{scenes_count}")

    full_audio = concatenate_audioclips(audio_clips)
    full_audio = clip_with_duration(full_audio, total_seconds)

    # 3) Video per scene = 10s (2 images x 5s) + subtitle overlay + smooth crossfades
    cb(0.70, "Building video (subtitles + smooth transitions)...")
    scene_videos = []
    d_img = min(CROSSFADE, IMG_SECONDS * 0.45)
    d_scene = min(CROSSFADE, 0.8)

    for i, sc in enumerate(scenes, start=1):
        imgs = all_scene_images[i - 1]
        subtitle = sc["subtitle"]

        c1 = ImageClip(str(imgs[0]))
        c1 = clip_with_duration(c1, IMG_SECONDS)

        c2 = ImageClip(str(imgs[1]))
        c2 = clip_with_duration(c2, IMG_SECONDS)

        if hasattr(c2, "crossfadein"):
            c2 = c2.crossfadein(d_img)

        scene_clip = concatenate_videoclips([c1, c2], method="compose", padding=-d_img)
        scene_clip = clip_with_duration(scene_clip, SCENE_SECONDS)

        sub_png = IMG_DIR / f"{reel_id}_sub_{i:02d}.png"
        make_subtitle_png(subtitle, sub_png)
        sub_clip = ImageClip(str(sub_png))
        sub_clip = clip_with_duration(sub_clip, SCENE_SECONDS)

        composed = CompositeVideoClip([scene_clip, sub_clip], size=(W, H))
        composed = clip_with_duration(composed, SCENE_SECONDS)
        composed = clip_with_audio(composed, audio_clips[i - 1])

        scene_videos.append(composed)
        cb(0.70 + 0.20 * (i / scenes_count), f"Scene {i}/{scenes_count}")

    # Crossfade between scenes
    for k in range(1, len(scene_videos)):
        if hasattr(scene_videos[k], "crossfadein"):
            scene_videos[k] = scene_videos[k].crossfadein(d_scene)

    final_video = concatenate_videoclips(scene_videos, method="compose", padding=-d_scene)
    final_video = clip_with_duration(final_video, total_seconds)
    final_video = clip_with_audio(final_video, full_audio)

    # 4) Export
    cb(0.93, "Exporting MP4...")
    out = VID_DIR / f"{reel_id}.mp4"
    final_video.write_videofile(
        str(out),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        threads=2,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        logger=None,
    )
    cb(1.0, "Done.")

    try:
        final_video.close()
    except Exception:
        pass

    return out

# =========================
# Batch zip
# =========================
def make_zip_bytes(files: List[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f.exists():
                z.writestr(f.name, f.read_bytes())
    return buf.getvalue()

# =========================
# UI
# =========================
st.set_page_config(page_title="Reel Factory", layout="wide")

st.title("Reel Factory — Gemini Script + Pexels Images + Subtitle Overlay")
st.caption("Per reel: 10s/scene • 2 images/scene • smooth crossfade • subtitles overlay • scene-synced voice (10s each)")

# Init session state
st.session_state.setdefault("topics_20", [])
st.session_state.setdefault("topic_idx", 0)
st.session_state.setdefault("scenes_per_reel", 6)

# Secrets (Streamlit Cloud: set in App → Settings → Secrets)
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = st.secrets.get("PEXELS_API_KEY", "")

if not GEMINI_API_KEY:
    st.error("Missing GEMINI_API_KEY in Streamlit secrets.")
    st.stop()
if not PEXELS_API_KEY:
    st.error("Missing PEXELS_API_KEY in Streamlit secrets.")
    st.stop()

client = gemini_client(GEMINI_API_KEY)

colA, colB = st.columns([1.05, 0.95])

with colA:
    st.subheader("Step 1 — Generate Topics (non-overlapping)")

    scenes = st.selectbox("Scenes per reel", options=[6, 8], index=0)
    st.session_state["scenes_per_reel"] = scenes
    st.caption(f"Each scene is {SCENE_SECONDS}s → reel length ≈ {scenes * SCENE_SECONDS}s")

    if st.button("Generate 20 New Topics"):
        history = ensure_history()
        with st.spinner("Asking Gemini for 20 new topics..."):
            topics = gemini_generate_topics(client, history, n=20)

        # Save to history (lowercased, deduped)
        history.extend([t.lower() for t in topics])
        history = list(dict.fromkeys([x for x in history if x.strip()]))
        save_json(TOPIC_HISTORY_FILE, history)

        st.session_state["topics_20"] = topics
        st.session_state["topic_idx"] = 0

    if st.session_state["topics_20"]:
        labels = [f"Reel {i+1:02d}: {t}" for i, t in enumerate(st.session_state["topics_20"])]
        st.session_state["topic_idx"] = st.selectbox(
            "Select a reel topic",
            options=list(range(len(labels))),
            format_func=lambda i: labels[i],
            index=min(st.session_state["topic_idx"], len(labels) - 1),
        )

        selected_topic = st.session_state["topics_20"][st.session_state["topic_idx"]]
        st.write("Selected topic:")
        st.code(selected_topic)

        st.divider()
        st.subheader("Step 3 — Batch Build (choose 1–20 reels)")

        max_n = len(st.session_state["topics_20"])
        n_to_build = st.slider("How many reels to build now?", 1, max_n, min(2, max_n))
        st.caption("Batch build auto-generates scripts and videos for the first N topics from the list.")

    else:
        st.info("Click **Generate 20 New Topics** to populate the dropdown and batch controls.")

with colB:
    st.subheader("Step 2 — Create Script + Build Video (single)")

    if not st.session_state["topics_20"]:
        st.warning("Generate topics first (Step 1).")
    else:
        topic = st.session_state["topics_20"][st.session_state["topic_idx"]].strip()
        reel_key = topic.lower()

        db = ensure_db()
        saved = db["reels"].get(reel_key)

        if saved:
            st.success("Saved in library.")
            if saved.get("script"):
                with st.expander("View saved script", expanded=False):
                    st.json(saved["script"])
            if saved.get("video_path") and Path(saved["video_path"]).exists():
                st.video(saved["video_path"])
                vp = Path(saved["video_path"])
                st.download_button(
                    "Download this MP4",
                    data=vp.read_bytes(),
                    file_name=vp.name,
                    mime="video/mp4",
                )

        if st.button("Generate Script (Gemini)"):
            with st.spinner("Generating script..."):
                script = gemini_script(client, topic, scenes=st.session_state["scenes_per_reel"])
            db = ensure_db()
            db["reels"].setdefault(reel_key, {})
            db["reels"][reel_key].update(
                {"topic": topic, "script": script, "updated_at": now_ts()}
            )
            save_json(REELS_DB_FILE, db)
            st.success("Script saved.")
            st.json(script)

        db = ensure_db()
        script = db["reels"].get(reel_key, {}).get("script")

        if script:
            st.caption("Build uses 2 images/scene + subtitles overlay + crossfade + 10s-per-scene voice sync.")

            progress = st.progress(0)
            status = st.empty()
            eta = st.empty()

            def cb(p, msg, elapsed):
                progress.progress(int(p * 100))
                status.write(f"{int(p*100)}% — {msg}")
                if p > 0:
                    remaining = (elapsed / p) - elapsed
                    eta.write(f"ETA ~ {int(max(0, remaining))}s")

            if st.button("Build Video (MP4)"):
                with st.spinner("Building MP4..."):
                    out = build_reel(topic, script, reel_index=1, pexels_key=PEXELS_API_KEY, progress_cb=cb)

                db = ensure_db()
                db["reels"][reel_key]["video_path"] = str(out)
                db["reels"][reel_key]["updated_at"] = now_ts()
                save_json(REELS_DB_FILE, db)

                st.success("Video created.")
                st.video(str(out))
                st.download_button(
                    "Download MP4",
                    data=Path(out).read_bytes(),
                    file_name=Path(out).name,
                    mime="video/mp4",
                )

# -------------------------
# Batch build section (bottom)
# -------------------------
if st.session_state["topics_20"]:
    st.divider()
    st.subheader("Batch Build Runner")

    max_n = len(st.session_state["topics_20"])
    n_to_build = st.session_state.get("batch_n", None)
    # Read current slider value from the left column by re-deriving
    # (Streamlit reruns; safe to compute again)
    n_to_build = st.slider("How many reels to build now? (repeat)", 1, max_n, min(2, max_n), key="batch_n")

    overall_bar = st.progress(0)
    current_bar = st.progress(0)
    overall_status = st.empty()
    current_status = st.empty()
    overall_eta = st.empty()

    if st.button(f"Build {n_to_build} reels now (auto script + video)"):
        built_files: List[Path] = []
        started = time.time()

        for idx in range(n_to_build):
            topic = st.session_state["topics_20"][idx].strip()
            reel_key = topic.lower()

            # Per-reel progress callback
            def per_cb(p, msg, elapsed):
                # current reel bar
                current_bar.progress(int(p * 100))
                current_status.write(f"Reel {idx+1}/{n_to_build} — {int(p*100)}% — {msg}")

                # overall progress
                overall_p = (idx + p) / n_to_build
                overall_bar.progress(int(overall_p * 100))
                overall_status.write(f"Overall {int(overall_p*100)}% — Building reel {idx+1}/{n_to_build}")

                # overall ETA
                elapsed_total = time.time() - started
                if overall_p > 0:
                    rem = (elapsed_total / overall_p) - elapsed_total
                    overall_eta.write(f"Overall ETA ~ {int(max(0, rem))}s")

            # Generate script if missing
            db = ensure_db()
            if reel_key not in db["reels"] or not db["reels"][reel_key].get("script"):
                overall_status.write(f"Overall — generating script for reel {idx+1}/{n_to_build}")
                script = gemini_script(client, topic, scenes=st.session_state["scenes_per_reel"])
                db["reels"].setdefault(reel_key, {})
                db["reels"][reel_key].update({"topic": topic, "script": script, "updated_at": now_ts()})
                save_json(REELS_DB_FILE, db)
            else:
                script = db["reels"][reel_key]["script"]

            # Build video
            out = build_reel(topic, script, reel_index=idx + 1, pexels_key=PEXELS_API_KEY, progress_cb=per_cb)

            db = ensure_db()
            db["reels"].setdefault(reel_key, {})
            db["reels"][reel_key].update({"video_path": str(out), "updated_at": now_ts()})
            save_json(REELS_DB_FILE, db)

            built_files.append(Path(out))

        overall_bar.progress(100)
        current_bar.progress(100)
        overall_status.write("Overall 100% — Batch complete.")
        current_status.write("All reels completed.")

        # Show downloads
        st.success(f"Built {len(built_files)} reels.")

        # ZIP download (batch)
        zip_bytes = make_zip_bytes(built_files)
        st.download_button(
            "Download ALL as ZIP",
            data=zip_bytes,
            file_name=f"reels_{now_ts()}.zip",
            mime="application/zip",
        )

        # Individual downloads
        st.subheader("Individual downloads")
        for f in built_files:
            if f.exists():
                st.write(f.name)
                st.download_button(
                    f"Download {f.name}",
                    data=f.read_bytes(),
                    file_name=f.name,
                    mime="video/mp4",
                    key=f"dl_{f.name}",
                )
