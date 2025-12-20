import json, time, re, os
from io import BytesIO
from pathlib import Path
from typing import List
import zipfile

import streamlit as st
import requests
import numpy as np
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip,
)

from google import genai
from google.genai import types

# -------------------- CONFIG --------------------
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMAGE_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE
CROSSFADE = 0.6
FONT_SIZE = 72

BASE = Path(__file__).parent
IMG = BASE / "images"
AUD = BASE / "audio"
VID = BASE / "video"
CACHE = BASE / "cache"
for d in (IMG, AUD, VID, CACHE):
    d.mkdir(exist_ok=True)

# -------------------- KEYS --------------------
client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
PEXELS_KEY = st.secrets["PEXELS_API_KEY"]

# -------------------- HELPERS --------------------
def slug(t): return re.sub(r"[^a-z0-9]+", "_", t.lower())[:50]

def silence(d):
    return AudioClip(lambda t: np.zeros((1,)), duration=d, fps=44100)

def fit_audio(a, d):
    if a.duration > d:
        return a.subclip(0, d)
    if a.duration < d:
        return concatenate_audioclips([a, silence(d - a.duration)])
    return a

def font():
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", FONT_SIZE)
    except:
        return ImageFont.load_default()

FONT = font()

def subtitle_png(text, out):
    img = Image.new("RGBA", (W, H), (0,0,0,0))
    d = ImageDraw.Draw(img)
    box_h = 200
    d.rounded_rectangle(
        (80, H-box_h-80, W-80, H-80),
        40,
        fill=(0,0,0,200)
    )
    d.text((W//2, H-box_h//2-80), text, fill="white",
           font=FONT, anchor="mm", align="center")
    img.save(out)

def pexels(query):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_KEY},
        params={"query": query, "orientation": "portrait", "per_page": 10},
        timeout=20
    )
    return r.json().get("photos", [])

def get_image(url, out):
    r = requests.get(url, timeout=20)
    img = Image.open(BytesIO(r.content)).convert("RGB")
    img = ImageOps.fit(img, (W, H))
    img.save(out)

# -------------------- GEMINI --------------------
def generate_topics(existing, n):
    prompt = {
        "topics": [f"Generate {n} new curiosity science short video topics"]
    }
    r = client.models.generate_content(
        model="models/gemini-2.5-flash",
        contents=json.dumps(prompt),
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    topics = json.loads(r.text)["topics"]
    return [t for t in topics if t.lower() not in existing][:n]

def generate_script(topic, scenes):
    prompt = {
        "topic": topic,
        "scenes": scenes,
        "rules": "Each scene spoken in ~10 seconds",
    }
    r = client.models.generate_content(
        model="models/gemini-2.5-flash",
        contents=json.dumps(prompt),
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(r.text)["scenes"]

# -------------------- BUILD REEL --------------------
def build_reel(topic, scenes, idx, progress):
    clips, auds = [], []
    for i, sc in enumerate(scenes, 1):
        photos = pexels(sc["image_query"])
        imgs = []
        for j in range(2):
            out = IMG / f"{idx}_{i}_{j}.jpg"
            if j < len(photos):
                get_image(photos[j]["src"]["portrait"], out)
            else:
                Image.new("RGB",(W,H),(30,30,30)).save(out)
            imgs.append(out)

        c1 = ImageClip(str(imgs[0])).set_duration(IMAGE_SECONDS)
        c2 = ImageClip(str(imgs[1])).set_duration(IMAGE_SECONDS).crossfadein(CROSSFADE)
        scene = concatenate_videoclips([c1, c2], padding=-CROSSFADE)

        sub = IMG / f"sub_{idx}_{i}.png"
        subtitle_png(sc["subtitle"], sub)
        scene = CompositeVideoClip([scene, ImageClip(str(sub)).set_duration(SCENE_SECONDS)])

        mp3 = AUD / f"{idx}_{i}.mp3"
        gTTS(sc["subtitle"]).save(mp3)
        aud = fit_audio(AudioFileClip(str(mp3)), SCENE_SECONDS)

        clips.append(scene.set_audio(aud))
        auds.append(aud)
        progress(i / len(scenes))

    final = concatenate_videoclips(clips, padding=-CROSSFADE)
    final.set_audio(concatenate_audioclips(auds))

    out = VID / f"{slug(topic)}.mp4"
    final.write_videofile(str(out), fps=FPS, codec="libx264", audio_codec="aac")
    return out

# -------------------- UI --------------------
st.set_page_config(layout="wide")
st.title("🎬 Reel Factory (Stable)")

if "topics" not in st.session_state:
    st.session_state.topics = []

scenes = st.selectbox("Scenes per reel", [6,8])
count = st.slider("How many reels to generate", 1, 20, 1)

if st.button("Generate Topics"):
    st.session_state.topics = generate_topics(
        [t.lower() for t in st.session_state.topics], count
    )

if st.session_state.topics:
    topic = st.selectbox("Select topic", st.session_state.topics)

    if st.button("Build Reel(s)"):
        vids = []
        prog = st.progress(0.0)
        for i in range(count):
            script = generate_script(topic, scenes)
            vids.append(build_reel(topic, script, i, lambda p: prog.progress(p)))
        if len(vids) == 1:
            st.video(str(vids[0]))
            st.download_button("Download MP4", open(vids[0],"rb"), vids[0].name)
        else:
            zipf = VID / "batch.zip"
            with zipfile.ZipFile(zipf, "w") as z:
                for v in vids: z.write(v, v.name)
            st.download_button("Download ZIP", open(zipf,"rb"), "reels.zip")
