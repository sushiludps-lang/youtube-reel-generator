import json
import os
import re
import time
import io
import zipfile
from pathlib import Path
from typing import List, Dict, Any, Tuple

import numpy as np
import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# MoviePy v1.0.3 (Streamlit Cloud safe)
from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip,
    vfx,
)

# FFmpeg path (Streamlit Cloud)
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# Google GenAI SDK (new)
from google import genai
from google.genai import types

# =========================
# Page config + UI style
# =========================
st.set_page_config(page_title="Reel Factory", layout="wide")
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
      h1,h2,h3 { letter-spacing: -0.02em; }
      .card {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.08);
        padding: 16px 18px;
        border-radius: 18px;
      }
      .muted { opacity: 0.85; }
      .small { font-size: 0.92rem; opacity: 0.9; }
      .pill {
        display:inline-block; padding: 6px 10px; border-radius: 999px;
        background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10);
        margin-right: 8px; font-size: 0.88rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================
# Storage paths (Cloud-safe)
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
# Secrets
# =========================
# Streamlit Cloud -> Settings -> Secrets
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = st.secrets.get("PEXELS_API_KEY", "")

if not GEMINI_API_KEY:
    st.error("Missing GEMINI_API_KEY in Streamlit Cloud Secrets.")
    st.stop()
if not PEXELS_API_KEY:
    st.error("Missing PEXELS_API_KEY in Streamlit Cloud Secrets.")
    st.stop()

client = genai.Client(api_key=GEMINI_API_KEY)

# Text model (works on free tier if your quota allows)
TEXT_MODEL = "models/gemini-2.5-flash"

# =========================
# Video constants
# =========================
W, H = 1080, 1920
FPS = 30

SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE  # 5 seconds each image

CROSSFADE = 0.75           # smooth crossfade (no black gaps)
MICRO_ZOOM = 1.035         # subtle zoom for life
AUDIO_FPS = 44100

# Subtitle styling (overlay layer, NOT burned into image)
SUB_FONT_SIZE = 66
SUB_MARGIN_BOTTOM = 140
SUB_BOX_PAD_X = 60
SUB_BOX_PAD_Y = 36
SUB_BOX_RADIUS = 36

# =========================
# Helpers: JSON store
# =========================
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

# =========================
# Helpers: text / filenames
# =========================
def slugify(t: str) -> str:
    t = (t or "").lower().strip()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    t = t.strip("_")[:70]
    return t or "reel"

# =========================
# Pexels images
# =========================
def pexels_search(query: str, per_page: int = 30) -> List[Dict[str, Any]]:
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
    draw.text((60, 140), text[:240], fill=(230, 230, 230), font=font)
    img.save(out_path, quality=95)
    return out_path

# =========================
# Subtitle overlay PNG
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

    # rounded dark box
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
# Audio: exact 10s per scene
# =========================
def silence_audio(duration: float) -> AudioClip:
    def make_frame(t):
        return np.zeros((1,), dtype=np.float32)  # mono silence
    return AudioClip(make_frame, duration=duration, fps=AUDIO_FPS)

def fit_audio_to_duration(audio: AudioFileClip, duration: float):
    if audio.duration > duration:
        return audio.subclip(0, duration)
    if audio.duration < duration:
        pad = silence_audio(duration - audio.duration)
        return concatenate_audioclips([audio, pad]).set_duration(duration)
    return audio

def make_scene_audio(scene_text: str, out_mp3: Path, duration: float):
    gTTS(scene_text).save(str(out_mp3))
    a = AudioFileClip(str(out_mp3))
    return fit_audio_to_duration(a, duration)

# =========================
# Gemini: topics without overlap
# =========================
def gemini_generate_topics(existing: List[str], n: int = 20) -> List[str]:
    existing_lower = {e.strip().lower() for e in existing if e.strip()}
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Generate {n} unique YouTube Shorts science/curiosity topics.
Constraints:
- Each topic is a short question (max 12 words).
- Avoid duplicates and near-duplicates.
- Avoid any topic that matches this existing list (case-insensitive):
{list(existing_lower)[:400]}

