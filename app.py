import json
import random
import re
import textwrap
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps
from moviepy import ImageClip, AudioFileClip, CompositeVideoClip

# =============================
# SESSION STATE (MUST BE FIRST)
# =============================
if "topics_text" not in st.session_state:
    st.session_state["topics_text"] = ""

# =============================
# CONFIG
# =============================
WIDTH, HEIGHT = 1080, 1920
IMAGES_PER_SCENE = 2

BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE_DIR = BASE / "cache"

for d in [IMG_DIR, AUD_DIR, VID_DIR, CACHE_DIR]:
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# =============================
# UTILS
# =============================
def fmt_time(s):
    s = int(max(0, s))
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if m else f"{s}s"

def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:60]

# =============================
# TOPIC MEMORY (NO DUPLICATES)
# =============================
HISTORY_FILE = CACHE_DIR / "topics.json"

def load_history():
    if HISTORY_FILE.exists():
        return set(json.loads(HISTORY_FILE.read_text()))
    return set()

def save_history(hist):
    HISTORY_FILE.write_text(json.dumps(sorted(hist)))

TOPIC_HISTORY = load_history()

# =============================
# AUTO TOPIC GENERATOR
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
    random.shuffle(TOPIC_POOL)
    new = []
    for t in TOPIC_POOL:
        if t.lower() not in TOPIC_HISTORY:
            new.append(t)
            if len(new) == n:
                break
    return new

def cb_autogen():
    new = generate_new_topics(20)
    for t in new:
        TOPIC_HISTORY.add(t.lower())
    save_history(TOPIC_HISTORY)
    st.session_state["topics_text"] = "\n".join(new)

# =============================
# SCRIPT GENERATOR (TOPIC SAFE)
# =============================
def script_pool(topic):
    t = topic.lower()

    if "hiccup" in t:
        return [
            "Why do hiccups happen?",
            "Hiccups occur when the diaphragm suddenly contracts.",
            "This causes a quick intake of air.",
            "The vocal cords snap shut, creating the hic sound.",
            "Eating too fast or carbonated drinks can trigger hiccups.",
            "They usually stop on their own within minutes.",
            "That’s the biology behind hiccups.",
        ]

    if "fire" in t and "shadow" in t:
        return [
            "Why does fire have no shadow?",
            "A shadow forms when light is blocked.",
            "Fire emits its own light instead of blocking it.",
            "Flames are made of glowing gases.",
            "This fills in the dark area where a shadow would form.",
            "That’s why fire usually has no sharp shadow.",
        ]

    return [
        topic,
        "This happens due to a simple scientific mechanism.",
        "It involves how energy or signals move.",
        "Small changes create surprising effects.",
        "Science helps explain what’s really happening.",
    ]

# =============================
# PEXELS IMAGES
# =============================
def pexels_images(query):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 15, "orientation": "portrait"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json()["photos"]

def prepare_image(url, caption, idx):
    path = IMG_DIR / f"{idx}_{abs(hash(url))}.jpg"
    path.write_bytes(requests.get(url).content)

    img = Image.open(path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    lines = textwrap.wrap(caption, 30)
    y = HEIGHT - 220

    draw.rectangle([(0, y - 20), (WIDTH, HEIGHT)], fill=(0, 0, 0))
    for line in lines[:3]:
        draw.text((40, y), line, fill="white", font=font)
        y += 35

    img.save(path)
    return path

# =============================
# VIDEO BUILDER (NO STUTTER)
# =============================
def build_video(images, audio_path):
    audio = AudioFileClip(str(audio_path))
    dur = audio.duration
    per_img = dur / len(images)

    clips = []
    for i, img in enumerate(images):
        c = ImageClip(str(img)).set_duration(per_img)
        c = c.set_start(i * per_img)
        clips.append(c)

    video = CompositeVideoClip(clips, size=(WIDTH, HEIGHT))
    video = video.set_audio(audio)
    return video

# =============================
# BUILD ONE REEL
# =============================
def build_reel(topic, idx, progress):
    start = time.time()
    script = script_pool(topic)

    progress(0.1, "Generating narration")
    narration = " ".join(script)
    audio_path = AUD_DIR / f"voice_{idx}.mp3"
    gTTS(narration).save(audio_path)

    progress(0.3, "Fetching images")
    images = []
    for s in script:
        photos = pexels_images(s)
        for p in photos[:IMAGES_PER_SCENE]:
            images.append(prepare_image(p["src"]["portrait"], s, idx))
        time.sleep(0.3)

    progress(0.7, "Rendering video")
    video = build_video(images, audio_path)

    out = VID_DIR / f"reel_{idx}_{slugify(topic)}.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    progress(1.0, "Done")
    return out, time.time() - start

# =============================
# UI
# =============================
st.title("🎬 YouTube Reel Generator (20/day, FREE)")

mode = st.radio("Mode", ["Single", "Batch (20)"], horizontal=True)

if mode == "Single":
    topic = st.text_input("Topic", "Why do hiccups happen?")
    if st.button("Generate Reel"):
        bar = st.progress(0)
        status = st.empty()

        def cb(p, m):
            bar.progress(int(p * 100))
            status.write(m)

        mp4, _ = build_reel(topic, 1, cb)
        st.video(str(mp4))

else:
    st.text_area("Topics (one per line)", key="topics_text", height=250)
    st.button("Auto-generate 20 topics", on_click=cb_autogen)

    if st.button("Generate 20 Reels"):
        topics = [t for t in st.session_state["topics_text"].splitlines() if t][:20]

        overall = st.progress(0)
        eta_box = st.empty()

        times = []
        outputs = []

        for i, t in enumerate(topics, 1):
            bar = st.progress(0)
            status = st.empty()

            def cb(p, m, i=i, t=t):
                bar.progress(int(p * 100))
                status.write(f"Reel {i}/20 — {m}")

            out, dt = build_reel(t, i, cb)
            outputs.append(out)
            times.append(dt)

            avg = sum(times) / len(times)
            remaining = avg * (len(topics) - i)
            overall.progress(int(i / len(topics) * 100))
            eta_box.write(f"Batch ETA remaining: {fmt_time(remaining)}")

        zip_path = VID_DIR / "batch.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in outputs:
                z.write(p, p.name)

        st.download_button("Download all (ZIP)", open(zip_path, "rb"), "reels.zip")
