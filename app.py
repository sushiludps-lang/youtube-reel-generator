import json, time, re, os
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Any
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
FONT_SIZE = 84
AUDIO_FPS = 44100

BASE = Path(__file__).parent
IMG = BASE / "images"
AUD = BASE / "audio"
VID = BASE / "video"
CACHE = BASE / "cache"
for d in (IMG, AUD, VID, CACHE):
    d.mkdir(exist_ok=True)

TOPIC_HISTORY = CACHE / "topics_history.json"
REELS_DB = CACHE / "reels_db.json"

# -------------------- KEYS --------------------
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
PEXELS_KEY = st.secrets["PEXELS_API_KEY"]
client = genai.Client(api_key=GEMINI_API_KEY)
TEXT_MODEL = "models/gemini-2.5-flash"

# -------------------- STORAGE --------------------
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def history_list() -> List[str]:
    return load_json(TOPIC_HISTORY, [])

def db_obj() -> Dict[str, Any]:
    return load_json(REELS_DB, {"reels": {}})

# -------------------- HELPERS --------------------
def slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (t or "").lower()).strip("_")[:60] or "reel"

def silence(d: float):
    return AudioClip(lambda t: np.zeros((1,), dtype=np.float32), duration=d, fps=AUDIO_FPS)

def fit_audio(a: AudioFileClip, d: float):
    if a.duration > d:
        return a.subclip(0, d)
    if a.duration < d:
        return concatenate_audioclips([a, silence(d - a.duration)]).set_duration(d)
    return a

def get_font():
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", FONT_SIZE)
    except Exception:
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", FONT_SIZE)
        except Exception:
            return ImageFont.load_default()

FONT = get_font()

def subtitle_png(text: str, out: Path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # wrap to 2 lines
    words = (text or "").split()
    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        if len(test) <= 30:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    lines = lines[:2] or [""]

    # measure
    spacing = 12
    widths, heights = [], []
    for line in lines:
        bb = d.textbbox((0, 0), line, font=FONT)
        widths.append(bb[2] - bb[0])
        heights.append(bb[3] - bb[1])

    tw = max(widths) if widths else 0
    th = sum(heights) + spacing * (len(lines) - 1)

    pad_x, pad_y = 60, 34
    box_w = tw + 2 * pad_x
    box_h = th + 2 * pad_y

    x1 = (W - box_w) // 2
    y2 = H - 140
    y1 = y2 - box_h
    x2 = x1 + box_w

    # box
    d.rounded_rectangle((x1, y1, x2, y2), radius=36, fill=(0, 0, 0, 200))

    # centered text
    y = y1 + pad_y
    for i, line in enumerate(lines):
        bb = d.textbbox((0, 0), line, font=FONT)
        lw = bb[2] - bb[0]
        lx = (W - lw) // 2
        d.text((lx, y), line, fill=(255, 255, 255, 255), font=FONT)
        y += heights[i] + spacing

    img.save(out)

def pexels(query: str, per_page: int = 15):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_KEY},
        params={"query": query, "orientation": "portrait", "per_page": per_page},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("photos", [])

def get_image(url: str, out: Path):
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    img = ImageOps.fit(img, (W, H))
    img.save(out, quality=90)

def placeholder(out: Path, text: str):
    im = Image.new("RGB", (W, H), (30, 30, 35))
    d = ImageDraw.Draw(im)
    d.text((60, 80), "PLACEHOLDER", fill=(240, 240, 240), font=FONT)
    d.text((60, 200), (text or "")[:120], fill=(240, 240, 240), font=FONT)
    im.save(out, quality=90)