JSON format exactly:
{{"topics": ["topic 1", "topic 2", "..."]}}
"""
    resp = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    raw = (resp.text or "").strip()
    data = json.loads(raw)

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
# Gemini: script for N scenes (each ~10s)
# =========================
def gemini_script(topic: str, scenes: int) -> Dict[str, Any]:
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Create a short script for a YouTube Short.
Topic: "{topic}"

Rules:
- Exactly {scenes} scenes.
- Each scene should be spoken in ~10 seconds.
- Each scene must include:
  - subtitle: spoken line(s) for the scene (1–2 sentences)
  - image_query: a stock image search phrase for that scene
- Keep it accurate, simple, and on-topic.
- Do NOT mention any other topic.

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
    data = json.loads(raw)

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
# Build one reel (script->images->audio->video)
# =========================
def build_reel(topic: str, script: Dict[str, Any], reel_index: int, progress_cb=None) -> Tuple[Path, float]:
    t0 = time.time()

    def cb(p: float, msg: str):
        if progress_cb:
            progress_cb(p, msg, time.time() - t0)

    scenes = script["scenes"]
    scenes_count = len(scenes)
    total_seconds = scenes_count * SCENE_SECONDS

    reel_id = f"reel{reel_index:02d}_{slugify(topic)}_{int(time.time())}"

    # 1) Images
    cb(0.05, "Fetching images…")
    all_scene_images: List[List[Path]] = []

    for i, sc in enumerate(scenes, start=1):
        q = sc["image_query"]
        paths: List[Path] = []

        photos = []
        try:
            photos = pexels_search(q, per_page=35)
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

            # fallback placeholder
            paths.append(make_placeholder(out, f"{topic} / scene {i}"))
        all_scene_images.append(paths)

        cb(0.05 + 0.35 * (i / scenes_count), f"Images {i}/{scenes_count}")
        time.sleep(0.10)

    # 2) Audio per scene (exact 10s each)
    cb(0.42, "Generating voiceover (scene-synced)…")
    audio_clips = []
    for i, sc in enumerate(scenes, start=1):
        mp3 = AUD_DIR / f"{reel_id}_scene_{i:02d}.mp3"
        a = make_scene_audio(sc["subtitle"], mp3, duration=SCENE_SECONDS)
        audio_clips.append(a)
        cb(0.42 + 0.18 * (i / scenes_count), f"Voiceover {i}/{scenes_count}")

    full_audio = concatenate_audioclips(audio_clips).set_duration(total_seconds)

    # 3) Video per scene (2 images x 5s with crossfade + subtitle overlay)
    cb(0.62, "Building video (smooth transitions + subtitles)…")
    scene_videos = []
    d = min(CROSSFADE, IMG_SECONDS * 0.45)

    for i, sc in enumerate(scenes, start=1):
        imgs = all_scene_images[i - 1]
        subtitle = sc["subtitle"]

        c1 = ImageClip(str(imgs[0])).set_duration(IMG_SECONDS).fx(vfx.resize, MICRO_ZOOM)
        c2 = ImageClip(str(imgs[1])).set_duration(IMG_SECONDS).fx(vfx.resize, MICRO_ZOOM)

        # Crossfade within scene
        c2 = c2.crossfadein(d)
        scene_clip = concatenate_videoclips([c1, c2], method="compose", padding=-d).set_duration(SCENE_SECONDS)

        # Subtitle overlay for whole scene
        sub_png = IMG_DIR / f"{reel_id}_sub_{i:02d}.png"
        make_subtitle_png(subtitle, sub_png)
        sub_clip = ImageClip(str(sub_png)).set_duration(SCENE_SECONDS)

        composed = CompositeVideoClip([scene_clip, sub_clip], size=(W, H)).set_duration(SCENE_SECONDS)

        # Attach exact 10s audio for this scene
        composed = composed.set_audio(audio_clips[i - 1]).set_duration(SCENE_SECONDS)

        scene_videos.append(composed)
        cb(0.62 + 0.24 * (i / scenes_count), f"Scene {i}/{scenes_count}")

    # Crossfade between scenes too (very smooth)
    d_scene = min(CROSSFADE, 0.85)
    for k in range(1, len(scene_videos)):
        scene_videos[k] = scene_videos[k].crossfadein(d_scene)

    final_video = concatenate_videoclips(scene_videos, method="compose", padding=-d_scene).set_duration(total_seconds)
    final_video = final_video.set_audio(full_audio).set_duration(total_seconds)

    # 4) Export
    cb(0.90, "Exporting MP4…")
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

    return out, (time.time() - t0)

# =========================
# Batch ZIP builder
# =========================
def make_zip_bytes(files: List[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in files:
            if f.exists():
                z.write(f, arcname=f.name)
    buf.seek(0)
    return buf.getvalue()

# =========================
# Session state init
# =========================
st.session_state.setdefault("topics_20", [])
st.session_state.setdefault("selected_topic", "")
st.session_state.setdefault("scenes_per_reel", 6)
st.session_state.setdefault("last_generated_at", 0)

# =========================
# Header
# =========================
st.markdown(
    """
    <div class="card">
      <div style="font-size:2.0rem; font-weight:700;">Reel Factory — Gemini Script + Pexels Images + Subtitle Overlay</div>
      <div class="muted small">
        Per reel: <span class="pill">10s/scene</span>
        <span class="pill">2 images/scene</span>
        <span class="pill">smooth crossfade</span>
        <span class="pill">subtitles overlay</span>
        <span class="pill">scene-synced voice</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

