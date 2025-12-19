import textwrap
import requests
from pathlib import Path

import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# MoviePy v2 compatible imports
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
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

# ===============================
# UI
# ===============================
st.title("YouTube Reel Generator — 6 Scenes × 10s (2 Images Each)")

topic = st.text_input("Topic", "Why does fire have no shadow?")

SCENES = 6
SCENE_SECONDS = 10.0
TARGET_SECONDS = 60.0
IMAGES_PER_SCENE = 2

TRANSITION_SEC = st.slider("Transition (crossfade) seconds", 0.2, 1.0, 0.5)
ENABLE_ZOOM = st.toggle("Enable subtle zoom (Ken Burns)", True)
ZOOM_STRENGTH = st.slider("Zoom strength", 1.01, 1.08, 1.04)

ENABLE_CAPTIONS = st.toggle("Burn captions on video", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

DEBUG = st.toggle("Show debug", False)

# ===============================
# SCRIPT (fixed 6 scenes)
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

def burn_caption(img, caption, font):
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    lines = textwrap.wrap(caption, width=28)[:3]
    line_h = int(CAPTION_FONT_SIZE * 1.2)

    box_h = 60 + line_h * len(lines) + 30
    y1 = HEIGHT - box_h - 120
    y2 = HEIGHT - 120

    draw.rectangle([(60, y1), (WIDTH - 60, y2)],
                   fill=(0, 0, 0, int(CAPTION_BOX_OPACITY)))

    y = y1 + 35
    for line in lines:
        draw.text((90, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_h

    return Image.alpha_composite(img, overlay).convert("RGB")

# ===============================
# PEXELS FETCH (exactly 2 images/scene)
# ===============================
def fetch_images(scene_text, font):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {
        "query": scene_text,
        "per_page": 30,
        "orientation": "portrait",
        "size": "large",
    }
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    photos = r.json().get("photos", [])

    paths = []
    for i, p in enumerate(photos[:IMAGES_PER_SCENE]):
        url = p["src"].get("portrait") or p["src"].get("large")
        if not url:
            continue

        img_path = IMG_DIR / f"img_{abs(hash((scene_text, i)))}.jpg"
        data = requests.get(url, timeout=20).content
        img_path.write_bytes(data)

        img = Image.open(img_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        if ENABLE_CAPTIONS:
            img = burn_caption(img, scene_text, font)

        img.save(img_path, quality=95)
        paths.append(img_path)

    if not paths:
        return []

    while len(paths) < IMAGES_PER_SCENE:
        paths.append(paths[-1])

    return paths[:IMAGES_PER_SCENE]

# ===============================
# ZOOM
# ===============================
def apply_zoom(clip):
    if not ENABLE_ZOOM:
        return clip
    return clip.resize(lambda t: 1 + (ZOOM_STRENGTH - 1) * (t / clip.duration))

# ===============================
# AUDIO FIT: pad or trim to 60s (NO speedx needed)
# ===============================
def fit_audio_to_duration(audio: AudioFileClip, target_seconds: float) -> AudioFileClip:
    if audio.duration >= target_seconds:
        return audio.subclip(0, target_seconds)

    # pad silence to reach target
    sr = 44100
    missing = target_seconds - audio.duration
    n = int(sr * missing)
    silence = np.zeros((n, 2), dtype=np.float32)  # stereo
    silence_clip = AudioArrayClip(silence, fps=sr)
    return concatenate_audios([audio, silence_clip]).subclip(0, target_seconds)

def concatenate_audios(clips):
    # moviepy v2: easiest is to convert to AudioClip list with concatenation via VideoClip trick
    # We'll just use AudioArrayClip join:
    arrays = []
    fps = 44100
    for c in clips:
        if hasattr(c, "fps") and c.fps:
            fps = c.fps
        arr = c.to_soundarray(fps=fps)
        arrays.append(arr)
    joined = np.vstack(arrays)
    return AudioArrayClip(joined, fps=fps)

# ===============================
# BUILD VIDEO
# ===============================
if st.button("Generate Final MP4 Reel"):
    st.info("Generating 60s reel…")

    # Voiceover
    narration = " ".join(script)
    audio_path = AUD_DIR / "voice.mp3"
    gTTS(narration).save(str(audio_path))
    audio = AudioFileClip(str(audio_path))
    audio = fit_audio_to_duration(audio, TARGET_SECONDS)

    # Visuals: 6 scenes × 2 images = 12 images, 5s each
    per_image_dur = SCENE_SECONDS / IMAGES_PER_SCENE  # 5 seconds

    font = load_font(CAPTION_FONT_SIZE)
    clips = []
    used_images = 0

    for scene in script:
        imgs = fetch_images(scene, font)
        if not imgs:
            st.error("No images fetched. Check PEXELS_API_KEY or try a different topic.")
            st.stop()

        for img in imgs:
            c = ImageClip(str(img), duration=per_image_dur)
            c = apply_zoom(c)
            clips.append(c)
            used_images += 1

    video = concatenate_videoclips(clips, method="compose", padding=-TRANSITION_SEC)
    video = video.with_audio(audio)

    out = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    if DEBUG:
        st.write("Audio duration:", round(audio.duration, 2))
        st.write("Video duration:", round(video.duration, 2))
        st.write("Images used:", used_images)

    st.success("Done.")
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4", mime="video/mp4")
