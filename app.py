import json
import os
import random
import re
import textwrap
import time
import zipfile
from pathlib import Path

import numpy as np
import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

# -------- MoviePy imports (v1.0.3 stable) ----------
from moviepy.editor import (
    AudioFileClip,
    ImageClip,
    concatenate_videoclips,
    vfx,
)

# Ensure ffmpeg works on Streamlit Cloud
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# =============================
# SESSION STATE (MUST BE EARLY)
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

# Caption styling (big)
CAPTION_FONT_SIZE = 74
CAPTION_LINE_SPACING = 14
CAPTION_PADDING_X = 70
CAPTION_PADDING_Y = 55
CAPTION_BOX_RADIUS = 42

# Top title pill
ENABLE_TOP_TITLE = True
TITLE_FONT_SIZE = 54
TITLE_PAD_TOP = 70

# Transitions
FADE_SECONDS = 0.35  # smooth, no black gaps if we overlap using compose concat

# Encode settings
FPS = 30
FFMPEG_PRESET = "ultrafast"
FFMPEG_THREADS = 2
FFMPEG_PARAMS = ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]

BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE_DIR = BASE / "cache"
HISTORY_FILE = CACHE_DIR / "topics.json"

for d in (IMG_DIR, AUD_DIR, VID_DIR, CACHE_DIR):
    d.mkdir(exist_ok=True)

# ===== REQUIRED SECRET =====
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# =============================
# UTILS
# =============================
def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:60]

