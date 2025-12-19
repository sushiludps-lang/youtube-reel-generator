import textwrap
import requests
from pathlib import Path

import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# MoviePy v2 imports
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

# ===============================
# CONFIG / FOLDERS
# ===============================
WIDTH, HEIGHT = 1080, 1920
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
for d in (IMG_DIR, AUD_DIR, VID_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# Always 2 images per scene (your requirement)
IMAGES_PER_SCENE = 2

# ===============================
# UI
# ===============================
st.title("YouTube Reel Generator — Duration Driven by Narration (2 images/scene)")

topic = st.text_input("Topic", "Why does fire have no shadow?")

target_scene_seconds = st.slider(
    "Target seconds per scene (auto adjusts #scenes)",
    4, 12, 7
)

ENABLE_CAPTIONS = st.toggle("Burn captions on images", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

DEBUG = st.toggle("Show debug", False)

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
# CAPTION BURN (PIL)
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
# PEXELS FETCH (exactly 2 images per scene)
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
# SCRIPT POOL (free, no LLM)
# ===============================
def script_pool(topic: str):
    # A pool of short scene lines; we will take as many as needed.
    return [
        f"{topic} — quick answer.",
        "A shadow needs a strong background light and something that blocks it.",
        "Flames are not solid; they’re hot gases plus glowing particles.",
        "Fire emits light in many directions, so it fills in the dark area.",
        "Flames are also partly transparent, so they don’t block all light.",
        "That’s why a candle usually doesn’t cast a crisp shadow indoors.",
        "But if you put a brighter light behind the flame, you can see a shadow.",
        "Try it: flashlight behind a lighter, then look at the wall.",
        "The flame’s brightness and transparency decide how visible the shadow is.",
        "So the ‘no shadow’ idea is really: no clear shadow in normal lighting."
    ]

# ===============================
# AUTO-ADJUST SCENES BASED ON TTS DURATION (no repetition)
# ===============================
def build_script_to_match_duration(topic: str, target_scene_sec: float):
    pool = script_pool(topic)

    # Start with a reasonable guess
    script = pool[:6]

    # Iterate to stabilize: script length -> TTS duration -> needed scenes -> adjust script length
    for _ in range(3):
        narration = " ".join(script)
        tmp_mp3 = AUD_DIR / "tmp_voice.mp3"
        gTTS(narration).save(str(tmp_mp3))
        audio = AudioFileClip(str(tmp_mp3))
        dur = max(1.0, audio.duration)

        scenes_needed = max(1, int(round(dur / target_scene_sec)))
        scenes_needed = min(scenes_needed, len(pool))  # don’t exceed pool size

        new_script = pool[:scenes_needed]

        if len(new_script) == len(script):
            return new_script, tmp_mp3, dur

        script = new_script

    # final
    narration = " ".join(script)
    tmp_mp3 = AUD_DIR / "tmp_voice.mp3"
    gTTS(narration).save(str(tmp_mp3))
    audio = AudioFileClip(str(tmp_mp3))
    dur = max(1.0, audio.duration)
    return script, tmp_mp3, dur

# ===============================
# BUILD VIDEO (duration == audio duration)
# ===============================
if st.button("Generate Final MP4 Reel"):
    script, voice_path, audio_duration = build_script_to_match_duration(topic, target_scene_seconds)

    scenes = len(script)
    scene_duration = audio_duration / scenes
    per_image_duration = scene_duration / IMAGES_PER_SCENE  # always 2 images

    if DEBUG:
        st.write("Audio duration (s):", round(audio_duration, 2))
        st.write("Scenes:", scenes)
        st.write("Scene duration (s):", round(scene_duration, 2))
        st.write("Per-image duration (s):", round(per_image_duration, 2))

    # Load final audio (use the same mp3 we generated)
    audio = AudioFileClip(str(voice_path))

    clips = []
    used = 0

    for scene_text in script:
        imgs = fetch_images(scene_text)
        if not imgs:
            st.error("No images fetched from Pexels. Try a different topic or verify PEXELS_API_KEY.")
            st.stop()

        for img in imgs:
            clips.append(ImageClip(str(img), duration=per_image_duration))
            used += 1

    video = concatenate_videoclips(clips, method="compose", padding=0)

    # Attach audio; trim audio to video length (or vice versa) safely
    # (They should match closely; this avoids tiny drift.)
    final_dur = video.duration
    if hasattr(audio, "subclip") and audio.duration > final_dur:
        audio = audio.subclip(0, final_dur)

    if hasattr(video, "with_audio"):
        video = video.with_audio(audio)
    else:
        video = video.set_audio(audio)

    out = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    st.success(f"Done. Length: {video.duration:.1f}s • Scenes: {scenes} • Images: {used}")
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4", mime="video/mp4")
