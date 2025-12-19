import json
import random
from io import BytesIO
from pathlib import Path

import requests
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS

# -------------------------------------------------
# MoviePy import (Cloud + Local compatible)
# -------------------------------------------------
try:
    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
    MOVIEPY_V2 = False

# -----------------------
# Paths
# -----------------------
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
IMG_DIR.mkdir(exist_ok=True)
AUD_DIR.mkdir(exist_ok=True)
VID_DIR.mkdir(exist_ok=True)

# -----------------------
# Page setup
# -----------------------
st.set_page_config(page_title="Reel Generator", layout="wide", page_icon="🎬")
st.title("YouTube Reel Generator – Full MP4 Builder (FREE Images)")

# -----------------------
# Secrets + GenAI client
# -----------------------
gemini_key = st.secrets.get("GEMINI_API_KEY", "")
if not gemini_key:
    st.error("Missing GEMINI_API_KEY in Secrets.")
    st.stop()

PEXELS_KEY = st.secrets.get("PEXELS_API_KEY", "")

client = genai.Client(api_key=gemini_key)
TEXT_MODEL = "gemini-2.5-flash"

# -----------------------
# UI inputs
# -----------------------
topic = st.text_input("Enter a topic for the YouTube Reel", value="Why does fire have no shadow?")
num_scenes = st.slider("Number of scenes", 5, 10, 7)

image_source = st.selectbox("Image source", ["Pexels (free key)", "Placeholders only"], index=0)
show_debug = st.toggle("Show image debug", value=True)

if image_source.startswith("Pexels") and not PEXELS_KEY:
    st.warning("PEXELS_API_KEY is missing. Add it in Streamlit Cloud → Manage app → Settings → Secrets.")

# -----------------------
# Helpers
# -----------------------
def _load_font(size: int = 64):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()

def make_placeholder_image(text: str, idx: int) -> Path:
    img = Image.new("RGB", (1080, 1920), (15, 15, 20))
    draw = ImageDraw.Draw(img)
    font = _load_font(64)

    draw.text((80, 120), f"Scene {idx}", fill=(200, 200, 200), font=font)

    words = text.split()
    lines, line = [], ""
    for w in words:
        if len((line + " " + w).strip()) <= 26:
            line = (line + " " + w).strip()
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)

    y = 500
    for ln in lines[:8]:
        draw.text((80, y), ln, fill=(240, 240, 240), font=font)
        y += 90

    out = IMG_DIR / f"scene_{idx}.png"
    img.save(out)
    return out

@st.cache_data(show_spinner=False, ttl=3600)
def pexels_search(query: str):
    if not PEXELS_KEY:
        return None, None, "PEXELS_API_KEY missing"

    headers = {"Authorization": PEXELS_KEY}
    params = {
        "query": query,
        "per_page": 20,
        "orientation": "portrait",
        "size": "large",
    }
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)

    if r.status_code != 200:
        return None, r.status_code, r.text[:500]

    data = r.json()
    photos = data.get("photos", [])
    if not photos:
        return None, 200, "No photos found"

    # Prefer portrait URL
    urls = []
    for p in photos:
        src = p.get("src", {})
        if src.get("portrait"):
            urls.append(src["portrait"])
        elif src.get("large"):
            urls.append(src["large"])

    if not urls:
        return None, 200, "Photos returned but no usable src URLs"

    return random.choice(urls), 200, "OK"

def download_and_fit_9x16(img_url: str, out_path: Path):
    r = requests.get(img_url, timeout=30)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")

    target_w, target_h = 1080, 1920
    target_ratio = target_w / target_h

    w, h = img.size
    src_ratio = w / h

    # Center crop to 9:16
    if src_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    img = img.resize((target_w, target_h), Image.LANCZOS)
    img.save(out_path, format="PNG")

def build_image_for_scene(scene_text: str, idx: int):
    if image_source.startswith("Pexels"):
        url, status, msg = pexels_search(scene_text)
        if show_debug:
            with st.expander(f"Pexels debug (Scene {idx})", expanded=False):
                st.write({"status": status, "message": msg, "url": url})

        if url:
            out = IMG_DIR / f"scene_{idx}.png"
            try:
                download_and_fit_9x16(url, out)
                return out
            except Exception as e:
                if show_debug:
                    with st.expander(f"Download/crop error (Scene {idx})", expanded=False):
                        st.write(str(e))

    return make_placeholder_image(scene_text, idx)

# -----------------------
# Generate everything
# -----------------------
if st.button("Generate Final MP4 Reel", type="primary"):

    script_prompt = f"""
Return ONLY valid JSON. No commentary. No markdown. No extra text.

Topic: {topic}
Scenes: {num_scenes}

Format EXACTLY:
{{
  "hook": "short hook",
  "scenes": ["scene text", "..."],
  "cta": "short CTA"
}}
""".strip()

    # Force JSON output
    result = client.models.generate_content(
        model=TEXT_MODEL,
        contents=script_prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    raw = (getattr(result, "text", "") or "").strip()
    if not raw:
        st.error("Gemini returned empty output.")
        st.stop()

    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        st.error("Gemini did not return valid JSON. Raw output below:")
        st.code(raw)
        st.stop()

    scenes = data.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        st.error("No scenes returned; cannot build video.")
        st.stop()

    st.subheader("Hook")
    st.success(data.get("hook", ""))

    images = []
    narration_parts = [data.get("hook", "")]

    st.subheader("Scenes")
    progress = st.progress(0)

    for i, scene in enumerate(scenes, start=1):
        scene_text = str(scene)
        narration_parts.append(scene_text)

        img_path = build_image_for_scene(scene_text, i)
        images.append(img_path)

        st.write(f"**Scene {i}:** {scene_text}")
        st.image(str(img_path), use_container_width=True)
        progress.progress(i / len(scenes))

    cta = data.get("cta", "")
    st.subheader("CTA")
    st.info(cta)
    narration_parts.append(cta)

    narration = ". ".join([p.strip() for p in narration_parts if p and p.strip()]) + "."

    # Voiceover
    audio_path = AUD_DIR / "voiceover.mp3"
    gTTS(narration, lang="en", slow=False).save(str(audio_path))
    st.subheader("Voiceover Preview")
    st.audio(str(audio_path))

    # Video render
    audio = AudioFileClip(str(audio_path))
    per_image = max(0.8, audio.duration / len(images))

    if MOVIEPY_V2:
        clips = [ImageClip(str(img), duration=per_image) for img in images]
        video = concatenate_videoclips(clips, method="compose").with_audio(audio)
    else:
        clips = [ImageClip(str(img)).set_duration(per_image) for img in images]
        video = concatenate_videoclips(clips, method="compose").set_audio(audio)

    out_video = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out_video), fps=30, codec="libx264", audio_codec="aac")

    st.success("Final MP4 ready")
    st.video(str(out_video))
    st.download_button(
        "Download MP4",
        data=open(out_video, "rb"),
        file_name="final_reel.mp4",
        mime="video/mp4",
    )
