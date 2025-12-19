import textwrap
import requests
from pathlib import Path

import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# MoviePy v2 imports
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, CompositeAudioClip
from moviepy.audio.AudioClip import AudioArrayClip
import numpy as np

# Try MoviePy v2 effects (optional; code works without them)
try:
    from moviepy import vfx
except Exception:
    vfx = None

# ===============================
# CONFIG
# ===============================
WIDTH, HEIGHT = 1080, 1920
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
TMP_DIR = BASE / "tmp"

for d in (IMG_DIR, AUD_DIR, VID_DIR, TMP_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

SCENES = 6
SCENE_SECONDS = 10.0
TARGET_SECONDS = 60.0
IMAGES_PER_SCENE = 2  # fixed as you requested

# ===============================
# UI
# ===============================
st.title("YouTube Reel Generator — 6 Scenes × 10s (2 Images Each)")

topic = st.text_input("Topic", "Why does fire have no shadow?")

ENABLE_CAPTIONS = st.toggle("Burn captions on video", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

ENABLE_ZOOM = st.toggle("Enable subtle zoom", True)
ZOOM_STRENGTH = st.slider("Zoom strength", 1.01, 1.08, 1.03)

ENABLE_FADE = st.toggle("Enable fade transitions (safe)", True)
FADE_SEC = st.slider("Fade seconds", 0.1, 1.0, 0.35)

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

base_scenes = build_script(topic)

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
# CAPTIONS (PIL)
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
# PEXELS IMAGES (2 per scene)
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
# MOVIEPY SAFE EFFECTS
# ===============================
def apply_zoom(clip):
    if not ENABLE_ZOOM or ZOOM_STRENGTH <= 1.0:
        return clip
    if vfx is not None and hasattr(clip, "with_effects") and hasattr(vfx, "Resize"):
        try:
            return clip.with_effects([
                vfx.Resize(lambda t: 1 + (ZOOM_STRENGTH - 1) * (t / clip.duration))
            ])
        except Exception:
            return clip
    return clip

def apply_fade(clip):
    if not ENABLE_FADE or FADE_SEC <= 0:
        return clip
    if vfx is not None and hasattr(clip, "with_effects"):
        effs = []
        if hasattr(vfx, "FadeIn"):
            effs.append(vfx.FadeIn(FADE_SEC))
        if hasattr(vfx, "FadeOut"):
            effs.append(vfx.FadeOut(FADE_SEC))
        if effs:
            try:
                return clip.with_effects(effs)
            except Exception:
                return clip
    return clip

# ===============================
# AUDIO: make each scene ~10s of SPEECH (not silence)
# ===============================
FILLERS = [
    "Here’s an easy way to picture it.",
    "This is the key idea to remember.",
    "Think of the flame as a light source, not an object.",
    "That simple detail changes the shadow behavior.",
    "Most indoor flames won’t show a clear shadow.",
]

def tts_to_file(text, out_path):
    gTTS(text).save(str(out_path))
    return out_path

def clip_set_start(aclip, t):
    # moviepy v2 uses with_start; older uses set_start
    if hasattr(aclip, "with_start"):
        return aclip.with_start(t)
    return aclip.set_start(t)

def clip_set_duration(aclip, d):
    if hasattr(aclip, "with_duration"):
        return aclip.with_duration(d)
    return aclip.set_duration(d)

def make_scene_audio(scene_text, idx, target=SCENE_SECONDS):
    """
    Create an mp3 for this scene whose spoken duration is close to target.
    We keep adding short filler sentences until duration >= ~9.5s,
    then trim/pad to exactly 10s using a silence bed.
    """
    text = scene_text
    tries = 0

    while True:
        tmp_mp3 = TMP_DIR / f"scene_{idx}_try{tries}.mp3"
        tts_to_file(text, tmp_mp3)
        a = AudioFileClip(str(tmp_mp3))

        if DEBUG:
            st.write(f"Scene {idx} try {tries} duration:", round(a.duration, 2))

        # If close enough, stop expanding
        if a.duration >= target * 0.95 or tries >= 6:
            break

        # Add a filler sentence and retry
        text = text + " " + FILLERS[tries % len(FILLERS)]
        tries += 1

    # Build a 10s silence bed and overlay narration
    sr = 44100
    bed = AudioArrayClip(np.zeros((int(sr * target), 2), dtype=np.float32), fps=sr)

    # Trim narration to 10s if too long
    if a.duration > target:
        if hasattr(a, "subclip"):
            a = a.subclip(0, target)

    mixed = CompositeAudioClip([bed, a])
    mixed = clip_set_duration(mixed, target)

    return mixed

def make_full_audio(scene_texts):
    # 60s silence bed
    sr = 44100
    bed60 = AudioArrayClip(np.zeros((int(sr * TARGET_SECONDS), 2), dtype=np.float32), fps=sr)

    parts = [bed60]
    for i, s in enumerate(scene_texts):
        sa = make_scene_audio(s, i + 1, target=SCENE_SECONDS)
        sa = clip_set_start(sa, i * SCENE_SECONDS)
        parts.append(sa)

    full = CompositeAudioClip(parts)
    full = clip_set_duration(full, TARGET_SECONDS)
    return full

# ===============================
# BUILD VIDEO
# ===============================
if st.button("Generate Final MP4 Reel"):
    st.info("Generating 60s reel… (speech will fill each 10s scene)")

    # Audio (speech-filled per scene)
    audio = make_full_audio(base_scenes)

    # Visuals: 6 scenes × 2 images = 12 images, 5s each
    per_image_dur = SCENE_SECONDS / IMAGES_PER_SCENE  # 5 seconds

    clips = []
    used_images = 0

    for scene in base_scenes:
        imgs = fetch_images(scene)
        if not imgs:
            st.error("No images fetched. Check PEXELS_API_KEY or try a different topic.")
            st.stop()

        for img in imgs:
            c = ImageClip(str(img), duration=per_image_dur)
            c = apply_zoom(c)
            c = apply_fade(c)
            clips.append(c)
            used_images += 1

    # No overlap (keeps duration exactly 60s)
    video = concatenate_videoclips(clips, method="compose", padding=0)

    if hasattr(video, "with_audio"):
        video = video.with_audio(audio)
    else:
        video = video.set_audio(audio)

    out = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    if DEBUG:
        st.write("Final audio duration:", round(audio.duration, 2))
        st.write("Final video duration:", round(video.duration, 2))
        st.write("Images used:", used_images)

    st.success("Done (narration fills the full 60s and matches 6×10s scenes).")
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4", mime="video/mp4")