def fmt_time(s):
    s = int(max(0, s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def eta_remaining(elapsed, pct):
    if pct <= 0:
        return 0
    total_est = elapsed / pct
    return max(0, total_est - elapsed)

# =============================
# TOPIC HISTORY
# =============================
def load_history():
    if HISTORY_FILE.exists():
        try:
            return set(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_history(h):
    HISTORY_FILE.write_text(json.dumps(sorted(h), ensure_ascii=False, indent=2), encoding="utf-8")

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
    "Why do we blink?",
    "Why does soda fizz?",
    "Why do magnets stick to some metals?",
    "Why do we feel dizzy after spinning?",
    "Why do we get static shocks in winter?",
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
        st.session_state["auto_error"] = "No new topics left. Clear history or expand pool."
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
# SCRIPT (TOPIC SAFE)
# =============================
def script_pool(topic):
    t = (topic or "").lower()

    if "hiccup" in t:
        return [
            "Why do hiccups happen?",
            "Your diaphragm suddenly contracts.",
            "Air rushes in fast.",
            "Your vocal cords snap shut—‘hic!’",
            "Triggers: eating fast, soda, temperature shifts.",
            "Most stop on their own in minutes.",
            "Breath-holding raises CO₂ and may help.",
            "That’s the quick biology of hiccups.",
        ]

    if "fire" in t and "shadow" in t:
        return [
            "Why does fire have no shadow?",
            "A shadow forms when light is blocked.",
            "But fire emits light instead of blocking it.",
            "Flames are glowing gases and hot particles.",
            "That added light fills the dark region.",
            "So the shadow is weak or blurry, not sharp.",
            "Try a bright flashlight behind a flame to force a shadow.",
        ]

    return [
        topic.strip() if topic else "Quick science explanation",
        "This happens due to a simple scientific mechanism.",
        "It depends on how energy moves in the system.",
        "Once you break it down, it becomes intuitive.",
        "Follow for more quick science reels.",
    ]

# =============================
# CAPTION / STYLE
# =============================
def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates += ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    candidates += ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

CAPTION_FONT = load_font(CAPTION_FONT_SIZE, bold=True)
TITLE_FONT = load_font(TITLE_FONT_SIZE, bold=True)

def rounded_rectangle(draw, xy, radius, fill):
    draw.rounded_rectangle(list(xy), radius=radius, fill=fill)

def add_vignette(img):
    overlay = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(overlay)
    d.ellipse([-WIDTH * 0.25, -HEIGHT * 0.15, WIDTH * 1.25, HEIGHT * 1.15], fill=255)
    overlay = overlay.filter(ImageFilter.GaussianBlur(90))
    vignette = ImageOps.invert(overlay)
    return Image.composite(img, Image.new("RGB", img.size, (0, 0, 0)), vignette.point(lambda p: p * 0.35))

def draw_caption(img, caption, topic_title=None):
    img = img.convert("RGB")
    img = add_vignette(img)
    draw = ImageDraw.Draw(img)

    # Top title pill
    if ENABLE_TOP_TITLE and topic_title:
        title_lines = textwrap.wrap(topic_title.strip(), width=24)[:2]
        text = "\n".join(title_lines)
        bbox = draw.multiline_textbbox((0, 0), text, font=TITLE_FONT, spacing=10)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x1 = (WIDTH - tw) // 2 - 30
        y1 = TITLE_PAD_TOP
        x2 = (WIDTH + tw) // 2 + 30
        y2 = y1 + th + 24
        rounded_rectangle(draw, (x1 + 6, y1 + 6, x2 + 6, y2 + 6), 28, fill=(0, 0, 0))
        rounded_rectangle(draw, (x1, y1, x2, y2), 28, fill=(12, 12, 14))
        draw.multiline_text((x1 + 30, y1 + 12), text, font=TITLE_FONT, fill=(255, 255, 255), spacing=10)

    # Bottom caption card
    lines = textwrap.wrap(caption.strip(), width=26)[:3] or [""]
    line_h = CAPTION_FONT_SIZE + CAPTION_LINE_SPACING
    box_h = CAPTION_PADDING_Y * 2 + line_h * len(lines)

    y2 = HEIGHT - 120
    y1 = y2 - box_h
    x1, x2 = 60, WIDTH - 60

    rounded_rectangle(draw, (x1 + 8, y1 + 10, x2 + 8, y2 + 10), CAPTION_BOX_RADIUS, fill=(0, 0, 0))
    rounded_rectangle(draw, (x1, y1, x2, y2), CAPTION_BOX_RADIUS, fill=(12, 12, 14))

    y = y1 + CAPTION_PADDING_Y
    for line in lines:
        draw.text((x1 + CAPTION_PADDING_X, y), line, font=CAPTION_FONT, fill=(255, 255, 255))
        y += line_h

    return img

# =============================
# PEXELS + FALLBACK PLACEHOLDERS
# =============================
def make_placeholder_image(text, out_path, topic_title):
    img = Image.new("RGB", (WIDTH, HEIGHT), (18, 18, 22))
    img = draw_caption(img, text, topic_title=topic_title)
    img.save(out_path, quality=95)
    return out_path

def pexels_images(query):
    headers = {"Authorization": PEXELS_API_KEY, "User-Agent": "Mozilla/5.0"}
    params = {"query": query, "per_page": 20, "orientation": "portrait"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json().get("photos", [])

def prepare_image(url, caption, out_path, topic_title):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
        r.raise_for_status()
        if not r.content:
            raise RuntimeError("Empty image bytes")

        out_path.write_bytes(r.content)
        img = Image.open(out_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        img = draw_caption(img, caption, topic_title=topic_title)
        img.save(out_path, quality=95)
        return out_path
    except Exception:
        return make_placeholder_image(caption, out_path, topic_title)

# =============================
# VIDEO BUILDER (GUARANTEED FRAMES)
# =============================
def build_video(image_paths, audio_path):
    audio = AudioFileClip(str(audio_path))
    dur = float(audio.duration)
    n = max(1, len(image_paths))
    per_img = dur / n

    clips = []
    for p in image_paths:
        c = ImageClip(str(p)).set_duration(per_img)
        # smooth fade (safe in v1)
        fade = min(FADE_SECONDS, per_img / 3)
        c = c.fx(vfx.fadein, fade).fx(vfx.fadeout, fade)
        clips.append(c)

    video = concatenate_videoclips(clips, method="compose").set_audio(audio).set_duration(dur)
    return video

# =============================
# BUILD ONE REEL (WITH % + ETA)
# =============================
def build_reel(topic, idx, progress_cb=None, pexels_delay=0.25):
    start = time.time()

    def cb(p, msg):
        if progress_cb:
            elapsed = time.time() - start
            progress_cb(p, msg, elapsed)

    W_SCRIPT, W_IMAGES, W_RENDER = 0.15, 0.55, 0.30

    cb(0.01, "Generating script + narration...")
    script = script_pool(topic)
    narration = " ".join(script)

    audio_path = AUD_DIR / f"voice_{idx}.mp3"
    gTTS(narration).save(str(audio_path))
    cb(W_SCRIPT, "Narration ready. Fetching images...")

    image_paths = []
    total = len(script)

    for si, scene_text in enumerate(script, start=1):
        cb(W_SCRIPT + W_IMAGES * ((si - 1) / max(1, total)), f"Images {si}/{total}...")

        try:
            photos = pexels_images(scene_text)
        except Exception:
            photos = []

        if not photos:
            for j in range(1, IMAGES_PER_SCENE + 1):
                outp = IMG_DIR / f"reel{idx}_scene{si}_img{j}_placeholder.jpg"
                image_paths.append(make_placeholder_image(scene_text, outp, topic))
            continue

        picks = photos[:IMAGES_PER_SCENE]
        for j, p in enumerate(picks, start=1):
            src = p.get("src", {})
            url = src.get("portrait") or src.get("large2x") or src.get("large")
            outp = IMG_DIR / f"reel{idx}_scene{si}_img{j}_{abs(hash(url))}.jpg"
            image_paths.append(prepare_image(url, scene_text, outp, topic))

        time.sleep(pexels_delay)

    cb(W_SCRIPT + W_IMAGES, "Rendering MP4...")
    video = build_video(image_paths, audio_path)

    out = VID_DIR / f"reel_{idx:02d}_{slugify(topic)}.mp4"
    cb(W_SCRIPT + W_IMAGES + W_RENDER * 0.5, "Encoding...")

    try:
        video.write_videofile(
            str(out),
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset=FFMPEG_PRESET,
            threads=FFMPEG_THREADS,
            ffmpeg_params=FFMPEG_PARAMS,
            temp_audiofile=str(AUD_DIR / f"temp_audio_{idx}.m4a"),
            remove_temp=True,
            logger=None,
        )
    finally:
        try:
            video.close()
        except Exception:
            pass

    cb(1.0, "Done.")
    return out, time.time() - start

# =============================
# UI
# =============================
st.title("YouTube Reel Generator — FIXED MoviePy Imports + Images + Progress")

mode = st.radio("Mode", ["Single", "Batch (20)"], horizontal=True)
pexels_delay = st.slider("Delay between Pexels calls (seconds)", 0.0, 1.5, 0.25)

def show_eta(elapsed, p):
    return fmt_time(eta_remaining(elapsed, p))

if mode == "Single":
    topic = st.text_input("Topic", "Why do hiccups happen?")

    if st.button("Generate Reel"):
        reel_bar = st.progress(0)
        reel_status = st.empty()
        reel_eta = st.empty()

        def cb(p, msg, elapsed):
            reel_bar.progress(int(p * 100))
            reel_status.write(f"{int(p*100)}% — {msg}")
            reel_eta.write(f"ETA: {show_eta(elapsed, p)}")

        mp4, _dt = build_reel(topic, 1, progress_cb=cb, pexels_delay=pexels_delay)
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
                reel_eta.write(f"Reel ETA: {show_eta(elapsed, p)} — {msg}")

            out, dt = build_reel(t, i, progress_cb=cb, pexels_delay=pexels_delay)
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
import json
import os
import random
import re
import textwrap
import time
import zipfile
from pathlib import Path

import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

# MoviePy (Streamlit Cloud is usually v2)
from moviepy import AudioFileClip, ImageClip, CompositeVideoClip, concatenate_videoclips

# Optional vfx (for smoother fades if available)
try:
    from moviepy import vfx  # moviepy v2
except Exception:
    vfx = None

# Ensure ffmpeg works on Streamlit Cloud (requires imageio-ffmpeg in requirements.txt)
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

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

# Caption styling (big + modern)
CAPTION_FONT_SIZE = 74
CAPTION_LINE_SPACING = 14
CAPTION_PADDING_X = 70
CAPTION_PADDING_Y = 55
CAPTION_BOX_RADIUS = 42

# Title pill at top
ENABLE_TOP_TITLE = True
TITLE_FONT_SIZE = 54
TITLE_PAD_TOP = 70

# Transitions (safe)
FADE_SECONDS = 0.35  # gentle fade-in/out per image (safe, avoids black gaps)

# Encode settings (Streamlit Cloud stability)
FPS = 30
FFMPEG_PRESET = "ultrafast"
FFMPEG_THREADS = 2
FFMPEG_PARAMS = ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]

BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
CACHE_DIR = BASE / "cache"
HISTORY_FILE = CACHE_DIR / "topics.json"

for d in (IMG_DIR, AUD_DIR, VID_DIR, CACHE_DIR):
    d.mkdir(exist_ok=True)

# ===== REQUIRED SECRET =====
# Streamlit Cloud -> Settings -> Secrets:
# PEXELS_API_KEY="..."
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# =============================
# MOVIEPY v1/v2 COMPAT
# =============================
def clip_with_duration(c, d):
    return c.with_duration(d) if hasattr(c, "with_duration") else c.set_duration(d)

def clip_with_audio(c, a):
    return c.with_audio(a) if hasattr(c, "with_audio") else c.set_audio(a)

def clip_with_fps(c, fps):
    return c.with_fps(fps) if hasattr(c, "with_fps") else c.set_fps(fps)

# =============================
# UTILS
# =============================
def slugify(t):
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:60]

def fmt_time(s):
    s = int(max(0, s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"

def eta_remaining(elapsed, pct):
    if pct <= 0:
        return 0
    total_est = elapsed / pct
    return max(0, total_est - elapsed)

# =============================
# TOPIC HISTORY
# =============================
def load_history():
    if HISTORY_FILE.exists():
        try:
            return set(json.loads(HISTORY_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_history(h):
    HISTORY_FILE.write_text(json.dumps(sorted(h), ensure_ascii=False, indent=2), encoding="utf-8")

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
    "Why do we blink?",
    "Why does soda fizz?",
    "Why do magnets stick to some metals?",
    "Why do we feel dizzy after spinning?",
    "Why do we get static shocks in winter?",
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
        st.session_state["auto_error"] = "No new topics left. Clear history or expand the pool."
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
# SCRIPT (TOPIC SAFE)
# =============================
def script_pool(topic):
    t = (topic or "").lower()

    if "hiccup" in t:
        return [
            "Why do hiccups happen?",
            "Your diaphragm suddenly contracts.",
            "Air rushes in fast.",
            "Your vocal cords snap shut—‘hic!’",
            "Triggers: eating fast, soda, temperature shifts.",
            "Most stop on their own in minutes.",
            "Breath-holding raises CO₂ and may help.",
            "That’s the quick biology of hiccups.",
        ]

    if "fire" in t and "shadow" in t:
        return [
            "Why does fire have no shadow?",
            "A shadow forms when light is blocked.",
            "But fire emits light instead of blocking it.",
            "Flames are glowing gases and hot particles.",
            "That added light fills the dark region.",
            "So the shadow is weak or blurry, not sharp.",
            "Try a bright flashlight behind a flame to force a shadow.",
        ]

    return [
        topic.strip() if topic else "Quick science explanation",
        "This happens due to a simple scientific mechanism.",
        "It depends on how energy moves in the system.",
        "Once you break it down, it becomes intuitive.",
        "Follow for more quick science reels.",
    ]

# =============================
# STYLE HELPERS
# =============================
def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates += ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    candidates += ["DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

CAPTION_FONT = load_font(CAPTION_FONT_SIZE, bold=True)
TITLE_FONT = load_font(TITLE_FONT_SIZE, bold=True)

def rounded_rectangle(draw, xy, radius, fill):
    draw.rounded_rectangle(list(xy), radius=radius, fill=fill)

def add_vignette(img):
    overlay = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(overlay)
    d.ellipse([-WIDTH * 0.25, -HEIGHT * 0.15, WIDTH * 1.25, HEIGHT * 1.15], fill=255)
    overlay = overlay.filter(ImageFilter.GaussianBlur(90))
    vignette = ImageOps.invert(overlay)
    return Image.composite(img, Image.new("RGB", img.size, (0, 0, 0)), vignette.point(lambda p: p * 0.35))

def draw_caption(img, caption, topic_title=None):
    img = img.convert("RGB")
    img = add_vignette(img)
    draw = ImageDraw.Draw(img)

    # Top title pill
    if ENABLE_TOP_TITLE and topic_title:
        title_lines = textwrap.wrap(topic_title.strip(), width=24)[:2]
        text = "\n".join(title_lines)
        bbox = draw.multiline_textbbox((0, 0), text, font=TITLE_FONT, spacing=10)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x1 = (WIDTH - tw) // 2 - 30
        y1 = TITLE_PAD_TOP
        x2 = (WIDTH + tw) // 2 + 30
        y2 = y1 + th + 24
        rounded_rectangle(draw, (x1 + 6, y1 + 6, x2 + 6, y2 + 6), 28, fill=(0, 0, 0))
        rounded_rectangle(draw, (x1, y1, x2, y2), 28, fill=(12, 12, 14))
        draw.multiline_text((x1 + 30, y1 + 12), text, font=TITLE_FONT, fill=(255, 255, 255), spacing=10)

    # Bottom caption card
    lines = textwrap.wrap(caption.strip(), width=26)[:3] or [""]
    line_h = CAPTION_FONT_SIZE + CAPTION_LINE_SPACING
    box_h = CAPTION_PADDING_Y * 2 + line_h * len(lines)

    y2 = HEIGHT - 120
    y1 = y2 - box_h
    x1, x2 = 60, WIDTH - 60

    rounded_rectangle(draw, (x1 + 8, y1 + 10, x2 + 8, y2 + 10), CAPTION_BOX_RADIUS, fill=(0, 0, 0))
    rounded_rectangle(draw, (x1, y1, x2, y2), CAPTION_BOX_RADIUS, fill=(12, 12, 14))

    y = y1 + CAPTION_PADDING_Y
    for line in lines:
        draw.text((x1 + CAPTION_PADDING_X, y), line, font=CAPTION_FONT, fill=(255, 255, 255))
        y += line_h

    return img

# =============================
# IMAGE FETCH (ROBUST) + PLACEHOLDER
# =============================
def make_placeholder_image(text, out_path, topic_title):
    img = Image.new("RGB", (WIDTH, HEIGHT), (18, 18, 22))
    img = add_vignette(img)
    img = draw_caption(img, text, topic_title=topic_title)
    img.save(out_path, quality=95)
    return out_path

def download_image_bytes(url):
    # Pexels sometimes needs a User-Agent; also handle 403/429 cleanly
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    r = requests.get(url, headers=headers, timeout=25)
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"Image download failed ({r.status_code})")
    return r.content

def pexels_images(query):
    headers = {"Authorization": PEXELS_API_KEY, "User-Agent": "Mozilla/5.0"}
    params = {"query": query, "per_page": 20, "orientation": "portrait"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json().get("photos", [])

def prepare_image(url, caption, out_path, topic_title):
    try:
        out_path.write_bytes(download_image_bytes(url))
        img = Image.open(out_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
        img = draw_caption(img, caption, topic_title=topic_title)
        img.save(out_path, quality=95)
        return out_path
    except Exception:
        # If anything fails (Pexels blocked / 429 / corrupt), fallback so video never goes blank
        return make_placeholder_image(caption, out_path, topic_title=topic_title)

# =============================
# VIDEO BUILDER (NO BLANK VIDEO)
# =============================
def apply_safe_fade(clip, fade_s):
    """
    Adds subtle fades if supported by installed MoviePy.
    If not supported, returns original clip (no crash).
    """
    if vfx is None:
        return clip
    try:
        # moviepy v2 effects
        clip = clip.with_effects([vfx.FadeIn(fade_s), vfx.FadeOut(fade_s)])
        return clip
    except Exception:
        return clip

def build_video(images, audio_path):
    audio = AudioFileClip(str(audio_path))
    dur = float(audio.duration)

    n = max(1, len(images))
    per_img = dur / n

    clips = []
    for img in images:
        c = ImageClip(str(img))
        c = clip_with_duration(c, per_img)
        c = apply_safe_fade(c, min(FADE_SECONDS, per_img / 3))
        clips.append(c)

    # Concatenate is the most reliable way to avoid "black/blank" on Streamlit Cloud
    video = concatenate_videoclips(clips, method="compose")
    video = clip_with_duration(video, dur)
    video = clip_with_audio(video, audio)
    video = clip_with_fps(video, FPS)
    return video

# =============================
# BUILD ONE REEL (WITH % + ETA)
# =============================
def build_reel(topic, idx, progress_cb=None, pexels_delay=0.25):
    start = time.time()

    def cb(p, msg):
        if progress_cb:
            elapsed = time.time() - start
            progress_cb(p, msg, elapsed)

    W_SCRIPT, W_IMAGES, W_RENDER = 0.15, 0.55, 0.30

    cb(0.01, "Generating script + narration...")
    script = script_pool(topic)
    narration = " ".join(script)

    audio_path = AUD_DIR / f"voice_{idx}.mp3"
    gTTS(narration).save(str(audio_path))
    cb(W_SCRIPT, "Narration ready. Fetching images...")

    images = []
    total_scenes = len(script)

    for si, scene_text in enumerate(script, start=1):
        p_scene = (si - 1) / max(1, total_scenes)
        cb(W_SCRIPT + W_IMAGES * p_scene, f"Images {si}/{total_scenes}...")

        photos = pexels_images(scene_text)

        # If API returns nothing, use placeholders (still produces a video with images)
        if not photos:
            for j in range(1, IMAGES_PER_SCENE + 1):
                out_path = IMG_DIR / f"reel{idx}_scene{si}_img{j}_placeholder.jpg"
                images.append(make_placeholder_image(scene_text, out_path, topic_title=topic))
            continue

        picks = photos[:IMAGES_PER_SCENE]
        for j, p in enumerate(picks, start=1):
            src = p.get("src", {})
            url = src.get("portrait") or src.get("large2x") or src.get("large")
            out_path = IMG_DIR / f"reel{idx}_scene{si}_img{j}_{abs(hash(url))}.jpg"
            images.append(prepare_image(url, scene_text, out_path, topic_title=topic))

        time.sleep(pexels_delay)

    cb(W_SCRIPT + W_IMAGES, "Rendering MP4...")
    video = build_video(images, audio_path)

    out = VID_DIR / f"reel_{idx:02d}_{slugify(topic)}.mp4"
    cb(W_SCRIPT + W_IMAGES + W_RENDER * 0.5, "Encoding...")

    try:
        video.write_videofile(
            str(out),
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset=FFMPEG_PRESET,
            threads=FFMPEG_THREADS,
            ffmpeg_params=FFMPEG_PARAMS,
            temp_audiofile=str(AUD_DIR / f"temp_audio_{idx}.m4a"),
            remove_temp=True,
            logger=None,
        )
    finally:
        try:
            video.close()
        except Exception:
            pass

    cb(1.0, "Done.")
    return out, time.time() - start

# =============================
# UI
# =============================
st.title("YouTube Reel Generator — Images + Beautiful Captions + Progress")

mode = st.radio("Mode", ["Single", "Batch (20)"], horizontal=True)

pexels_delay = st.slider("Delay between Pexels calls (seconds)", 0.0, 1.5, 0.25)

def show_eta(elapsed, p):
    return fmt_time(eta_remaining(elapsed, p))

if mode == "Single":
    topic = st.text_input("Topic", "Why do hiccups happen?")

    if st.button("Generate Reel"):
        reel_bar = st.progress(0)
        reel_status = st.empty()
        reel_eta = st.empty()

        def cb(p, msg, elapsed):
            reel_bar.progress(int(p * 100))
            reel_status.write(f"{int(p*100)}% — {msg}")
            reel_eta.write(f"ETA: {show_eta(elapsed, p)}")

        mp4, _dt = build_reel(topic, 1, progress_cb=cb, pexels_delay=pexels_delay)
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
                reel_eta.write(f"Reel ETA: {show_eta(elapsed, p)} — {msg}")

            out, dt = build_reel(t, i, progress_cb=cb, pexels_delay=pexels_delay)
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

