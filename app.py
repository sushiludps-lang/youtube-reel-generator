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

try:
    from moviepy import vfx
except Exception:
    vfx = None

# ======================================================
# FIX: session_state must be initialized BEFORE widgets
# ======================================================
if "topics_text" not in st.session_state:
    st.session_state["topics_text"] = ""

# ===============================
# CONFIG / FOLDERS
# ===============================
WIDTH, HEIGHT = 1080, 1920
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE_DIR = BASE / "cache"
CACHE_FILE = CACHE_DIR / "pexels_cache.json"
TOPIC_HISTORY_FILE = CACHE_DIR / "topic_history.json"

for d in (IMG_DIR, AUD_DIR, VID_DIR, CACHE_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]
IMAGES_PER_SCENE = 2  # fixed

# ===============================
# UTIL: JSON FILES
# ===============================
def load_json_file(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json_file(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

# ===============================
# CACHE: PEXELS
# ===============================
PEXELS_CACHE = load_json_file(CACHE_FILE, {})

# ===============================
# TOPIC HISTORY
# ===============================
def load_topic_history():
    data = load_json_file(TOPIC_HISTORY_FILE, {"topics": []})
    topics = data.get("topics", [])
    norm = []
    seen = set()
    for t in topics:
        t2 = (t or "").strip()
        if not t2:
            continue
        k = t2.lower()
        if k in seen:
            continue
        seen.add(k)
        norm.append(t2)
    return norm

def save_topic_history(topics_list):
    save_json_file(TOPIC_HISTORY_FILE, {"topics": topics_list, "updated_at": datetime.utcnow().isoformat()})

TOPIC_HISTORY = load_topic_history()

def remember_topics(topics):
    global TOPIC_HISTORY
    cur = TOPIC_HISTORY[:]
    s = set(t.lower() for t in cur)
    for t in topics:
        t2 = (t or "").strip()
        if not t2:
            continue
        k = t2.lower()
        if k in s:
            continue
        cur.append(t2)
        s.add(k)
    TOPIC_HISTORY = cur
    save_topic_history(TOPIC_HISTORY)

# ===============================
# UI
# ===============================
st.title("YouTube Reel Generator — Batch + Auto Topics (No duplicates)")

mode = st.radio("Mode", ["Single Reel", "Batch (20 Reels)"], horizontal=True)

target_scene_seconds = st.slider("Target seconds per scene", 4, 12, 7)
min_video_seconds = st.slider("Minimum video length (seconds)", 10, 60, 45)
min_scenes = st.slider("Minimum scenes", 2, 12, 6)
max_scenes = st.slider("Maximum scenes", 3, 20, 10)

ENABLE_CAPTIONS = st.toggle("Burn captions on images", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

crossfade_seconds = st.slider("Crossfade seconds", 0.2, 1.2, 0.7)
pexels_delay = st.slider("Delay between Pexels calls (seconds)", 0.0, 1.5, 0.25)
DEBUG = st.toggle("Show debug", False)

# ===============================
# FONT LOADER (cloud-safe)
# ===============================
def load_font(size):
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
# CAPTION BURN (PIL)
# ===============================
def burn_caption(img, caption):
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    lines = textwrap.wrap(caption, width=28)[:3]
    line_h = int(CAPTION_FONT_SIZE * 1.2)

    box_h = 60 + line_h * len(lines) + 30
    y1 = HEIGHT - box_h - 120
    y2 = HEIGHT - 120

    draw.rectangle(
        [(60, y1), (WIDTH - 60, y2)],
        fill=(0, 0, 0, int(CAPTION_BOX_OPACITY)),
    )

    y = y1 + 35
    for line in lines:
        draw.text((90, y), line, font=FONT, fill=(255, 255, 255, 255))
        y += line_h

    return Image.alpha_composite(img, overlay).convert("RGB")

# ===============================
# HELPERS
# ===============================
def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:60] or "topic"

def pexels_search(query: str):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 30, "orientation": "portrait", "size": "large"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json().get("photos", [])

def download_and_prepare_image(url: str, out_path: Path, caption_text: str):
    out_path.write_bytes(requests.get(url, timeout=25).content)
    img = Image.open(out_path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    if ENABLE_CAPTIONS:
        img = burn_caption(img, caption_text)
    img.save(out_path, quality=95)
    return out_path

# ===============================
# PEXELS FETCH (2 images/scene) + cache
# ===============================
def fetch_images(scene_text: str):
    cache_key = f"{scene_text}__{int(ENABLE_CAPTIONS)}__{CAPTION_FONT_SIZE}__{CAPTION_BOX_OPACITY}"
    cached = PEXELS_CACHE.get(cache_key, [])
    cached_paths = [Path(p) for p in cached if Path(p).exists()]

    if len(cached_paths) >= IMAGES_PER_SCENE:
        return cached_paths[:IMAGES_PER_SCENE]

    photos = pexels_search(scene_text)
    time.sleep(pexels_delay)

    paths = list(cached_paths)
    idx = 0
    for p in photos:
        if len(paths) >= IMAGES_PER_SCENE:
            break
        url = p.get("src", {}).get("portrait") or p.get("src", {}).get("large")
        if not url:
            continue

        img_path = IMG_DIR / f"img_{abs(hash((scene_text, idx, time.time_ns())))}.jpg"
        idx += 1

        try:
            download_and_prepare_image(url, img_path, scene_text)
            paths.append(img_path)
        except Exception:
            continue

    if not paths:
        return []

    while len(paths) < IMAGES_PER_SCENE:
        paths.append(paths[-1])

    PEXELS_CACHE[cache_key] = [str(p) for p in paths[:IMAGES_PER_SCENE]]
    save_json_file(CACHE_FILE, PEXELS_CACHE)

    return paths[:IMAGES_PER_SCENE]

# ===============================
# SCRIPT POOL (free, no LLM)
# ===============================
def script_pool(topic: str):
    return [
        f"{topic} — quick answer.",
        "A shadow needs a strong background light and something that blocks it.",
        "Fire isn’t a solid object; it’s hot gas plus glowing soot particles.",
        "Because fire emits light, it can fill in the dark area you expect.",
        "Flames are partly transparent, so they don’t block all light strongly.",
        "That’s why a candle often won’t cast a sharp shadow on a wall.",
        "But a brighter light behind the flame can force a visible shadow.",
        "Try it: flashlight behind a lighter, then look at the wall edge.",
        "If the flame is dim and the background light is strong, shadow becomes clearer.",
        "If the flame is bright, it washes out the shadow contrast.",
        "Moving flames blur edges, which makes shadows look weak.",
        "So it’s not zero shadow—usually it’s no crisp shadow in normal lighting.",
        "You can tune light intensity to make it appear or disappear.",
        "That’s the physics: emission + transparency + contrast.",
        "Follow for more quick science reels.",
    ]

# ===============================
# Build script until long enough (TTS duration-driven)
# ===============================
def tts_duration_for(script_lines):
    narration = " ".join(script_lines)
    tmp_mp3 = AUD_DIR / "tmp_voice.mp3"
    gTTS(narration).save(str(tmp_mp3))
    a = AudioFileClip(str(tmp_mp3))
    dur = a.duration
    a.close()
    return tmp_mp3, dur

def build_script(topic, target_scene_sec, min_secs, min_s, max_s):
    pool = script_pool(topic)

    n = max(min_s, 2)
    n = min(n, max_s, len(pool))
    script = pool[:n]

    for _ in range(30):
        mp3, dur = tts_duration_for(script)

        scenes_needed = int(round(dur / target_scene_sec))
        scenes_needed = max(min_s, min(max_s, scenes_needed, len(pool)))

        if dur < min_secs and len(script) < max_s and len(script) < len(pool):
            script = pool[:len(script) + 1]
            continue

        if scenes_needed > len(script):
            script = pool[:scenes_needed]
            continue

        return script, mp3, dur

    mp3, dur = tts_duration_for(script)
    return script, mp3, dur

# ===============================
# True crossfade synced to audio duration
# ===============================
def build_crossfade_video_synced(image_paths, audio_duration, xfade):
    n = len(image_paths)
    if n == 0:
        raise ValueError("No images to build video.")

    xfade = max(0.0, min(xfade, 2.0))

    # D so final duration == audio_duration:
    # final = n*D - (n-1)*xfade  =>  D = (audio + (n-1)*xfade)/n
    D = (audio_duration + (n - 1) * xfade) / n
    if D <= xfade * 1.1:
        xfade = D * 0.3
        D = (audio_duration + (n - 1) * xfade) / n

    step = D - xfade

    clips = []
    for i, p in enumerate(image_paths):
        c = ImageClip(str(p), duration=D)
        start_t = i * step
        c = c.with_start(start_t) if hasattr(c, "with_start") else c.set_start(start_t)

        if vfx is not None and hasattr(c, "with_effects"):
            effs = []
            if i > 0 and hasattr(vfx, "CrossFadeIn"):
                effs.append(vfx.CrossFadeIn(xfade))
            if i < n - 1 and hasattr(vfx, "CrossFadeOut"):
                effs.append(vfx.CrossFadeOut(xfade))
            if effs:
                try:
                    c = c.with_effects(effs)
                except Exception:
                    pass

        clips.append(c)

    video = CompositeVideoClip(clips, size=(WIDTH, HEIGHT))
    video = video.with_duration(audio_duration) if hasattr(video, "with_duration") else video.set_duration(audio_duration)
    return video

# ===============================
# SINGLE REEL BUILDER
# ===============================
def build_one_reel(topic: str, index: int):
    script, voice_path, _dur = build_script(
        topic, target_scene_seconds, min_video_seconds, min_scenes, max_scenes
    )

    audio = AudioFileClip(str(voice_path))

    image_paths = []
    for scene_text in script:
        imgs = fetch_images(scene_text)
        if not imgs:
            audio.close()
            raise RuntimeError("No images from Pexels for a scene. Try a different topic.")
        image_paths.extend(imgs)

    video = build_crossfade_video_synced(image_paths, audio.duration, crossfade_seconds)
    video = video.with_audio(audio) if hasattr(video, "with_audio") else video.set_audio(audio)

    out = VID_DIR / f"reel_{index:02d}_{slugify(topic)}.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    try:
        video.close()
    except Exception:
        pass
    try:
        audio.close()
    except Exception:
        pass

    return out

# ===============================
# AUTO TOPIC GENERATOR (no duplicates)
# ===============================
PILLARS = {
    "Physics": [
        "fire", "shadow", "heat", "sound", "electricity", "motion", "pressure", "gravity",
        "friction", "waves", "reflection", "refraction", "light", "static electricity"
    ],
    "Chemistry": [
        "water", "salt", "soap", "oil", "rust", "bubbles", "glass", "ice", "sugar",
        "coffee", "vinegar", "baking soda", "oxygen", "carbon dioxide"
    ],
    "Biology": [
        "sleep", "yawning", "heartbeat", "muscles", "brain", "eyes", "smell", "taste",
        "skin", "sweat", "goosebumps", "hiccups"
    ],
    "Space": [
        "moon", "stars", "black holes", "planets", "sun", "comets", "aurora",
        "time dilation", "meteorites", "galaxies", "satellites"
    ],
    "Mind": [
        "paradox", "illusion", "probability", "memory", "attention", "habit",
        "decision", "confidence", "bias", "pattern", "luck"
    ],
}

TEMPLATES = [
    "Why does {x} happen?",
    "Why doesn’t {x} do what we expect?",
    "What happens if {x} changes?",
    "Most people think {x}, but is it true?",
]

def make_candidates():
    candidates = []
    for words in PILLARS.values():
        for w in words:
            for tpl in TEMPLATES:
                q = tpl.format(x=w).strip()
                if len(q) <= 70:
                    candidates.append(q)
    return candidates

ALL_CANDIDATES = make_candidates()

def generate_new_topics(n=20):
    used = set(t.lower() for t in TOPIC_HISTORY)

    seed = int(datetime.utcnow().strftime("%Y%m%d"))
    rng = random.Random(seed + random.randint(0, 10_000_000))

    pool = ALL_CANDIDATES[:]
    rng.shuffle(pool)

    out = []
    for q in pool:
        k = q.lower().strip()
        if k in used:
            continue
        out.append(q)
        used.add(k)
        if len(out) >= n:
            break

    return out

# ===============================
# UI: Single or Batch
# ===============================
if mode == "Single Reel":
    topic_single = st.text_input("Single topic", "Why does fire have no shadow?")
    if st.button("Generate 1 Reel"):
        remember_topics([topic_single])
        with st.spinner("Generating..."):
            mp4_path = build_one_reel(topic_single, 1)
        st.success("Done.")
        st.video(str(mp4_path))
        st.download_button("Download MP4", open(mp4_path, "rb"), mp4_path.name, mime="video/mp4")

else:
    col1, col2 = st.columns([2, 1], vertical_alignment="top")

    with col1:
        st.text_area(
            "Topics (one per line) — use Auto-generate to fill",
            key="topics_text",
            height=320,
            placeholder="Click Auto-generate 20 new topics…"
        )

    with col2:
        st.subheader("Auto topics")

        if st.button("Auto-generate 20 NEW topics"):
            new_topics = generate_new_topics(20)
            if not new_topics:
                st.error("No new topics left in the pool. Clear history or expand pool.")
            else:
                remember_topics(new_topics)
                st.session_state["topics_text"] = "\n".join(new_topics)
                st.experimental_rerun()  # IMPORTANT

        if st.button("Clear topic history (start over)"):
            save_topic_history([])
            TOPIC_HISTORY[:] = load_topic_history()
            st.session_state["topics_text"] = ""
            st.experimental_rerun()

        st.caption(f"Remembered topics: {len(TOPIC_HISTORY)}")

    if st.button("Generate Batch (up to 20 Reels)"):
        topics = [t.strip() for t in st.session_state.get("topics_text", "").splitlines() if t.strip()]
        topics = topics[:20]

        if not topics:
            st.error("Add topics or click Auto-generate first.")
            st.stop()

        remember_topics(topics)

        prog = st.progress(0)
        status = st.empty()

        outputs = []
        errors = []

        for i, t in enumerate(topics, start=1):
            status.write(f"Generating {i}/{len(topics)}: {t}")
            try:
                mp4_path = build_one_reel(t, i)
                outputs.append(mp4_path)
            except Exception as e:
                errors.append((t, str(e)))

            prog.progress(int(i / len(topics) * 100))

        zip_path = VID_DIR / "reels_batch.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for p in outputs:
                z.write(p, arcname=p.name)

        if errors:
            st.error("Some reels failed:")
            for t, msg in errors:
                st.write(f"- {t}: {msg}")

        st.success(f"Batch done: {len(outputs)}/{len(topics)} reels created.")
        st.download_button("Download ZIP (all MP4s)", open(zip_path, "rb"), zip_path.name, mime="application/zip")

        if outputs:
            st.write("Preview (first reel):")
            st.video(str(outputs[0]))

        if DEBUG:
            st.write("Saved files:")
            for p in outputs:
                st.write(p.name)
