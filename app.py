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

# MoviePy v2 imports (Streamlit Cloud typically uses v2)
from moviepy import AudioFileClip, CompositeVideoClip, ImageClip

# Force a working ffmpeg on Streamlit Cloud (requires imageio-ffmpeg in requirements.txt)
try:
    import imageio_ffmpeg  # pip package: imageio-ffmpeg
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

# Encode settings (reduces ffmpeg IOErrors on Streamlit Cloud)
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

# ====== REQUIRED SECRETS ======
# Streamlit Cloud -> App -> Settings -> Secrets:
# PEXELS_API_KEY="..."
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]

# =============================
# MOVIEPY v1/v2 COMPAT HELPERS
# =============================
def clip_with_duration(c, d):
    return c.with_duration(d) if hasattr(c, "with_duration") else c.set_duration(d)

def clip_with_start(c, t):
    return c.with_start(t) if hasattr(c, "with_start") else c.set_start(t)

def clip_with_audio(c, a):
    return c.with_audio(a) if hasattr(c, "with_audio") else c.set_audio(a)

def clip_with_fps(c, fps):
    # v2 has with_fps; v1 uses set_fps
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
# TOPIC HISTORY (NO DUPES)
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
        st.session_state["auto_error"] = "No new topics left. Clear topic history or expand the pool."
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
# CAPTION/STYLE HELPERS
# =============================
def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates += [
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    candidates += [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
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
        title = topic_title.strip()
        title_lines = textwrap.wrap(title, width=24)[:2]
        text = "\n".join(title_lines)

        bbox = draw.multiline_textbbox((0, 0), text, font=TITLE_FONT, spacing=10)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

        x1 = (WIDTH - tw) // 2 - 30
        y1 = TITLE_PAD_TOP
        x2 = (WIDTH + tw) // 2 + 30
        y2 = y1 + th + 24

        # shadow + pill
        rounded_rectangle(draw, (x1 + 6, y1 + 6, x2 + 6, y2 + 6), 28, fill=(0, 0, 0))
        rounded_rectangle(draw, (x1, y1, x2, y2), 28, fill=(12, 12, 14))
        draw.multiline_text((x1 + 30, y1 + 12), text, font=TITLE_FONT, fill=(255, 255, 255), spacing=10)

    # Bottom caption card
    lines = textwrap.wrap(caption.strip(), width=26)[:3]
    if not lines:
        lines = [""]

    line_h = CAPTION_FONT_SIZE + CAPTION_LINE_SPACING
    box_h = CAPTION_PADDING_Y * 2 + line_h * len(lines)

    y2 = HEIGHT - 120
    y1 = y2 - box_h

    x1 = 60
    x2 = WIDTH - 60

    # shadow + card
    rounded_rectangle(draw, (x1 + 8, y1 + 10, x2 + 8, y2 + 10), CAPTION_BOX_RADIUS, fill=(0, 0, 0))
    rounded_rectangle(draw, (x1, y1, x2, y2), CAPTION_BOX_RADIUS, fill=(12, 12, 14))

    # text
    y = y1 + CAPTION_PADDING_Y
    for line in lines:
        draw.text((x1 + CAPTION_PADDING_X, y), line, font=CAPTION_FONT, fill=(255, 255, 255))
        y += line_h

    return img

# =============================
# PEXELS
# =============================
def pexels_images(query):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 20, "orientation": "portrait"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)
    r.raise_for_status()
    return r.json().get("photos", [])

def prepare_image(url, caption, out_path, topic_title):
    out_path.write_bytes(requests.get(url, timeout=25).content)
    img = Image.open(out_path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

    img = draw_caption(img, caption, topic_title=topic_title)

    img.save(out_path, quality=95)
    return out_path

# =============================
# VIDEO BUILDER (SYNCED, STABLE)
# =============================
def build_video(images, audio_path, crossfade=0.6):
    audio = AudioFileClip(str(audio_path))
    dur = float(audio.duration)

    n = max(1, len(images))
    overlap = max(0.2, min(crossfade, 1.2))

    # Ensure total duration matches audio:
    # total = n*D - (n-1)*overlap = dur  => D = (dur + (n-1)*overlap)/n
    D = (dur + (n - 1) * overlap) / n
    step = D - overlap

    clips = []
    for i, img in enumerate(images):
        c = ImageClip(str(img))
        c = clip_with_duration(c, D)

        start_t = 0.0 if i == 0 else i * step
        c = clip_with_start(c, start_t)

        # Note: true crossfade requires masks/effects which are inconsistent across MoviePy versions.
        # This overlap layout is stable and avoids black frames.
        clips.append(c)

    video = CompositeVideoClip(clips, size=(WIDTH, HEIGHT))
    video = clip_with_duration(video, dur)
    video = clip_with_audio(video, audio)
    video = clip_with_fps(video, FPS)
    return video

# =============================
# BUILD ONE REEL (WITH % + ETA)
# =============================
def build_reel(topic, idx, progress_cb=None, pexels_delay=0.25, crossfade=0.6):
    start = time.time()

    def cb(p, msg):
        if progress_cb:
            elapsed = time.time() - start
            progress_cb(p, msg, elapsed)

    W_SCRIPT = 0.15
    W_IMAGES = 0.55
    W_RENDER = 0.30

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
        if not photos:
            raise RuntimeError(f"Pexels returned 0 images for: {scene_text}")

        picks = photos[:IMAGES_PER_SCENE]
        for j, p in enumerate(picks, start=1):
            url = p["src"].get("portrait") or p["src"].get("large2x") or p["src"].get("large")
            out_path = IMG_DIR / f"reel{idx}_scene{si}_img{j}_{abs(hash(url))}.jpg"
            prepare_image(url, scene_text, out_path, topic_title=topic)
            images.append(out_path)

        time.sleep(pexels_delay)

    cb(W_SCRIPT + W_IMAGES, "Rendering MP4...")
    video = build_video(images, audio_path, crossfade=crossfade)

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
        # Close to prevent ffmpeg broken pipe / resource issues
        try:
            video.close()
        except Exception:
            pass

    cb(1.0, "Done.")
    return out, time.time() - start

# =============================
# UI
# =============================
st.title("YouTube Reel Generator — Beautiful Captions + Progress + Batch")

mode = st.radio("Mode", ["Single", "Batch (20)"], horizontal=True)

pexels_delay = st.slider("Delay between Pexels calls (seconds)", 0.0, 1.5, 0.25)
crossfade_seconds = st.slider("Smooth transition overlap (seconds)", 0.2, 1.2, 0.6)

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

        try:
            mp4, _dt = build_reel(topic, 1, progress_cb=cb, pexels_delay=pexels_delay, crossfade=crossfade_seconds)
            st.success("Done.")
            st.video(str(mp4))
            st.download_button("Download MP4", open(mp4, "rb"), mp4.name, mime="video/mp4")
        except OSError:
            st.error(
                "FFmpeg failed while writing the video. Fix this by adding "
                "`imageio-ffmpeg` to requirements.txt and rebooting the app."
            )
            st.stop()

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

            try:
                out, dt = build_reel(t, i, progress_cb=cb, pexels_delay=pexels_delay, crossfade=crossfade_seconds)
                outputs.append(out)
                times.append(dt)
            except OSError:
                st.error("FFmpeg failed mid-batch. Add `imageio-ffmpeg` to requirements.txt and reboot.")
                st.stop()

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
