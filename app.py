import textwrap
import requests
from pathlib import Path

import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips

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
# UI (fixed structure you asked)
# ===============================
st.title("YouTube Reel Generator – 6 Scenes × 10s (2 images per scene)")

topic = st.text_input("Topic", "Why does fire have no shadow?")

SCENES = 6
SCENE_SECONDS = 10.0
IMAGES_PER_SCENE = 2
TRANSITION_SEC = st.slider("Transition (crossfade) seconds", 0.2, 1.0, 0.5)
ENABLE_ZOOM = st.toggle("Enable subtle zoom (Ken Burns)", True)
ZOOM_STRENGTH = st.slider("Zoom strength", 1.01, 1.08, 1.04)

ENABLE_CAPTIONS = st.toggle("Burn captions on video", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

show_debug = st.toggle("Show debug", False)

# ===============================
# SCRIPT (no AI; stable)
# ===============================
def build_6_scene_script(topic: str):
    # 6 lines total = 6 scenes
    return [
        f"{topic} — quick answer.",
        "A shadow forms when one strong light is blocked.",
        "Fire is glowing hot gas that emits its own light.",
        "Because it emits light, it fills in its own shadow.",
        "Flames are partly transparent, so they don’t block all light.",
        "You only see a shadow if a much brighter light is behind the flame."
    ]

script = build_6_scene_script(topic)

# ===============================
# FONT LOADER (Cloud-safe)
# ===============================
def load_font(size: int):
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
# CAPTION DRAW (PIL)
# ===============================
def burn_caption(img: Image.Image, caption: str) -> Image.Image:
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    lines = textwrap.wrap(caption, width=28)[:3]
    line_height = int(CAPTION_FONT_SIZE * 1.2)
    padding = 60
    box_h = padding + line_height * len(lines) + 30
    box_y1 = HEIGHT - box_h - 120
    box_y2 = HEIGHT - 120

    draw.rectangle(
        [(60, box_y1), (WIDTH - 60, box_y2)],
        fill=(0, 0, 0, int(CAPTION_BOX_OPACITY)),
    )

    y = box_y1 + 35
    for line in lines:
        draw.text((90, y), line, font=FONT, fill=(255, 255, 255, 255))
        y += line_height

    return Image.alpha_composite(img, overlay).convert("RGB")

# ===============================
# PEXELS IMAGE FETCH (exactly 2 per scene)
# ===============================
def fetch_pexels_images(query: str, count: int):
    headers = {"Authorization": PEXELS_API_KEY}
    url = "https://api.pexels.com/v1/search"
    params = {
        "query": query,
        "per_page": 20,
        "orientation": "portrait",
        "size": "large",
    }
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    photos = r.json().get("photos", [])

    paths = []
    for i, p in enumerate(photos[:count]):
        img_url = p["src"].get("portrait") or p["src"].get("large")
        if not img_url:
            continue

        img_path = IMG_DIR / f"img_{abs(hash((query, i)))}.jpg"
        img_data = requests.get(img_url, timeout=20).content
        with open(img_path, "wb") as f:
            f.write(img_data)

        img = Image.open(img_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        if ENABLE_CAPTIONS:
            img = burn_caption(img, query)

        img.save(img_path, quality=95)
        paths.append(img_path)

    # If Pexels returned < count, duplicate last so timing is stable
    if not paths:
        return []
    while len(paths) < count:
        paths.append(paths[-1])

    return paths[:count]

# ===============================
# SAFE ZOOM
# ===============================
def apply_zoom(clip, zoom):
    if not ENABLE_ZOOM:
        return clip
    return clip.resize(lambda t: 1 + (zoom - 1) * (t / clip.duration))

# ===============================
# BUILD VIDEO (60 seconds exactly in visuals)
# ===============================
if st.button("Generate Final MP4 Reel"):
    st.info("Generating reel… (6 scenes × 10s, 2 images each)")

    # ---- Voiceover (will be whatever length TTS produces)
    narration = " ".join(script)
    audio_path = AUD_DIR / "voice.mp3"
    gTTS(narration).save(audio_path)
    audio = AudioFileClip(str(audio_path))

    # ---- Build visual timeline: 6 scenes × 10 seconds = 60 sec
    # Each scene has 2 images → 5 seconds each
    per_image_duration = SCENE_SECONDS / IMAGES_PER_SCENE  # 10/2 = 5s

    clips = []
    all_images = []

    for scene_text in script:
        imgs = fetch_pexels_images(scene_text, IMAGES_PER_SCENE)
        if not imgs:
            st.error("No images fetched. Check PEXELS_API_KEY or try a different topic.")
            st.stop()

        all_images.extend(imgs)

        for img_path in imgs:
            c = ImageClip(str(img_path)).set_duration(per_image_duration)
            c = apply_zoom(c, ZOOM_STRENGTH)
            clips.append(c)

    final_video = concatenate_videoclips(
        clips,
        method="compose",
        padding=-TRANSITION_SEC
    )

    # ---- AUDIO HANDLING
    # Option A (default): keep your narration audio; video duration stays 60s visuals.
    # If audio is longer than 60s, it will be cut.
    # If audio is shorter, you’ll have silence at the end.
    final_video = final_video.set_audio(audio.subclip(0, min(audio.duration, final_video.duration)))

    out = VID_DIR / "final_reel.mp4"
    final_video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    if show_debug:
        st.write("Audio duration:", round(audio.duration, 2))
        st.write("Video duration:", round(final_video.duration, 2))
        st.write("Images used:", len(all_images))

    st.success("Final MP4 ready (visual timeline = 60 seconds).")
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4")