# -------------------- JSON SAFE PARSE --------------------
def parse_json_strict(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        s = raw.find("{")
        e = raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(raw[s:e+1])
        raise

# -------------------- GEMINI --------------------
def generate_topics(n: int) -> List[str]:
    existing = {t.lower() for t in history_list()}
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Generate {n} unique YouTube Shorts science/curiosity topics.
Rules:
- Each is a short question (max 12 words).
- No repeats or near-repeats.
- Do NOT include any of these existing topics:
{list(existing)[:400]}

Format exactly:
{{"topics": ["..."]}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = parse_json_strict(r.text)
    topics = []
    for t in data.get("topics", []):
        t = (t or "").strip()
        if not t:
            continue
        if t.lower() in existing:
            continue
        topics.append(t)
        existing.add(t.lower())
        if len(topics) >= n:
            break

    # update history
    hist = history_list()
    hist.extend([t.lower() for t in topics])
    hist = list(dict.fromkeys(hist))
    save_json(TOPIC_HISTORY, hist)
    return topics

def generate_script(topic: str, scenes: int) -> List[Dict[str, str]]:
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Make a script for:
Topic: "{topic}"

Hard rules:
- EXACTLY {scenes} scenes
- Each scene spoken in ~10 seconds
- Each scene object must have:
  subtitle (1–2 sentences)
  image_query (search phrase)

Format exactly:
{{
  "scenes": [
    {{"subtitle":"...", "image_query":"..."}}
  ]
}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = parse_json_strict(r.text)
    scenes_list = data.get("scenes", [])
    if not isinstance(scenes_list, list):
        scenes_list = []

    scenes_list = scenes_list[:scenes]
    while len(scenes_list) < scenes:
        scenes_list.append({"subtitle": "Here is the key idea in simple terms.", "image_query": topic})

    cleaned = []
    for s in scenes_list:
        cleaned.append({
            "subtitle": str(s.get("subtitle", "")).strip() or "Here is the key idea in simple terms.",
            "image_query": str(s.get("image_query", "")).strip() or topic
        })
    return cleaned

# -------------------- BUILD REEL --------------------
def build_reel(topic: str, scenes_list: List[Dict[str, str]], idx: int, cb=None) -> Path:
    reel_id = f"reel{idx:02d}_{slug(topic)}_{int(time.time())}"
    clips, auds = [], []

    total = len(scenes_list)
    for si, sc in enumerate(scenes_list, 1):
        if cb:
            cb((si-1)/max(1,total), f"Scene {si}/{total}: images + voice")

        photos = []
        try:
            photos = pexels(sc["image_query"], per_page=20)
        except Exception:
            photos = []

        img_paths = []
        for j in range(IMAGES_PER_SCENE):
            out = IMG / f"{reel_id}_s{si:02d}_i{j+1:02d}.jpg"
            if j < len(photos):
                url = photos[j].get("src", {}).get("portrait") or photos[j].get("src", {}).get("large2x")
                try:
                    if url:
                        get_image(url, out)
                    else:
                        placeholder(out, f"{topic} / scene {si}")
                except Exception:
                    placeholder(out, f"{topic} / scene {si}")
            else:
                placeholder(out, f"{topic} / scene {si}")
            img_paths.append(out)

        # 2 images with crossfade inside scene
        c1 = ImageClip(str(img_paths[0])).set_duration(IMAGE_SECONDS)
        c2 = ImageClip(str(img_paths[1])).set_duration(IMAGE_SECONDS).crossfadein(CROSSFADE)
        scene_vid = concatenate_videoclips([c1, c2], method="compose", padding=-CROSSFADE).set_duration(SCENE_SECONDS)

        # subtitle overlay
        sub_path = IMG / f"{reel_id}_sub_{si:02d}.png"
        subtitle_png(sc["subtitle"], sub_path)
        sub = ImageClip(str(sub_path)).set_duration(SCENE_SECONDS)

        composed = CompositeVideoClip([scene_vid, sub], size=(W, H)).set_duration(SCENE_SECONDS)

        # per-scene audio exactly 10s
        mp3 = AUD / f"{reel_id}_scene_{si:02d}.mp3"
        gTTS(sc["subtitle"]).save(str(mp3))
        a = fit_audio(AudioFileClip(str(mp3)), SCENE_SECONDS)

        composed = composed.set_audio(a)
        clips.append(composed)
        auds.append(a)

    if cb:
        cb(0.85, "Stitching scenes...")

    # crossfade between scenes
    for k in range(1, len(clips)):
        clips[k] = clips[k].crossfadein(CROSSFADE)

    video = concatenate_videoclips(clips, method="compose", padding=-CROSSFADE)
    audio = concatenate_audioclips(auds).set_duration(len(scenes_list) * SCENE_SECONDS)
    video = video.set_audio(audio).set_duration(len(scenes_list) * SCENE_SECONDS)

    out = VID / f"{reel_id}.mp4"

    if cb:
        cb(0.92, "Exporting MP4...")

    video.write_videofile(
        str(out),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",
        threads=2,
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        logger=None,
    )

    if cb:
        cb(1.0, "Done.")

    try:
        video.close()
    except Exception:
        pass

    return out

# -------------------- UI --------------------
st.set_page_config(page_title="Reel Factory", layout="wide")
st.title("Reel Factory — Gemini Script + Pexels Images + Synced Subtitles")

col1, col2 = st.columns([1.1, 0.9])

if "topics" not in st.session_state:
    st.session_state["topics"] = []
if "selected" not in st.session_state:
    st.session_state["selected"] = None

with col1:
    st.subheader("1) Topics")
    how_many = st.slider("How many new topics?", 1, 20, 10)
    scenes_n = st.selectbox("Scenes per reel", [6, 8], index=0)
    st.caption(f"{scenes_n} scenes × {SCENE_SECONDS}s = ~{scenes_n*SCENE_SECONDS}s reel. (2 images/scene)")

    if st.button("Generate Topics"):
        with st.spinner("Generating topics..."):
            st.session_state["topics"] = generate_topics(how_many)
            st.session_state["selected"] = st.session_state["topics"][0] if st.session_state["topics"] else None

    if st.session_state["topics"]:
        st.session_state["selected"] = st.selectbox("Pick one topic", st.session_state["topics"])

with col2:
    st.subheader("2) Build Reel(s)")
    topic = st.session_state.get("selected")
    if not topic:
        st.info("Generate topics and select one.")
    else:
        reels_to_build = st.slider("How many reels to build now?", 1, min(20, len(st.session_state["topics"])), 1)
        build_list = st.session_state["topics"][:reels_to_build]

        if st.button("Build Now"):
            vids = []
            overall = st.progress(0)
            status = st.empty()
            eta = st.empty()

            start_all = time.time()
            for i, tp in enumerate(build_list, 1):
                t0 = time.time()
                status.write(f"Reel {i}/{len(build_list)}: {tp}")

                def cb(p, msg):
                    overall.progress((i-1 + p) / len(build_list))
                    elapsed = time.time() - start_all
                    done_frac = (i-1 + p) / len(build_list)
                    if done_frac > 0:
                        remaining = (elapsed / done_frac) - elapsed
                        eta.write(f"ETA ~ {int(max(0, remaining))}s")
                    status.write(f"Reel {i}/{len(build_list)} — {int(p*100)}%: {msg}")

                scenes_list = generate_script(tp, scenes_n)
                out = build_reel(tp, scenes_list, i, cb=cb)

                # Save to DB
                db = db_obj()
                key = tp.lower()
                db["reels"][key] = {
                    "topic": tp,
                    "scenes": scenes_list,
                    "video_path": str(out),
                    "updated_at": int(time.time()),
                }
                save_json(REELS_DB, db)

                vids.append(out)
                status.write(f"✅ Done reel {i} in {int(time.time()-t0)}s")

            st.success("All reels completed.")

            # show + download
            for v in vids:
                st.video(str(v))
                st.download_button(
                    f"Download {v.name}",
                    open(v, "rb"),
                    file_name=v.name,
                    mime="video/mp4",
                )

            if len(vids) > 1:
                zip_path = VID / f"reels_{int(time.time())}.zip"
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                    for v in vids:
                        z.write(v, arcname=v.name)
                st.download_button(
                    "Download ALL as ZIP",
                    open(zip_path, "rb"),
                    file_name=zip_path.name,
                    mime="application/zip",
                )
