import json
import random
import re
import textwrap
import time
import zipfile
from pathlib import Path

import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps
from moviepy import AudioFileClip, CompositeVideoClip, ImageClip

# =============================
# SESSION STATE
# =============================
if "topics_text" not in st.session_state:
    st.session_state["topics_text"] = ""
if "auto_error" not in st.session_state:
    st.session_state["auto_error"] = ""

# =============================
# CONFIG
# =============================
WIDTH, HEIGHT = 1080, 1920
IMAGES_PER_SCENE = 2

CAPTION_FONT_SIZE = 64        # 🔥 FIXED: large readable captions
CAPTION_LINE_SPACING = 12
CAPTION_PADDING = 60

BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE_DIR = BASE / "cache"
HISTORY_FILE = CACHE_DIR / "topics.json"

for d in (IMG_DIR, AUD_DIR, VID_DIR, CACHE_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# =============================
# MOVIEPY COMPAT
# =============================
def clip_with_duration(c, d):
    return c.with_duration(d) if hasattr(c, "with_duration") else c.set_duration(d)

def clip_with_start(c, t):
    return c.with_start(t) if hasattr(c, "with_start") else c.set_start(t)

def clip_with_audio(c, a):
    return c.with_audio(a) if hasattr(c, "with_audio") else c.set_audio(a)

# =============================
# UTILS
# =============================
def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:60]

def fmt_time(s):
    s = int(max(0, s))
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if m else f"{s}s"

# =============================
# TOPIC HISTORY
# =============================
def load_history():
    if HISTORY_FILE.exists():
        return set(json.loads(HISTORY_FILE.read_text()))
    return set()

def save_history(h):
    HISTORY_FILE.write_text(json.dumps(sorted(h), indent=2))

TOPIC_HISTORY = load_history()

# =============================
# AUTO TOPICS
# =============================
TOPIC_POOL = [
    "Why do hiccups happen?",
    "Why do we yawn?",
    "Why does fire have no shadow?",
    "Why does ice float?",
    "Why do fingers wrinkle in water?",
    "Why do onions make you cry?",
    "Why do we get goosebumps?",
    "Why does metal feel colder than wood?",
    "Why do we dream?",
    "Why does sound travel faster in water?",
    "Why does sugar dissolve faster in hot water?",
    "Why does rubbing alcohol feel cold?",
    "Why does lightning strike tall objects?",
    "Why does the sky look blue?",
    "Why does food taste different on airplanes?",
]

def generate_new_topics(n=20):
    used = set(t.lower() for t in TOPIC_HISTORY)
    pool = TOPIC_POOL[:]
    random.shuffle(pool)
    out = []
    for t in pool:
        if t.lower() not in used:
            out.append(t)
            used.add(t.lower())
            if len(out) >= n:
                break
    return out

def cb_autogen():
    new = generate_new_topics(20)
    if not new:
        st.session_state["auto_error"] = "No new topics left."
        return
    for t in new:
        TOPIC_HISTORY.add(t.lower())
    save_history(TOPIC_HISTORY)
    st.session_state["topics_text"] = "\n".join(new)
    st.session_state["auto_error"] = ""

# =============================
# SCRIPT (TOPIC SAFE)
# =============================
def script_pool(topic):
    t = topic.lower()

    if "hiccup" in t:
        return [
            "Why do hiccups happen?",
            "Hiccups start when the diaphragm suddenly contracts.",
            "This pulls air in quickly.",
            "The vocal cords snap shut, making the hic sound.",
            "Eating fast or soda often triggers hiccups.",
            "They usually stop on their own.",
            "That’s the biology behind hiccups.",
        ]

    if "fire" in t and "shadow" in t:
        return [
            "Why does fire have no shadow?",
            "A shadow forms when light is blocked.",
            "Fire emits light instead of blocking it.",
            "Flames are glowing gases.",
            "This fills the shadow region.",
            "That’s why fire has no sharp shadow.",
        ]

    return [
        topic,
        "This happens due to a simple scientific mechanism.",
        "It depends on how energy moves.",
        "Once broken down, it becomes clear.",
        "Science explains what we observe.",
    ]

# =============================
# CAPTIONED IMAGE (FIXED SIZE)
# =============================
def prepare_image(url, caption, out_path):
    out_path.write_bytes(requests.get(url, timeout=20).content)
    img = Image.open(out_path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", CAPTION_FONT_SIZE)
    except:
        font = ImageFont.load_default()

    lines = textwrap.wrap(caption, width=28)
    line_h = CAPTION_FONT_SIZE + CAPTION_LINE_SPACING
    box_h = CAPTION_PADDING * 2 + line_h * len(lines)

    y1 = HEIGHT - box_h - 120
    draw.rectangle([(0, y1), (WIDTH, HEIGHT)], fill=(0, 0, 0))

    y = y1 + CAPTION_PADDING
    for line in lines:
        draw.text((60, y), line, fill="white", font=font)
        y += line_h

    img.save(out_path, quality=95)
    return out_path

# =============================
# VIDEO BUILDER
# =============================
def build_video(images, audio_path):
    audio = AudioFileClip(str(audio_path))
    dur = audio.duration
    per_img = dur / len(images)

    clips = []
    for i, img in enumerate(images):
        c = ImageClip(str(img))
        c = clip_with_duration(c, per_img)
        c = clip_with_start(c, i * per_img)
        clips.append(c)

    video = CompositeVideoClip(clips, size=(WIDTH, HEIGHT))
    video = clip_with_audio(video, audio)
    return video

# =============================
# BUILD ONE REEL
# =============================
def build_reel(topic, idx, cb=None):
    script = script_pool(topic)

    narration = " ".join(script)
    audio_path = AUD_DIR / f"voice_{idx}.mp3"
    gTTS(narration).save(audio_path)

    images = []
    for s in script:
        photos = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": s, "per_page": 10, "orientation": "portrait"},
        ).json()["photos"]

        for p in photos[:IMAGES_PER_SCENE]:
            url = p["src"]["portrait"]
            out = IMG_DIR / f"{idx}_{abs(hash(url))}.jpg"
            images.append(prepare_image(url, s, out))
        time.sleep(0.25)

    video = build_video(images, audio_path)
    out = VID_DIR / f"reel_{idx}_{slugify(topic)}.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")
    return out

# =============================
# UI
# =============================
st.title("YouTube Reel Generator (Captions Fixed)")

mode = st.radio("Mode", ["Single", "Batch (20)"], horizontal=True)

if mode == "Single":
    topic = st.text_input("Topic", "Why do hiccups happen?")
    if st.button("Generate Reel"):
        mp4 = build_reel(topic, 1)
        st.video(str(mp4))
        st.download_button("Download MP4", open(mp4, "rb"), mp4.name)

else:
    st.text_area("Topics (one per line)", key="topics_text", height=260)
    st.button("Auto-generate 20 topics", on_click=cb_autogen)

    if st.button("Generate 20 Reels"):
        topics = [t for t in st.session_state["topics_text"].splitlines() if t][:20]
        outputs = []

        for i, t in enumerate(topics, 1):
            outputs.append(build_reel(t, i))

        zip_path = VID_DIR / "batch.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in outputs:
                z.write(p, p.name)

        st.download_button("Download ZIP", open(zip_path, "rb"), "reels.zip")
        st.video(str(outputs[0]))
