import json, time, re
from pathlib import Path
from typing import List
import zipfile
import requests
import numpy as np
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# ----------------------------
# FFmpeg fix for Streamlit Cloud
# ----------------------------
try:
    import imageio_ffmpeg
    import os
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# MoviePy (Cloud-safe)
from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip,
)

# Gemini
from google import genai
from google.genai import types

# ---------------- CONFIG ----------------
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE
CROSSFADE = 0.7  # smooth
AUDIO_FPS = 44100

BASE = Path(__file__).parent
IMG = BASE / "images"
AUD = BASE / "audio"
VID = BASE / "video"
for d in (IMG, AUD, VID):
    d.mkdir(exist_ok=True)

# ---------------- SECRETS ----------------
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]
client = genai.Client(api_key=GEMINI_API_KEY)
TEXT_MODEL = "models/gemini-2.5-flash"

# ---------------- HELPERS ----------------
def slug(t: str) -> str:
    t = (t or "").lower().strip()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    return t.strip("_")[:60] or "reel"

def pexels(q: str):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": q, "orientation": "portrait", "per_page": 15},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("photos", [])

def download_img(url: str, out: Path):
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out.write_bytes(r.content)
    img = Image.open(out).convert("RGB")
    img = ImageOps.exif_transpose(img)
    # force exactly 1080x1920 (no black bars)
    img = ImageOps.fit(img, (W, H))
    img.save(out, quality=92)

def placeholder(out: Path, text: str):
    img = Image.new("RGB", (W, H), (20, 20, 20))
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default()
    d.text((50, 80), "PLACEHOLDER", fill="white", font=f)
    d.text((50, 140), text[:220], fill="white", font=f)
    img.save(out, quality=92)

def subtitle_png(text: str, out: Path):
    # robust subtitles overlay layer
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default()
    text = (text or "").strip() or "..."
    bbox = d.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (W - tw) // 2
    y = H - 230
    pad_x, pad_y = 30, 18
    d.rectangle((x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y), fill=(0, 0, 0, 180))
    d.text((x, y), text, fill=(255, 255, 255, 255), font=f)
    img.save(out)

def silence(dur: float) -> AudioClip:
    return AudioClip(lambda t: np.zeros((1,), dtype=np.float32), duration=dur, fps=AUDIO_FPS)

def fit_audio(a: AudioFileClip, dur: float):
    if a.duration > dur:
        return a.subclip(0, dur)
    if a.duration < dur:
        return concatenate_audioclips([a, silence(dur - a.duration)]).set_duration(dur)
    return a

# ---------------- GEMINI ----------------
def gen_topics(existing: List[str], n: int):
    prompt = f"""
Return JSON only:
{{"topics": ["..."]}}

Generate {n} unique science/curiosity YouTube Shorts topics as short questions (<= 12 words).
Avoid duplicates with this list (case-insensitive):
{existing}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = json.loads((r.text or "").strip())
    return [t.strip() for t in data.get("topics", []) if t and t.strip()]

def gen_script(topic: str, scenes: int):
    prompt = f"""
Return JSON only.
Topic: "{topic}"

