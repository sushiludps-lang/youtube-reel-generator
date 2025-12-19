import os
import re
import time
import textwrap
from pathlib import Path

import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

# ✅ MoviePy v1.0.3 ONLY (Cloud-safe)
from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    concatenate_videoclips,
    vfx,
)

# Ensure ffmpeg exists on Streamlit Cloud
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# =============================
# CONFIG
# =============================
WIDTH, HEIGHT = 1080, 1920
FPS = 30
IMAGES_PER_SCENE = 2
FADE_SECONDS = 0.4

BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"

for d in (IMG_DIR, AUD_DIR, VID_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# =============================
# HELPERS
# =============================
def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")

def load_font(size, bold=False):
    try:
        return ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            size,
        )
    except:
        return ImageFont.load_default()

FONT_CAPTION = load_font(70, bold=True)
FONT_TITLE = load_font(52, bold=True)

def draw_caption(img, caption, title):
    img = img.convert("RGB")
    img = img.resize((WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)

    # Dark vignette
    overlay = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(overlay)
    d.ellipse([-300, -300, WIDTH+300, HEIGHT+300], fill=255)
    overlay = overlay.filter(ImageFilter.GaussianBlur(120))
    img = Image.composite(img, Image.new("RGB", img.size, (0,0,0)), overlay)

    # Title
    if title:
        tbox = draw.textbbox((0,0), title, font=FONT_TITLE)
        tx = (WIDTH - (tbox[2]-tbox[0])) // 2
        draw.rounded_rectangle((tx-20,40,tx+tbox[2]+20,40+tbox[3]+20),30,fill=(15,15,15))
        draw.text((tx,50), title, font=FONT_TITLE, fill="white")

    # Caption box
    lines = textwrap.wrap(caption, width=26)[:3]
    h = len(lines) * 80 + 60
    y1 = HEIGHT - h - 120
    draw.rounded_rectangle((80,y1,WIDTH-80,HEIGHT-120),40,fill=(15,15,15))
    y = y1 + 30
    for line in lines:
        draw.text((120,y), line, font=FONT_CAPTION, fill="white")
        y += 80

    return img

def placeholder_image(text, title, out):
    img = Image.new("RGB",(WIDTH,HEIGHT),(20,20,25))
    img = draw_caption(img, text, title)
    img.save(out)
    return out

def pexels_images(query):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": 10, "orientation": "portrait"},
        timeout=20
    )
    r.raise_for_status()
    return r.json().get("photos", [])

# =============================
# SCRIPT
# =============================
def script_for(topic):
    if "hiccup" in topic.lower():
        return [
            "Why do hiccups happen?",
            "Your diaphragm suddenly contracts.",
            "Air rushes in quickly.",
            "Your vocal cords snap shut.",
            "That sharp sound is the hiccup.",
            "They usually stop on their own.",
        ]
    return [
        topic,
        "Here’s the science behind it.",
        "It happens due to physics and biology.",
        "Once you understand it, it makes sense.",
        "Follow for more science facts.",
    ]

# =============================
# VIDEO BUILDER
# =============================
def build_video(images, audio_path):
    audio = AudioFileClip(str(audio_path))
    dur = audio.duration
    per_img = dur / len(images)

    clips = []
    for img in images:
        c = ImageClip(str(img)).set_duration(per_img)
        c = c.fx(vfx.fadein, FADE_SECONDS).fx(vfx.fadeout, FADE_SECONDS)
        clips.append(c)

    video = concatenate_videoclips(clips, method="compose")
    video = video.set_audio(audio)
    return video

def build_reel(topic):
    script = script_for(topic)
    narration = " ".join(script)

    audio_path = AUD_DIR / "voice.mp3"
    gTTS(narration).save(audio_path)

    images = []
    for i, line in enumerate(script):
        try:
            photos = pexels_images(line)
            if photos:
                url = photos[0]["src"]["portrait"]
                img_path = IMG_DIR / f"img_{i}.jpg"
                img_path.write_bytes(requests.get(url).content)
                img = Image.open(img_path)
                img = draw_caption(img, line, topic)
                img.save(img_path)
                images.append(img_path)
                continue
        except:
            pass

        img_path = IMG_DIR / f"ph_{i}.jpg"
        images.append(placeholder_image(line, topic, img_path))

    video = build_video(images, audio_path)
    out = VID_DIR / f"{slugify(topic)}.mp4"

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

# =============================
# STREAMLIT UI
# =============================
st.title("🎬 YouTube Reel Generator (STABLE)")

topic = st.text_input("Topic", "Why do hiccups happen?")

if st.button("Generate Reel"):
    with st.spinner("Creating reel..."):
        mp4 = build_reel(topic)
    st.success("Done!")
    st.video(str(mp4))
    st.download_button("Download MP4", open(mp4,"rb"), mp4.name)
