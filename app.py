import textwrap
import requests
from pathlib import Path

import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, CompositeAudioClip
from moviepy.audio.AudioClip import AudioArrayClip
import numpy as np

# ===============================
# CONFIG
# ===============================
WIDTH, HEIGHT = 1080, 1920
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
for d in (IMG_DIR, AUD_DIR, VID_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

SCENES = 6
SCENE_SECONDS = 10.0
TARGET_SECONDS = 60.0
IMAGES_PER_SCENE = 2  # fixed

# ===============================
# UI
# ===============================
st.title("YouTube Reel Generator — 60s (6 scenes × 10s, 2 images/scene)")

topic = st.text_input("Topic", "Why does fire have no shadow?")

ENABLE_CAPTIONS = st.toggle("Burn captions on images", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

DEBUG = st.toggle("Show debug", False)

# ===============================
# SCRIPT (6 scenes)
# ===============================
def build_script(topic):
    return [
        f"{topic} — quick answer.",
        "A shadow forms when one strong light is blocked.",
        "Fire is glowing hot gas that emits its own light.",
        "Because it emits light, it fills in its own shadow.",
        "Flames are partly transparent, so they don’t block all light.",
        "You only see a shadow if a brighter light is behind the flame.",
    ]

script = build_script(topic)

# ===============================
# FONT LOADER (cloud-safe)
# ===============================
def load_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()

FONT = load_font(CAPTION_FONT_SIZE)

# ===============================
# CAPTION DRAW
# ===============================
def burn_caption(img, caption):
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    lines = textwrap.wrap(caption, width=28)[:3]
    line_h = int(CAPTION_FONT_SIZE * 1.2)

    box_h = 60 + line_h * len(lines) + 30
    y1 = HEIGHT - box_h - 120
    y2 = HEIGHT - 120

    draw.rectangle(
        [(60, y1), (WIDTH - 60, y2)],
        fill=(0, 0, 0, int(CAPTION_BOX_OPACITY)),
    )

    y = y1 + 35
    for line in lines:
        draw.text((90, y), line, font=FONT, fill=(255, 255, 255, 255))
        y += line_h

    return Image.alpha_composite(img, overlay).convert("RGB")

# ===============================
# PEXELS FETCH (2 images/scene)
# ===============================
def fetch_images(scene_text):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": scene_text, "per_page": 30, "orientation": "portrait", "size": "large"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    photos = r.json().get("photos", [])

    paths = []
    for i, p in enumerate(photos[:IMAGES_PER_SCENE]):
        url = p["src"].get("portrait") or p["src"].get("large")
        if not url:
            continue

        img_path = IMG_DIR / f"img_{abs(hash((scene_text, i)))}.jpg"
        img_path.write_bytes(requests.get(url, timeout=20).content)

        img = Image.open(img_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        if ENABLE_CAPTIONS:
            img = burn_caption(img, scene_text)

        img.save(img_path, quality=95)
        paths.append(img_path)

    if not paths:
        return []
    while len(paths) < IMAGES_PER_SCENE:
        paths.append(paths[-1])
    return paths[:IMAGES_PER_SCENE]

# ===============================
# AUDIO: repeat narration to fill 60s
# ===============================
def make_audio_fill_60s(narration_mp3_path: Path, target_seconds: float):
    base = AudioFileClip(str(narration_mp3_path))

    # 60s silence bed
    sr = 44100
    bed = AudioArrayClip(np.zeros((int(sr * target_seconds), 2), dtype=np.float32), fps=sr)

    # If base is longer than 60s, just trim
    if base.duration >= target_seconds and hasattr(base, "subclip"):
        base = base.subclip(0, target_seconds)
        mixed = CompositeAudioClip([bed, base])
        mixed = mixed.with_duration(target_seconds) if hasattr(mixed, "with_duration") else mixed.set_duration(target_seconds)
        return mixed, base.duration

    # Repeat by placing the same audio clip multiple times
    parts = [bed]
    t = 0.0
    while t < target_seconds - 0.01:
        placed = base.with_start(t) if hasattr(base, "with_start") else base.set_start(t)
        parts.append(placed)
        t += base.duration

    mixed = CompositeAudioClip(parts)
    mixed = mixed.with_duration(target_seconds) if hasattr(mixed, "with_duration") else mixed.set_duration(target_seconds)
    return mixed, base.duration

# ===============================
# BUILD VIDEO
# ===============================
if st.button("Generate Final MP4 Reel"):
    st.info("Generating 60s reel…")

    # 1) TTS narration
    narration_text = " ".join(script)
    voice_path = AUD_DIR / "voice.mp3"
    gTTS(narration_text).save(str(voice_path))

    audio, base_audio_dur = make_audio_fill_60s(voice_path, TARGET_SECONDS)

    # 2) Visuals: 12 images total, 5 seconds each
    per_image_dur = SCENE_SECONDS / IMAGES_PER_SCENE  # 5s
    clips = []
    img_count = 0

    for scene in script:
        imgs = fetch_images(scene)
        if not imgs:
            st.error("No images fetched. Check PEXELS_API_KEY or try a different topic.")
            st.stop()

        for img in imgs:
            clips.append(ImageClip(str(img), duration=per_image_dur))
            img_count += 1

    video = concatenate_videoclips(clips, method="compose", padding=0)

    video = video.with_audio(audio) if hasattr(video, "with_audio") else video.set_audio(audio)

    out = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    if DEBUG:
        st.write("Base narration length (seconds):", round(base_audio_dur, 2))
        st.write("Final video length (seconds):", round(video.duration, 2))
        st.write("Images used:", img_count)

    st.success("Done. Narration will keep speaking until 60s.")
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4", mime="video/mp4")
