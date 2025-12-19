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
# UI
# ===============================
st.title("YouTube Reel Generator – MP4 Builder (Free Images + Captions)")

topic = st.text_input("Topic", "Why does fire have no shadow?")
scenes = st.slider("Scenes (script sections)", 5, 10, 7)
target_seconds = st.slider("Target reel length (seconds)", 30, 90, 60)

images_per_scene = st.slider("Images per scene", 2, 6, 4)

transition_sec = st.slider("Transition (crossfade) seconds", 0.2, 1.0, 0.5)
enable_zoom = st.toggle("Enable subtle zoom (Ken Burns)", True)
zoom_strength = st.slider("Zoom strength", 1.01, 1.08, 1.04)

enable_captions = st.toggle("Burn captions on video", True)
caption_font_size = st.slider("Caption font size", 42, 84, 64)
caption_box_opacity = st.slider("Caption box opacity", 80, 220, 160)
show_debug = st.toggle("Show debug", False)

# ===============================
# SCRIPT (NO AI — stable)
# ===============================
def fallback_script(topic, n):
    base = [
        f"{topic} — quick answer.",
        "A shadow forms when a strong, single light source is blocked.",
        "Fire is not a solid object; it’s glowing hot gas that emits light.",
        "Because flames emit light, they fill in their own shadow.",
        "Flames are also partly transparent, so they don’t block all light.",
        "You only see a shadow if a much brighter light is behind the flame.",
        "That’s why candle flames rarely cast clear shadows indoors.",
        "Try it: shine a flashlight behind a lighter and check the wall."
    ]
    return base[:n]

script = fallback_script(topic, scenes)

# ===============================
# FONT LOADER (Cloud-safe)
# ===============================
def load_font(size: int):
    # Streamlit Cloud often has DejaVu fonts available
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

FONT = load_font(caption_font_size)

# ===============================
# CAPTION DRAW (PIL)
# ===============================
def burn_caption(img: Image.Image, caption: str) -> Image.Image:
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Wrap text for 1080px wide
    max_chars = 28
    lines = textwrap.wrap(caption, width=max_chars)
    lines = lines[:3]  # keep it short

    # Calculate text block size
    line_height = int(caption_font_size * 1.2)
    padding = 60
    box_h = padding + line_height * len(lines) + 30
    box_y1 = HEIGHT - box_h - 120
    box_y2 = HEIGHT - 120

    # Semi-transparent box
    draw.rectangle(
        [(60, box_y1), (WIDTH - 60, box_y2)],
        fill=(0, 0, 0, int(caption_box_opacity)),
        outline=None,
    )

    # Text
    y = box_y1 + 35
    for line in lines:
        draw.text((90, y), line, font=FONT, fill=(255, 255, 255, 255))
        y += line_height

    combined = Image.alpha_composite(img, overlay).convert("RGB")
    return combined

# ===============================
# PEXELS IMAGE FETCH
# ===============================
def fetch_images(query, count):
    headers = {"Authorization": PEXELS_API_KEY}
    url = "https://api.pexels.com/v1/search"
    params = {
        "query": query,
        "per_page": min(max(count, 5), 20),
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

        img_path = IMG_DIR / f"img_{abs(hash(query))}_{i}.jpg"
        img_data = requests.get(img_url, timeout=20).content
        with open(img_path, "wb") as f:
            f.write(img_data)

        img = Image.open(img_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        # Burn captions per image (if enabled)
        if enable_captions:
            img = burn_caption(img, query)

        img.save(img_path, quality=95)
        paths.append(img_path)

    return paths

# ===============================
# SAFE ZOOM
# ===============================
def apply_zoom(clip, zoom):
    if not enable_zoom:
        return clip
    return clip.resize(lambda t: 1 + (zoom - 1) * (t / clip.duration))

# ===============================
# BUILD VIDEO
# ===============================
if st.button("Generate Final MP4 Reel"):
    st.info("Generating reel…")

    # ---- Voiceover
    narration = " ".join(script)
    audio_path = AUD_DIR / "voice.mp3"
    gTTS(narration).save(audio_path)
    audio = AudioFileClip(str(audio_path))

    # ---- Images (many per scene)
    all_images = []
    for s in script:
        imgs = fetch_images(s, images_per_scene)
        all_images.extend(imgs)
        if show_debug:
            st.write(f"Fetched {len(imgs)} images for:", s)

    if not all_images:
        st.error("No images fetched. Check your Pexels key.")
        st.stop()

    total_images = len(all_images)
    per_img_dur = max(0.6, audio.duration / total_images)

    # ---- Make clips
    clips = []
    for img in all_images:
        c = ImageClip(str(img)).set_duration(per_img_dur)
        c = apply_zoom(c, zoom_strength)
        clips.append(c)

    # ---- Concatenate with crossfade
    final = concatenate_videoclips(
        clips,
        method="compose",
        padding=-transition_sec
    ).set_audio(audio)

    out = VID_DIR / "final_reel.mp4"
    final.write_videofile(
        str(out),
        fps=30,
        codec="libx264",
        audio_codec="aac"
    )

    st.success(
        f"Final MP4 ready • Images: {total_images} • "
        f"Audio: {audio.duration:.1f}s • Per image: {per_img_dur:.2f}s"
    )
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4")
