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
from moviepy import AudioFileClip, CompositeVideoClip, ImageClip

# =============================
# SESSION STATE (MUST BE FIRST)
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
# MOVIEPY v1/v2 COMPAT HELPERS
# =============================
def clip_with_duration(clip, dur):
    return clip.with_duration(dur) if hasattr(clip, "with_duration") else clip.set_duration(dur)

def clip_with_start(clip, t):
    return clip.with_start(t) if hasattr(clip, "with_start") else clip.set_start(t)

def clip_with_audio(clip, audio):
    return clip.with_audio(audio) if hasattr(clip, "with_audio") else clip.set_audio(audio)

# =============================
# UTILS
# =============================
def fmt_time(s):
    s = int(max(0, s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:60]

# =============================
# TOPIC HISTORY (NO DUPLICATES)
# =============================
def load_history():
    if HISTORY_FILE.exists():
        try:
            return set(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_history(hist):
    HISTORY_FILE.write_text(json.dumps(sorted(hist), ensure_ascii=False, indent=2), encoding="utf-8")

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
    "Why do we blink?",
    "Why does your nose get stuffy on one side?",
    "Why does soda fizz?",
    "Why do magnets stick to some metals?",
    "Why do we feel dizzy after spinning?"
]

def generate_new_topics(n=20):
    used = set(x.lower() for x in TOPIC_HISTORY)
    pool = TOPIC_POOL[:]
    random.shuffle(pool)
    out = []
    for t in pool:
        if t.lower() in used:
            continue
        out.append(t)
        used.add(t.lower())
        if len(out) >= n:
            break
    return out

def cb_autogen():
    new = generate_new_topics(20)
    if not new:
        st.session_state["auto_error"] = "No new topics left. Clear history or expand topic pool."
        return
    for t in new:
        TOPIC_HISTORY.add(t.lower())
    save_history(TOPIC_HISTORY)
    st.session_state["topics_text"] = "\n".join(new)
    st.session_state["auto_error"] = ""

def cb_clear_history():
    TOPIC_HISTORY.clear()
    save_history(TOPIC_HISTORY)
    st.session_state["topics_text"] = ""
    st.session_state["auto_error"] = ""

# =============================
# SCRIPT GENERATOR (TOPIC SAFE)
# =============================
def script_pool(topic):
    t = (topic or "").lower()

    if "hiccup" in t:
        return [
            "Why do hiccups happen?",
            "Hiccups start when the diaphragm suddenly contracts.",
            "That pulls air in quickly.",
            "Then the vocal cords snap shut—making the “hic” sound.",
            "Triggers include eating too fast, soda, or sudden temperature change.",
            "Most hiccups stop on their own in minutes.",
            "Holding your breath raises CO₂ and can help stop them.",
            "That’s the simple biology behind hiccups.",
        ]

    if "fire" in t and "shadow" in t:
        return [
            "Why does fire have no shadow?",
            "A shadow forms when light is blocked.",
            "But fire emits light instead of blocking it.",
            "Flames are glowing gases and hot particles.",
            "That added light fills in the dark area where a shadow would be.",
            "So the shadow is usually weak or blurry, not sharp.",
            "Try it: use a bright flashlight behind a flame to force a shadow.",
        ]

    return [
        topic.strip() if topic else "Quick science explanation",
        "This happens because of a simple mechanism.",
        "It depends on how energy or signals move in the system.",
        "Small changes can produce a surprising result.",
        "Once you break it down, it becomes intuitive.",
        "Follow for more quick science reels.",
    ]

# =============================
# PEXELS IMAGES
# =============================
def pexels_images(query):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 20, "orientation": "portrait"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)
    r.raise_for_status()
    photos = r.json().get("photos", [])
    return photos

