import json
import os
import time
import re
from pathlib import Path
from typing import List

import streamlit as st
import requests
import numpy as np
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ---- MoviePy (Cloud-safe) ----
from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip,
    vfx,
)

# ---- FFmpeg fix (Streamlit Cloud) ----
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# ---- Gemini ----
from google import genai
from google.genai import types

# ===============================
# CONFIG
# ===============================
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE
CROSSFADE = 0.6
AUDIO_FPS = 44100

FONT_SIZE = 80  # BIG subtitles
SUB_MARGIN = 160

# ===============================
# PATHS
# ===============================
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE = BASE / "cache"

for d in (IMG_DIR, AUD_DIR, VID_DIR, CACHE):
    d.mkdir(exist_ok=True)

# ===============================
# SECRETS
# ===============================
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY)
TEXT_MODEL = "models/gemini-2.5-flash"

# ===============================
# HELPERS
# ===============================
def slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", t.lower())[:60]

def silence(duration: float):
    return AudioClip(lambda t: np.zeros((1,), dtype=np.float32),
                     duration=duration, fps=AUDIO_FPS)

def fit_audio(audio, duration):
    if audio.duration > duration:
        return audio.subclip(0, duration)
    if audio.duration < duration:
        return concatenate_audioclips([audio, silence(duration - audio.duration)])
    return audio

def pexels_images(query: str, n: int):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": n, "orientation": "portrait"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("photos", [])

def prepare_image(url: str, out: Path):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    img = ImageOps.fit(img, (W, H))
    img.save(out, quality=90)

def subtitle_clip(text: str, duration: float):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            FONT_SIZE,
        )
    except:
        font = ImageFont.load_default()

    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    x = (W - tw) // 2
    y = H - SUB_MARGIN - th

    d.rectangle(
        (x - 40, y - 30, x + tw + 40, y + th + 30),
        fill=(0, 0, 0, 180),
    )
    d.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    path = IMG_DIR / f"sub_{time.time_ns()}.png"
    img.save(path)

    return ImageClip(str(path)).set_duration(duration)

# ===============================
# GEMINI
# ===============================
def generate_topics(n: int):
    prompt = f"""
Return JSON only.
Generate {n} unique science curiosity YouTube Shorts topics.
Format:
{{"topics": ["..."]}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(r.text)["topics"]

def generate_script(topic: str, scenes: int):
    prompt = f"""
Return JSON only.

Topic: "{topic}"
Create exactly {scenes} scenes.
Each scene spoken for ~10 seconds.

Format:
{{
 "scenes":[
   {{"subtitle":"...", "image_query":"..."}}
 ]
}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(r.text)

# ===============================
# BUILD REEL
# ===============================
def build_reel(topic: str, script: dict, index: int):
    clips = []
    audios = []

    for i, sc in enumerate(script["scenes"], 1):
        photos = pexels_images(sc["image_query"], IMAGES_PER_SCENE)
        imgs = []

        for j in range(IMAGES_PER_SCENE):
            out = IMG_DIR / f"{slug(topic)}_{index}_{i}_{j}.jpg"
            if j < len(photos):
                prepare_image(photos[j]["src"]["portrait"], out)
            else:
                Image.new("RGB", (W, H), (30, 30, 30)).save(out)
            imgs.append(out)

        c1 = ImageClip(str(imgs[0])).set_duration(IMG_SECONDS)
        c2 = ImageClip(str(imgs[1])).set_duration(IMG_SECONDS).crossfadein(CROSSFADE)
        scene = concatenate_videoclips([c1, c2], method="compose", padding=-CROSSFADE)
        scene = scene.fx(vfx.resize, 1.02)

        # audio
        mp3 = AUD_DIR / f"{slug(topic)}_{index}_{i}.mp3"
        gTTS(sc["subtitle"]).save(str(mp3))
        a = fit_audio(AudioFileClip(str(mp3)), SCENE_SECONDS)

        sub = subtitle_clip(sc["subtitle"], SCENE_SECONDS)
        final_scene = CompositeVideoClip([scene, sub]).set_audio(a)

        clips.append(final_scene)
        audios.append(a)

    video = concatenate_videoclips(clips, method="compose", padding=-CROSSFADE)
    out = VID_DIR / f"{slug(topic)}_{index}.mp4"
    video.write_videofile(
        str(out),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        threads=2,
        logger=None,
    )
    return out

# ===============================
# UI
# ===============================
st.set_page_config("Reel Factory", layout="wide")
st.title("🎬 Reel Factory (Stable)")

num_reels = st.slider("How many reels to generate?", 1, 20, 1)

if st.button("Generate Topics"):
    st.session_state["topics"] = generate_topics(num_reels)

topics = st.session_state.get("topics", [])

if topics:
    selected = st.multiselect("Select reels to build", topics, default=topics[:1])

    if st.button("Build Selected Reels"):
        for i, t in enumerate(selected, 1):
            st.write(f"🎞️ Building: {t}")
            script = generate_script(t, 6)
            out = build_reel(t, script, i)
            st.video(str(out))
            st.download_button(
                f"⬇️ Download {t}",
                open(out, "rb"),
                file_name=out.name,
            )