st.write("")

# =========================
# Layout
# =========================
left, right = st.columns([1.08, 0.92], gap="large")

# =========================
# LEFT: Step 1 topics + batch controls
# =========================
with left:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Step 1 — Generate 20 Topics (non-overlapping)")

    scenes_choice = st.selectbox("Scenes per reel", options=[6, 8], index=0)
    st.session_state["scenes_per_reel"] = scenes_choice
    st.caption(f"Each scene is {SCENE_SECONDS}s → Reel length ≈ {scenes_choice * SCENE_SECONDS}s")

    if st.button("Generate 20 New Topics", use_container_width=True):
        history = ensure_history()
        with st.spinner("Asking Gemini for 20 new topics..."):
            topics = gemini_generate_topics(history, n=20)

        # update history
        history.extend([t.strip().lower() for t in topics])
        history = list(dict.fromkeys(history))
        save_json(TOPIC_HISTORY_FILE, history)

        st.session_state["topics_20"] = topics
        if topics:
            st.session_state["selected_topic"] = topics[0]
        st.session_state["last_generated_at"] = int(time.time())

    topics_20 = st.session_state.get("topics_20", [])
    if topics_20:
        labels = [f"Reel {i+1}: {t}" for i, t in enumerate(topics_20)]
        chosen = st.selectbox("Select a reel topic", options=labels, index=0)
        idx = labels.index(chosen)
        st.session_state["selected_topic"] = topics_20[idx]
        st.write("Selected topic:")
        st.code(st.session_state["selected_topic"])
    else:
        st.info("Click 'Generate 20 New Topics' to populate the dropdown.")

    st.markdown("</div>", unsafe_allow_html=True)

    st.write("")

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Step 3 — Batch Build (auto-generate scripts + videos)")

    if not topics_20:
        st.warning("Generate 20 topics first (Step 1).")
    else:
        n_to_build = st.slider("How many reels to generate now?", min_value=1, max_value=20, value=5)
        only_missing = st.checkbox("Only build reels missing video (skip already done)", value=True)

        st.caption("Batch will run: Script → Images → Voiceover → Video → Save → ZIP downloads")

        overall = st.progress(0)
        overall_status = st.empty()
        overall_eta = st.empty()

        if st.button("Batch Build Now", use_container_width=True):
            db = ensure_db()
            built_files: List[Path] = []
            built_count = 0
            total = n_to_build

            start_batch = time.time()

            for i in range(total):
                topic = topics_20[i]
                key = topic.lower().strip()

                # skip if exists
                if only_missing and key in db["reels"] and db["reels"][key].get("video_path") and Path(db["reels"][key]["video_path"]).exists():
                    built_files.append(Path(db["reels"][key]["video_path"]))
                    built_count += 1
                    frac = built_count / total
                    overall.progress(int(frac * 100))
                    overall_status.write(f"{int(frac*100)}% — Skipped (already done): Reel {i+1}")
                    continue

                overall_status.write(f"Starting Reel {i+1}/{total}: {topic}")

                # Script
                if key not in db["reels"] or "script" not in db["reels"][key]:
                    script = gemini_script(topic, scenes=scenes_choice)
                    db["reels"].setdefault(key, {})
                    db["reels"][key]["topic"] = topic
                    db["reels"][key]["script"] = script
                    db["reels"][key]["updated_at"] = int(time.time())
                    save_json(REELS_DB_FILE, db)
                else:
                    script = db["reels"][key]["script"]

                # Build reel with per-reel progress
                reel_bar = st.progress(0)
                reel_msg = st.empty()
                reel_eta = st.empty()
                reel_start = time.time()

                def cb(p, msg, elapsed):
                    reel_bar.progress(int(p * 100))
                    reel_msg.write(f"Reel {i+1}/{total} — {int(p*100)}%: {msg}")
                    if p > 0:
                        remaining = (elapsed / p) - elapsed
                        reel_eta.write(f"Reel ETA ~ {int(max(0, remaining))}s")

                out, seconds = build_reel(topic, script, reel_index=i+1, progress_cb=cb)

                db = ensure_db()
                db["reels"].setdefault(key, {})
                db["reels"][key]["topic"] = topic
                db["reels"][key]["script"] = script
                db["reels"][key]["video_path"] = str(out)
                db["reels"][key]["build_seconds"] = float(seconds)
                db["reels"][key]["updated_at"] = int(time.time())
                save_json(REELS_DB_FILE, db)

                built_files.append(out)
                built_count += 1

                # overall progress
                frac = built_count / total
                overall.progress(int(frac * 100))
                elapsed_batch = time.time() - start_batch
                if frac > 0:
                    remaining_batch = (elapsed_batch / frac) - elapsed_batch
                else:
                    remaining_batch = 0
                overall_status.write(f"{int(frac*100)}% — Completed Reel {i+1}/{total}")
                overall_eta.write(f"Batch ETA ~ {int(max(0, remaining_batch))}s")

            # Batch downloads
            st.success("Batch complete.")

            # ZIP with MP4s
            zip_bytes = make_zip_bytes(built_files)
            st.download_button(
                "Download ALL MP4s (ZIP)",
                data=zip_bytes,
                file_name="reels_batch.zip",
                mime="application/zip",
                use_container_width=True,
            )

            # ZIP with scripts JSON
            db = ensure_db()
            scripts_buf = io.BytesIO()
            with zipfile.ZipFile(scripts_buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for i in range(total):
                    topic = topics_20[i]
                    key = topic.lower().strip()
                    script = db["reels"].get(key, {}).get("script")
                    if script:
                        z.writestr(f"reel_{i+1:02d}_{slugify(topic)}.json", json.dumps(script, ensure_ascii=False, indent=2))
            scripts_buf.seek(0)

            st.download_button(
                "Download ALL Scripts (ZIP)",
                data=scripts_buf.getvalue(),
                file_name="reels_scripts.zip",
                mime="application/zip",
                use_container_width=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)

# =========================
# RIGHT: single reel script + build + downloads + library
# =========================
with right:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Step 2 — Create Script + Build Video (single)")

    topic = (st.session_state.get("selected_topic", "") or "").strip()
    if not topic:
        st.warning("Generate topics and select one first.")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        db = ensure_db()
        key = topic.lower().strip()
        entry = db["reels"].get(key, {})

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Generate Script (Gemini)", use_container_width=True):
                with st.spinner("Generating script..."):
                    script = gemini_script(topic, scenes=st.session_state["scenes_per_reel"])
                db = ensure_db()
                db["reels"].setdefault(key, {})
                db["reels"][key]["topic"] = topic
                db["reels"][key]["script"] = script
                db["reels"][key]["updated_at"] = int(time.time())
                save_json(REELS_DB_FILE, db)
                st.success("Script saved.")

        db = ensure_db()
        entry = db["reels"].get(key, {})
        script = entry.get("script")

        if script:
            st.caption("Build uses: 2 images/scene, smooth crossfade, subtitles overlay, 10s voice per scene.")
            st.json(script)

            # downloads for script
            st.download_button(
                "Download Script JSON",
                data=json.dumps(script, ensure_ascii=False, indent=2).encode("utf-8"),
                file_name=f"{slugify(topic)}_script.json",
                mime="application/json",
                use_container_width=True,
            )

            progress = st.progress(0)
            status = st.empty()
            eta = st.empty()

            def cb(p, msg, elapsed):
                progress.progress(int(p * 100))
                status.write(f"{int(p*100)}% — {msg}")
                if p > 0:
                    remaining = (elapsed / p) - elapsed
                    eta.write(f"ETA ~ {int(max(0, remaining))}s")

            with col2:
                if st.button("Build Video (MP4)", use_container_width=True):
                    with st.spinner("Building MP4..."):
                        out, seconds = build_reel(topic, script, reel_index=1, progress_cb=cb)

                    db = ensure_db()
                    db["reels"].setdefault(key, {})
                    db["reels"][key]["topic"] = topic
                    db["reels"][key]["script"] = script
                    db["reels"][key]["video_path"] = str(out)
                    db["reels"][key]["build_seconds"] = float(seconds)
                    db["reels"][key]["updated_at"] = int(time.time())
                    save_json(REELS_DB_FILE, db)

                    st.success("Video created.")
                    st.video(str(out))

                    st.download_button(
                        "Download MP4",
                        data=open(out, "rb").read(),
                        file_name=out.name,
                        mime="video/mp4",
                        use_container_width=True,
                    )
        else:
            st.info("Generate a script first, then build video.")

        st.markdown("</div>", unsafe_allow_html=True)

    st.write("")

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader("Saved Library (cache/reels_db.json)")

    db = ensure_db()
    reels = db.get("reels", {})
    if not reels:
        st.caption("No saved reels yet.")
    else:
        items = []
        for k, v in reels.items():
            items.append({
                "topic": v.get("topic", k),
                "has_script": "script" in v,
                "has_video": bool(v.get("video_path")) and Path(v.get("video_path")).exists(),
                "build_seconds": v.get("build_seconds", None),
                "video_path": v.get("video_path", ""),
                "updated_at": v.get("updated_at", 0),
            })

        items = sorted(items, key=lambda x: x["updated_at"], reverse=True)
        for it in items[:12]:
            st.write(f"• **{it['topic']}**  | script: {it['has_script']} | video: {it['has_video']}")
            if it["has_video"]:
                vp = it["video_path"]
                st.video(vp)
                st.download_button(
                    f"Download MP4 — {it['topic'][:30]}",
                    data=open(vp, "rb").read(),
                    file_name=Path(vp).name,
                    mime="video/mp4",
                    use_container_width=True,
                )

    st.markdown("</div>", unsafe_allow_html=True)
