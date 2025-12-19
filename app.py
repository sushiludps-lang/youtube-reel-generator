import json
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# MoviePy v1.0.3 stable import (Streamlit Cloud safe)
from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip,
    vfx,
)

# FFmpeg path for Streamlit Cloud
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# Google GenAI SDK
from google import genai
from google.genai import types

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
LATEST_TOPICS_FILE = CACHE_DIR / "latest_topics.json"  # NEW

# =========================
# Secrets
# =========================
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)

TEXT_MODEL = "models/gemini-2.5-flash"

# =========================
# Video constants
# =========================
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE  # 5s each
CROSSFADE = 0.6  # smooth crossfade (no black gap)
AUDIO_FPS = 44100

# Subtitle styling
SUB_FONT_SIZE = 64
SUB_MARGIN_BOTTOM = 140
SUB_BOX_PAD_X = 60
SUB_BOX_PAD_Y = 36
SUB_BOX_RADIUS = 36

# =========================
# Utilities
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

def load_latest_topics() -> List[str]:
    return load_json(LATEST_TOPICS_FILE, [])

def save_latest_topics(topics: List[str]):
    save_json(LATEST_TOPICS_FILE, topics)

def pexels_search(query: str, per_page: int = 20) -> List[Dict[str, Any]]:
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
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
    draw.text((60, 140), text[:220], fill=(230, 230, 230), font=font)
    img.save(out_path, quality=95)
    return out_path

# =========================
# Subtitles as overlay frames (NOT burned onto images)
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

def rounded_rect(draw: ImageDraw.ImageDraw, xy, radius, fill):
    draw.rounded_rectangle(xy, radius=radius, fill=fill)

def make_subtitle_png(text: str, out_path: Path) -> Path:
    # Transparent subtitle layer
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    lines = wrap_lines(text, max_chars=28)
    # Measure
    spacing = 10
    widths = []
    heights = []
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

    # Dark box
    rounded_rect(draw, (x1, y1, x2, y2), SUB_BOX_RADIUS, fill=(10, 10, 12, 220))

    # Draw lines centered
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
# Audio: per scene -> exactly 10s
# =========================
def silence_audio(duration: float) -> AudioClip:
    def make_frame(t):
        return np.zeros((1,), dtype=np.float32)  # mono silence
    return AudioClip(make_frame, duration=duration, fps=AUDIO_FPS)

def fit_audio_to_duration(audio: AudioFileClip, duration: float) -> AudioClip:
    if audio.duration > duration:
        return audio.subclip(0, duration)
    if audio.duration < duration:
        pad = silence_audio(duration - audio.duration)
        return concatenate_audioclips([audio, pad]).set_duration(duration)
    return audio

def make_scene_audio(scene_text: str, out_mp3: Path, duration: float) -> AudioClip:
    gTTS(scene_text).save(str(out_mp3))
    a = AudioFileClip(str(out_mp3))
    return fit_audio_to_duration(a, duration)

# =========================
# Gemini: Topics (20) without overlap
# =========================
def gemini_generate_topics(existing: List[str], n: int = 20) -> List[str]:
    existing_lower = {e.strip().lower() for e in existing if e.strip()}
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Generate {n} unique YouTube Shorts science/curiosity topics.
Constraints:
- Each topic should be a short question (max 12 words).
- Avoid duplicates and near-duplicates.
- Avoid any topic that matches this existing list (case-insensitive):
{list(existing_lower)[:200]}