Exactly {scenes} scenes.
Each scene is spoken in ~10 seconds.
Format:
{{"scenes":[{{"subtitle":"...","query":"..."}}]}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = json.loads((r.text or "").strip())
    scenes_list = data.get("scenes", [])
    if not isinstance(scenes_list, list):
        scenes_list = []
    scenes_list = scenes_list[:scenes]
    while len(scenes_list) < scenes:
        scenes_list.append({"subtitle": "Here is the key idea in simple terms.", "query": topic})

    cleaned = []
    for s in scenes_list:
        sub = (s.get("subtitle") or "").strip() or "Here is the key idea in simple terms."
        q = (s.get("query") or topic).strip() or topic
        cleaned.append({"subtitle": sub, "query": q})
    return cleaned

# ---------------- BUILD REEL ----------------
def build_reel(topic: str, scenes: list, idx: int, cb):
    start = time.time()
    def prog(p, m):
        cb(p, m, int(time.time() - start))

    # 1) Images
    prog(0.05, "Fetching images")
    all_imgs = []
    for i, s in enumerate(scenes, 1):
        q = s["query"]
        try:
            photos = pexels(q)
        except Exception:
            photos = []

        pair = []
        for j in range(IMAGES_PER_SCENE):
            out = IMG / f"{idx:02d}_s{i:02d}_i{j:02d}.jpg"
            try:
                url = (photos[j].get("src", {}) or {}).get("portrait")
                if url:
                    download_img(url, out)
                else:
                    raise RuntimeError("no url")
            except Exception:
                placeholder(out, f"{topic} / scene {i}")
            pair.append(out)
        all_imgs.append(pair)
        prog(0.05 + 0.25 * (i / len(scenes)), f"Images {i}/{len(scenes)}")

    # 2) Audio (each scene forced to 10s)
    prog(0.35, "Generating voiceover")
    aud_clips = []
    for i, s in enumerate(scenes, 1):
        mp3 = AUD / f"{idx:02d}_scene_{i:02d}.mp3"
        gTTS(s["subtitle"]).save(str(mp3))
        a = AudioFileClip(str(mp3))
        aud_clips.append(fit_audio(a, SCENE_SECONDS))
        prog(0.35 + 0.20 * (i / len(scenes)), f"Audio {i}/{len(scenes)}")

    total_seconds = len(scenes) * SCENE_SECONDS
    full_audio = concatenate_audioclips(aud_clips).set_duration(total_seconds)

    # 3) Video clips per scene
    prog(0.60, "Building video")
    scene_clips = []
    for i, (pair, s) in enumerate(zip(all_imgs, scenes), 1):
        # image 1 + image 2 with crossfade (NO resize to avoid Pillow ANTIALIAS bug)
        c1 = ImageClip(str(pair[0])).set_duration(IMG_SECONDS)
        c2 = ImageClip(str(pair[1])).set_duration(IMG_SECONDS).crossfadein(CROSSFADE)

        base = concatenate_videoclips(
            [c1, c2],
            method="compose",
            padding=-CROSSFADE
        ).set_duration(SCENE_SECONDS)

        # subtitles overlay for full 10s
        sub = IMG / f"{idx:02d}_sub_{i:02d}.png"
        subtitle_png(s["subtitle"], sub)
        subclip = ImageClip(str(sub)).set_duration(SCENE_SECONDS)

        scene = CompositeVideoClip([base, subclip], size=(W, H)).set_duration(SCENE_SECONDS)
        scene = scene.set_audio(aud_clips[i - 1]).set_duration(SCENE_SECONDS)
        scene_clips.append(scene)

        prog(0.60 + 0.25 * (i / len(scenes)), f"Scene {i}/{len(scenes)}")

    # crossfade between scenes
    for k in range(1, len(scene_clips)):
        scene_clips[k] = scene_clips[k].crossfadein(CROSSFADE)

    final = concatenate_videoclips(scene_clips, method="compose", padding=-CROSSFADE)
    final = final.set_audio(full_audio).set_duration(total_seconds)

    # 4) Export
    prog(0.90, "Exporting MP4")
    out = VID / f"{idx:02d}_{slug(topic)}.mp4"
    final.write_videofile(
        str(out),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        preset="ultrafast",
        threads=2,
        logger=None,
    )

    prog(1.0, "Done")
    try:
        final.close()
    except Exception:
        pass
    return out

# ---------------- UI ----------------
st.set_page_config(page_title="Reel Factory", layout="wide")
st.title("Reel Factory — Gemini Script + Pexels Images + Subtitles + Smooth Crossfade")

scenes_n = st.selectbox("Scenes per reel (each 10s)", [6, 8], index=0)
reels_n = st.number_input("How many reels to generate now?", min_value=1, max_value=20, value=1, step=1)

if "topics" not in st.session_state:
    st.session_state["topics"] = []

col1, col2 = st.columns([1.1, 0.9])

with col1:
    st.subheader("Step 1 — Generate Topics")
    if st.button("Generate 20 Topics"):
        with st.spinner("Generating topics..."):
            st.session_state["topics"] = gen_topics([], 20)

    topics = st.session_state["topics"]
    if topics:
        st.write("Topics:")
        for i, t in enumerate(topics[:20], 1):
            st.write(f"{i}. {t}")
    else:
        st.info("Click 'Generate 20 Topics' first.")

with col2:
    st.subheader("Step 2 — Build Reels (1 to N)")
    topics = st.session_state["topics"]
    if not topics:
        st.warning("Generate topics first.")
    else:
        topics_to_build = topics[: int(reels_n)]
        st.write(f"Will build: {len(topics_to_build)} reel(s).")

        progress = st.progress(0)
        status = st.empty()
        eta = st.empty()

        def cb(p, msg, elapsed):
            progress.progress(int(p * 100))
            status.write(f"{int(p*100)}% — {msg}")
            if p > 0.01:
                remaining = (elapsed / p) - elapsed
                eta.write(f"ETA ~ {max(0, int(remaining))}s")

        if st.button("Build Now"):
            built = []
            for i, topic in enumerate(topics_to_build, 1):
                st.write(f"Building Reel {i}: {topic}")
                with st.spinner("Generating script..."):
                    scenes = gen_script(topic, scenes_n)
                out = build_reel(topic, scenes, i, cb)
                built.append(out)

            st.success("Build complete.")

            if len(built) == 1:
                out = built[0]
                st.video(str(out))
                st.download_button("Download MP4", data=open(out, "rb"), file_name=out.name, mime="video/mp4")
            else:
                zip_path = VID / "batch_reels.zip"
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                    for out in built:
                        z.write(out, arcname=out.name)
                st.download_button("Download ALL (ZIP)", data=open(zip_path, "rb"), file_name="batch_reels.zip", mime="application/zip")
