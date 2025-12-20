import json, time, re, os, zipfile
from pathlib import Path
import requests
import numpy as np
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# FFmpeg fix
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

from moviepy.editor import (
    ImageClip, AudioFileClip, CompositeVideoClip,
    concatenate_videoclips, concatenate_audioclips, AudioClip
)

from google import genai
from google.genai import types

# ================= CONFIG =================
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE
CROSSFADE = 0.7
AUDIO_FPS = 44100

CAPTION_SCALE = 2.5  # 🔥 BIGGER TEXT HERE

BASE = Path(__file__).parent
IMG = BASE / "images"
AUD = BASE / "audio"
VID = BASE / "video"
for d in (IMG, AUD, VID):
    d.mkdir(exist_ok=True)

# ================= SECRETS =================
client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]
TEXT_MODEL = "models/gemini-2.5-flash"

# ================= HELPERS =================
def slug(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower())[:60] or "reel"

def pexels(q):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": q, "orientation": "portrait", "per_page": 15},
        timeout=25,
    )
    return r.json().get("photos", [])

def download_img(url, out):
    out.write_bytes(requests.get(url, timeout=25).content)
    img = Image.open(out).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = ImageOps.fit(img, (W, H))
    img.save(out, quality=92)

def placeholder(out, text):
    img = Image.new("RGB", (W, H), (25, 25, 30))
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default()
    d.text((60, 80), "PLACEHOLDER", fill="white", font=f)
    d.text((60, 150), text[:200], fill="white", font=f)
    img.save(out, quality=92)

# ================= HUGE SUBTITLES =================
def subtitle_png(text, out):
    scale = CAPTION_SCALE
    bigW, bigH = int(W * scale), int(H * scale)

    img = Image.new("RGBA", (bigW, bigH), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    words = text.split()
    lines, cur = [], []
    for w in words:
        if len(" ".join(cur + [w])) <= 24:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    lines = lines[:2]

    boxes = [d.textbbox((0, 0), l, font=font) for l in lines]
    widths = [b[2] - b[0] for b in boxes]
    heights = [b[3] - b[1] for b in boxes]

    text_w = max(widths)
    text_h = sum(heights) + 40
    pad_x, pad_y = 120, 80

    box_w = text_w + 2 * pad_x
    box_h = text_h + 2 * pad_y

    x1 = (bigW - box_w) // 2
    y2 = bigH - int(360 * scale)
    y1 = y2 - box_h

    d.rounded_rectangle((x1, y1, x1 + box_w, y2), 60, fill=(0, 0, 0, 200))

    y = y1 + pad_y
    for i, l in enumerate(lines):
        lx = (bigW - widths[i]) // 2
        d.text((lx, y), l, fill="white", font=font)
        y += heights[i] + 40

    img = img.resize((W, H), Image.LANCZOS)
    img.save(out)

def silence(d):
    return AudioClip(lambda t: np.zeros((1,), dtype=np.float32), duration=d, fps=AUDIO_FPS)

def fit_audio(a, d):
    if a.duration > d:
        return a.subclip(0, d)
    if a.duration < d:
        return concatenate_audioclips([a, silence(d - a.duration)])
    return a

# ================= GEMINI =================
def gen_topics(n):
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=f'Return JSON only: {{"topics":[...]}}. Generate {n} science curiosity questions.',
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(r.text)["topics"]

def gen_script(topic, scenes):
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=f'''
Return JSON only.
Exactly {scenes} scenes.
Each scene ~10 seconds.

{{"scenes":[{{"subtitle":"...","query":"..."}}]}}

Topic: {topic}
''',
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(r.text)["scenes"]

# ================= BUILD =================
def build_reel(topic, scenes, idx):
    auds, vids = [], []

    for i, s in enumerate(scenes, 1):
        photos = pexels(s["query"])
        pair = []

        for j in range(2):
            out = IMG / f"{idx}_{i}_{j}.jpg"
            try:
                download_img(photos[j]["src"]["portrait"], out)
            except Exception:
                placeholder(out, topic)
            pair.append(out)

        mp3 = AUD / f"{idx}_{i}.mp3"
        gTTS(s["subtitle"]).save(str(mp3))

        # ✅ FIXED LINE (Path → str)
        a = fit_audio(AudioFileClip(str(mp3)), SCENE_SECONDS)
        auds.append(a)

        c1 = ImageClip(str(pair[0])).set_duration(IMG_SECONDS)
        c2 = ImageClip(str(pair[1])).set_duration(IMG_SECONDS).crossfadein(CROSSFADE)
        base = concatenate_videoclips([c1, c2], padding=-CROSSFADE)

        sub = IMG / f"{idx}_{i}_sub.png"
        subtitle_png(s["subtitle"], sub)
        subclip = ImageClip(str(sub)).set_duration(SCENE_SECONDS)

        vids.append(CompositeVideoClip([base, subclip]).set_audio(a))

    final = concatenate_videoclips(vids, padding=-CROSSFADE)
    final = final.set_audio(concatenate_audioclips(auds))

    out = VID / f"{idx}_{slug(topic)}.mp4"
    final.write_videofile(
        str(out),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        logger=None,
    )
    return out

# ================= UI =================
st.set_page_config("Reel Factory", layout="wide")
st.title("🎬 Reel Factory — BIG Subtitles (Fixed)")

scenes_n = st.selectbox("Scenes per reel (10s each)", [6, 8])
reels_n = st.number_input("Reels to generate", 1, 20, 1)

if st.button("Generate Topics"):
    st.session_state.topics = gen_topics(20)

if "topics" in st.session_state:
    topics = st.session_state.topics[:reels_n]

    if st.button("Build"):
        vids = []
        for i, t in enumerate(topics, 1):
            scenes = gen_script(t, scenes_n)
            vids.append(build_reel(t, scenes, i))

        if len(vids) == 1:
            st.video(str(vids[0]))
            st.download_button("Download MP4", open(vids[0], "rb"), vids[0].name)
        else:
            zip_path = VID / "batch.zip"
            with zipfile.ZipFile(zip_path, "w") as z:
                for v in vids:
                    z.write(v, v.name)
            st.download_button("Download ALL", open(zip_path, "rb"), "reels.zip")