def prepare_image(url, caption, out_path):
    out_path.write_bytes(requests.get(url, timeout=25).content)

    img = Image.open(out_path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    lines = textwrap.wrap(caption, 30)[:3]
    box_h = 40 + len(lines) * 30
    y1 = HEIGHT - box_h - 60

    draw.rectangle([(0, y1), (WIDTH, HEIGHT)], fill=(0, 0, 0))

    y = y1 + 20
    for line in lines:
        draw.text((40, y), line, fill="white", font=font)
        y += 30

    img.save(out_path, quality=92)
    return out_path

# =============================
# VIDEO BUILDER (NO START HICCUP, SYNCED)
# =============================
def build_video(images, audio_path, crossfade=0.5):
    audio = AudioFileClip(str(audio_path))
    dur = float(audio.duration)

    n = max(1, len(images))
    # base duration per image
    D = dur / n

    # Build timeline with overlap crossfade by placing clips with small overlap
    # We keep total length exactly dur by distributing start times evenly.
    overlap = min(max(crossfade, 0.0), 1.5)
    step = max(0.05, D - overlap)

    # recompute D so final duration matches dur:
    # final = (n-1)*step + D  => D = dur - (n-1)*step
    # but step depends on D; solve by using:
    # final = n*D - (n-1)*overlap = dur => D = (dur + (n-1)*overlap)/n
    D = (dur + (n - 1) * overlap) / n
    step = D - overlap

    clips = []
    for i, img in enumerate(images):
        c = ImageClip(str(img))
        c = clip_with_duration(c, D)

        # IMPORTANT: first clip starts exactly at 0 (prevents start stutter)
        start_t = 0.0 if i == 0 else i * step
        c = clip_with_start(c, start_t)

        # MoviePy crossfade (v2 uses "with_effects", v1 has "crossfadein")
        # We'll do a safe opacity ramp by using Composite overlap:
        clips.append(c)

    video = CompositeVideoClip(clips, size=(WIDTH, HEIGHT))
    video = clip_with_duration(video, dur)
    video = clip_with_audio(video, audio)
    return video

# =============================
# BUILD ONE REEL (with progress callback)
# =============================
def build_reel(topic, idx, progress_cb=None, pexels_delay=0.25, crossfade=0.5):
    start = time.time()

    def cb(p, msg):
        if progress_cb:
            progress_cb(p, msg, time.time() - start)

    # Stage weights
    W_SCRIPT = 0.15
    W_IMAGES = 0.55
    W_RENDER = 0.30

    cb(0.01, "Generating script + narration (gTTS)...")
    script = script_pool(topic)
    narration = " ".join(script)

    audio_path = AUD_DIR / f"voice_{idx}.mp3"
    gTTS(narration).save(str(audio_path))
    cb(W_SCRIPT, "Narration ready. Fetching images...")

    images = []
    total_scenes = len(script)

    for si, scene_text in enumerate(script, start=1):
        p_scene = (si - 1) / max(1, total_scenes)
        cb(W_SCRIPT + W_IMAGES * p_scene, f"Fetching images {si}/{total_scenes}...")

        photos = pexels_images(scene_text)
        if not photos:
            raise RuntimeError(f"Pexels returned 0 images for: {scene_text}")

        # take 2 images per scene
        picks = photos[:IMAGES_PER_SCENE]
        for j, p in enumerate(picks, start=1):
            url = p["src"].get("portrait") or p["src"].get("large2x") or p["src"].get("large")
            out_path = IMG_DIR / f"reel{idx}_scene{si}_img{j}_{abs(hash(url))}.jpg"
            prepare_image(url, scene_text, out_path)
            images.append(out_path)

        time.sleep(pexels_delay)

    cb(W_SCRIPT + W_IMAGES, "Images ready. Rendering MP4...")

    cb(W_SCRIPT + W_IMAGES + W_RENDER * 0.2, "Compositing...")
    video = build_video(images, audio_path, crossfade=crossfade)

    out = VID_DIR / f"reel_{idx:02d}_{slugify(topic)}.mp4"
    cb(W_SCRIPT + W_IMAGES + W_RENDER * 0.5, "Encoding video...")
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    cb(1.0, "Done.")
    return out, time.time() - start

# =============================
# UI
# =============================
st.title("YouTube Reel Generator (Free: Pexels + gTTS)")

mode = st.radio("Mode", ["Single", "Batch (20)"], horizontal=True)

pexels_delay = st.slider("Delay between Pexels calls (seconds)", 0.0, 1.5, 0.25)
crossfade_seconds = st.slider("Crossfade seconds", 0.2, 1.2, 0.6)

def eta_remaining(elapsed, pct):
    if pct <= 0:
        return 0
    total_est = elapsed / pct
    return max(0, total_est - elapsed)

if mode == "Single":
    topic = st.text_input("Topic", "Why do hiccups happen?")

    if st.button("Generate Reel"):
        reel_bar = st.progress(0)
        reel_status = st.empty()
        reel_eta = st.empty()

        def cb(p, msg, elapsed):
            reel_bar.progress(int(p * 100))
            reel_status.write(f"{int(p*100)}% — {msg}")
            reel_eta.write(f"Estimated remaining: {fmt_time(eta_remaining(elapsed, p))}")

        mp4, _dt = build_reel(topic, 1, progress_cb=cb, pexels_delay=pexels_delay, crossfade=crossfade_seconds)
        st.success("Done.")
        st.video(str(mp4))
        st.download_button("Download MP4", open(mp4, "rb"), mp4.name, mime="video/mp4")

else:
    col1, col2 = st.columns([2, 1], vertical_alignment="top")

    with col1:
        st.text_area("Topics (one per line)", key="topics_text", height=280)
        if st.session_state["auto_error"]:
            st.error(st.session_state["auto_error"])

    with col2:
        st.subheader("Auto topics")
        st.button("Auto-generate 20 topics", on_click=cb_autogen)
        st.button("Clear topic history", on_click=cb_clear_history)
        st.caption(f"Remembered topics: {len(TOPIC_HISTORY)}")

    if st.button("Generate 20 Reels"):
        topics = [t.strip() for t in st.session_state["topics_text"].splitlines() if t.strip()][:20]
        if not topics:
            st.error("Add topics or click Auto-generate first.")
            st.stop()

        overall_bar = st.progress(0)
        overall_txt = st.empty()
        overall_eta = st.empty()

        outputs = []
        times = []

        for i, t in enumerate(topics, start=1):
            reel_bar = st.progress(0)
            reel_txt = st.empty()
            reel_eta = st.empty()

            def cb(p, msg, elapsed, i=i, t=t):
                reel_bar.progress(int(p * 100))
                reel_txt.write(f"Reel {i}/{len(topics)} — {int(p*100)}% — {t}")
                reel_eta.write(f"Reel ETA: {fmt_time(eta_remaining(elapsed, p))} — {msg}")

            out, dt = build_reel(t, i, progress_cb=cb, pexels_delay=pexels_delay, crossfade=crossfade_seconds)
            outputs.append(out)
            times.append(dt)

            overall_bar.progress(int(i / len(topics) * 100))
            overall_txt.write(f"Batch progress: {i}/{len(topics)} reels completed")

            avg = sum(times) / len(times)
            remaining = avg * (len(topics) - i)
            overall_eta.write(f"Batch ETA remaining: {fmt_time(remaining)} (avg/reel {fmt_time(avg)})")

        zip_path = VID_DIR / "reels_batch.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in outputs:
                z.write(p, arcname=p.name)

        st.success("Batch complete.")
        st.download_button("Download ZIP (all MP4s)", open(zip_path, "rb"), zip_path.name, mime="application/zip")
        if outputs:
            st.video(str(outputs[0]))