JSON format exactly:
{{"topics": ["topic 1", "topic 2", "..."]}}
"""
    resp = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    raw = (resp.text or "").strip()

    # HARDEN JSON parsing
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON between { ... }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(raw[start:end+1])
        else:
            raise ValueError("Gemini did not return valid JSON for topics.")

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

# =========================
# Gemini: Script for N scenes (each 10s)
# =========================
def gemini_script(topic: str, scenes: int) -> Dict[str, Any]:
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Create a short script for a YouTube Short topic.
Topic: "{topic}"

Rules:
- Exactly {scenes} scenes.
- Each scene should be spoken in ~10 seconds.
- Each scene should have:
  - "subtitle": short spoken line(s) for that 10s scene (1–2 sentences)
  - "image_query": a search phrase for stock footage/images for that scene
- Keep it accurate and simple.
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
    raw = (resp.text or "").strip()

    # HARDEN JSON parsing
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(raw[start:end+1])
        else:
            raise ValueError("Gemini did not return valid JSON for script.")

    scenes_list = data.get("scenes", [])
    if not isinstance(scenes_list, list):
        scenes_list = []

    scenes_list = scenes_list[:scenes]
    while len(scenes_list) < scenes:
        scenes_list.append({"subtitle": "One simple fact about this.", "image_query": topic})

    cleaned = []
    for s in scenes_list:
        subtitle = (s.get("subtitle") or "").strip()
        image_query = (s.get("image_query") or topic).strip()
        if not subtitle:
            subtitle = "Here is the key idea in simple terms."
        cleaned.append({"subtitle": subtitle, "image_query": image_query})

    return {"title": topic, "scenes": cleaned}

# =========================
# Build one reel
# =========================
def build_reel(topic: str, script: Dict[str, Any], reel_index: int, progress_cb=None) -> Path:
    start = time.time()

    def cb(p: float, msg: str):
        if progress_cb:
            progress_cb(p, msg, time.time() - start)

    cb(0.02, "Preparing script...")
    scenes = script["scenes"]
    scenes_count = len(scenes)
    total_seconds = scenes_count * SCENE_SECONDS

    reel_id = f"reel{reel_index:02d}_{slugify(topic)}_{int(time.time())}"

    # 1) Images
    cb(0.08, "Fetching images...")
    all_scene_image_pairs: List[List[Path]] = []
    for i, sc in enumerate(scenes, start=1):
        q = sc["image_query"]
        paths = []
        try:
            photos = pexels_search(q, per_page=25)
        except Exception:
            photos = []

        picked = photos[:IMAGES_PER_SCENE] if photos else []

        for j in range(1, IMAGES_PER_SCENE + 1):
            out = IMG_DIR / f"{reel_id}_s{i:02d}_i{j:02d}.jpg"
            if j <= len(picked):
                src = picked[j - 1].get("src", {})
                url = src.get("portrait") or src.get("large2x") or src.get("large")
                if url:
                    try:
                        download_image(url, out)
                        img = open_and_fit(out)
                        img.save(out, quality=92)
                        paths.append(out)
                        continue
                    except Exception:
                        pass
            paths.append(make_placeholder(out, f"{topic} / scene {i}"))
        all_scene_image_pairs.append(paths)
        cb(0.08 + 0.35 * (i / scenes_count), f"Images {i}/{scenes_count}")
        time.sleep(0.15)

    # 2) Audio per scene => exactly 10s
    cb(0.45, "Generating voiceover (scene-synced)...")
    audio_clips = []
    for i, sc in enumerate(scenes, start=1):
        mp3 = AUD_DIR / f"{reel_id}_scene_{i:02d}.mp3"
        a = make_scene_audio(sc["subtitle"], mp3, duration=SCENE_SECONDS)
        audio_clips.append(a)
        cb(0.45 + 0.18 * (i / scenes_count), f"Voiceover {i}/{scenes_count}")

    full_audio = concatenate_audioclips(audio_clips).set_duration(total_seconds)

    # 3) Build video
    cb(0.68, "Building video (subtitles + transitions)...")
    scene_videos = []
    for i, sc in enumerate(scenes, start=1):
        imgs = all_scene_image_pairs[i - 1]
        subtitle = sc["subtitle"]

        c1 = ImageClip(str(imgs[0])).set_duration(IMG_SECONDS)
        c2 = ImageClip(str(imgs[1])).set_duration(IMG_SECONDS)

        c1 = c1.fx(vfx.resize, 1.03)
        c2 = c2.fx(vfx.resize, 1.03)

        d = min(CROSSFADE, IMG_SECONDS * 0.45)
        c2 = c2.crossfadein(d)
        scene_clip = concatenate_videoclips([c1, c2], method="compose", padding=-d).set_duration(SCENE_SECONDS)

        sub_png = IMG_DIR / f"{reel_id}_sub_{i:02d}.png"
        make_subtitle_png(subtitle, sub_png)
        sub_clip = ImageClip(str(sub_png)).set_duration(SCENE_SECONDS)

        composed = CompositeVideoClip([scene_clip, sub_clip], size=(W, H)).set_duration(SCENE_SECONDS)
        composed = composed.set_audio(audio_clips[i - 1]).set_duration(SCENE_SECONDS)

        scene_videos.append(composed)
        cb(0.68 + 0.20 * (i / scenes_count), f"Scene {i}/{scenes_count}")

    d_scene = min(CROSSFADE, 0.8)
    for k in range(1, len(scene_videos)):
        scene_videos[k] = scene_videos[k].crossfadein(d_scene)

    final_video = concatenate_videoclips(scene_videos, method="compose", padding=-d_scene).set_duration(total_seconds)
    final_video = final_video.set_audio(full_audio).set_duration(total_seconds)

    # 4) Export
    cb(0.92, "Exporting MP4...")
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
# UI
# =========================
st.set_page_config(page_title="Reel Factory", layout="wide")
st.title("Reel Factory — Gemini Script + Pexels Images + Subtitle Overlay")

# Init session state
if "topics_20" not in st.session_state:
    st.session_state["topics_20"] = []
if "selected_topic" not in st.session_state:
    st.session_state["selected_topic"] = ""
if "scenes" not in st.session_state:
    st.session_state["scenes"] = 6

# Restore last topics after reruns/restarts
if not st.session_state["topics_20"]:
    st.session_state["topics_20"] = load_latest_topics()
    if st.session_state["topics_20"] and not st.session_state["selected_topic"]:
        st.session_state["selected_topic"] = st.session_state["topics_20"][0]

colA, colB = st.columns([1.1, 0.9])

with colA:
    st.subheader("Step 1 — Generate 20 Topics (non-overlapping)")
    scenes = st.selectbox("Scenes per reel", options=[6, 8], index=0)
    st.session_state["scenes"] = scenes
    total_len = scenes * SCENE_SECONDS
    st.caption(f"Each scene is {SCENE_SECONDS}s → Reel length ≈ {total_len}s")

    if st.button("Generate 20 New Topics"):
        history = ensure_history()

        try:
            with st.spinner("Asking Gemini for 20 new topics..."):
                topics = gemini_generate_topics(history, n=20)

            if not topics:
                st.error("Gemini returned 0 topics. Try again.")
            else:
                st.session_state["topics_20"] = topics
                st.session_state["selected_topic"] = topics[0]
                save_latest_topics(topics)

                history.extend([t.strip().lower() for t in topics if t.strip()])
                history = list(dict.fromkeys(history))[-200:]
                save_json(TOPIC_HISTORY_FILE, history)

                st.success("20 topics generated.")

        except Exception as e:
            st.error(f"Topic generation failed: {e}")
            st.info("Streamlit Cloud → Manage app → Logs shows full details.")

    if st.session_state["topics_20"]:
        labels = [f"Reel {i+1}: {t}" for i, t in enumerate(st.session_state["topics_20"])]
        chosen = st.selectbox("Select a reel topic", options=labels, index=0)
        idx = labels.index(chosen)
        topic = st.session_state["topics_20"][idx]
        st.session_state["selected_topic"] = topic
        st.write("Selected topic:")
        st.code(topic)
    else:
        st.info("Click 'Generate 20 New Topics' to populate the dropdown.")

with colB:
    st.subheader("Step 2 — Create Script + Build Video")
    topic = st.session_state.get("selected_topic", "").strip()
    if not topic:
        st.warning("Generate topics and select one first.")
    else:
        db = ensure_db()
        reel_key = topic.lower()

        exists = reel_key in db["reels"]
        if exists:
            st.success("This topic already has a saved script/video.")
            st.json(db["reels"][reel_key]["script"])
            vp = db["reels"][reel_key].get("video_path")
            if vp and Path(vp).exists():
                st.video(vp)

        if st.button("Generate Script (Gemini)"):
            try:
                with st.spinner("Generating script from Gemini..."):
                    script = gemini_script(topic, scenes=st.session_state["scenes"])
                db["reels"][reel_key] = db["reels"].get(reel_key, {})
                db["reels"][reel_key]["topic"] = topic
                db["reels"][reel_key]["script"] = script
                db["reels"][reel_key]["updated_at"] = int(time.time())
                save_json(REELS_DB_FILE, db)
                st.success("Script saved.")
                st.json(script)
            except Exception as e:
                st.error(f"Script generation failed: {e}")

        db = ensure_db()
        script = db["reels"].get(reel_key, {}).get("script")

        if script:
            st.caption("Build uses: 2 images/scene, subtitles overlay, smooth crossfade, scene-synced voiceover (10s per scene).")

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
                try:
                    with st.spinner("Building MP4..."):
                        out = build_reel(topic, script, reel_index=1, progress_cb=cb)

                    db = ensure_db()
                    db["reels"][reel_key]["video_path"] = str(out)
                    db["reels"][reel_key]["updated_at"] = int(time.time())
                    save_json(REELS_DB_FILE, db)

                    st.success("Video created and saved.")
                    st.video(str(out))
                    st.download_button("Download MP4", open(out, "rb"), file_name=out.name, mime="video/mp4")
                except Exception as e:
                    st.error(f"Video build failed: {e}")
