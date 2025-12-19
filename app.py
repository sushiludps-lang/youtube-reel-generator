import os
import re
import time
import textwrap
from pathlib import Path

import requests
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance

# ✅ Cloud-safe MoviePy v1 import
from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    concatenate_videoclips,
    vfx,
)

# Ensure ffmpeg exists (Streamlit Cloud)
try:
    import imageio_ffmpeg
    os.environ["IMAGEIO_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    pass

# =============================
# CONFIG
# =============================
WIDTH, HEIGHT = 1080, 1920
FPS = 30

IMAGES_PER_SCENE = 2            # ✅ required
CROSSFADE_SECONDS = 0.5         # ✅ smooth transition, no black gaps

# Caption style
CAPTION_FONT_SIZE = 72
TITLE_FONT_SIZE = 52
BOTTOM_MARGIN = 120

BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"

for d in (IMG_DIR, AUD_DIR, VID_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]  # required on Streamlit Cloud

# =============================
# HELPERS
# =============================
def slugify(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (t or "").lower()).strip("_")[:70] or "reel"

def load_font(size, bold=False):
    # Works on Streamlit Cloud
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

FONT_CAPTION = load_font(CAPTION_FONT_SIZE, bold=True)
FONT_TITLE = load_font(TITLE_FONT_SIZE, bold=True)

def words_count(s: str) -> int:
    return len(re.findall(r"\b\w+\b", s or ""))

def estimate_seconds_for_words(w: int, wpm: int = 155) -> float:
    # gTTS typically around 140–170 wpm, use 155 as default
    return (w / wpm) * 60.0

def split_sentences(topic: str):
    t = (topic or "").lower()

    if "fire" in t and "shadow" in t:
        return [
            "Why does fire have no shadow?",
            "A shadow forms when an object blocks light.",
            "But fire is not a solid object—it emits light.",
            "Flames are hot glowing gases plus tiny glowing particles.",
            "That extra light fills the dark region where a shadow would appear.",
            "So any shadow becomes weak, blurry, or disappears.",
            "Try a bright flashlight behind a candle to force a faint shadow.",
            "That’s why fire usually looks like it has no shadow.",
        ]

    if "hiccup" in t:
        return [
            "Why do hiccups happen?",
            "Your diaphragm suddenly contracts.",
            "Air rushes in fast.",
            "Your vocal cords snap shut—hic!",
            "Triggers include eating fast, soda, or temperature changes.",
            "Most hiccups stop on their own in minutes.",
            "Holding your breath can raise CO2 and sometimes helps.",
        ]

    return [
        topic.strip() or "Quick science explanation",
        "Here’s what’s happening in simple terms.",
        "It comes down to how energy moves through the system.",
        "Once you see the mechanism, it becomes intuitive.",
        "Follow for more quick science reels.",
    ]

def expand_script_to_target(script_lines, target_seconds: int) -> list:
    """
    Ensures reels don’t become 10–15s:
    We expand the *spoken* script (not silence) until it hits target duration.
    """
    topic = script_lines[0] if script_lines else "Topic"
    base = " ".join(script_lines)
    w = words_count(base)
    est = estimate_seconds_for_words(w)

    fillers = [
        "Here’s a simple way to test it at home.",
        "If you change the background light, the effect becomes more obvious.",
        "The key idea is that brightness can hide shadows.",
        "This happens because the flame adds light in many directions.",
        "That’s why the shadow, if any, looks soft rather than sharp.",
        "One more detail: a shadow needs a strong, single-direction light source.",
        "When light is scattered or added, shadows wash out.",
    ]

    i = 0
    out = list(script_lines)
    while est < target_seconds - 2 and i < 30:
        out.append(fillers[i % len(fillers)])
        w = words_count(" ".join(out))
        est = estimate_seconds_for_words(w)
        i += 1

    # Optional CTA at end
    if out and not re.search(r"\bfollow\b|\bsubscribe\b", out[-1].lower()):
        out.append("Follow for more 60-second science.")
    return out

def pexels_search(query: str):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": query, "per_page": 20, "orientation": "portrait"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("photos", [])

def fetch_image(url: str, out_path: Path) -> Path:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return out_path

def improve_background(img: Image.Image) -> Image.Image:
    # Prevent “too dark / dull” look
    img = img.convert("RGB")
    img = ImageEnhance.Brightness(img).enhance(1.08)
    img = ImageEnhance.Contrast(img).enhance(1.10)
    return img

def draw_caption_on_image(img: Image.Image, caption: str, title: str) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = img.resize((WIDTH, HEIGHT))
    img = improve_background(img)

    draw = ImageDraw.Draw(img)

    # Top title pill
    title = (title or "").strip()
    if title:
        title_lines = textwrap.wrap(title, width=26)[:2]
        title_text = "\n".join(title_lines)
        tb = draw.multiline_textbbox((0, 0), title_text, font=FONT_TITLE, spacing=8)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        x1 = (WIDTH - tw) // 2 - 26
        y1 = 50
        x2 = (WIDTH + tw) // 2 + 26
        y2 = y1 + th + 24
        draw.rounded_rectangle((x1, y1, x2, y2), radius=26, fill=(10, 10, 12))
        draw.multiline_text((x1 + 26, y1 + 10), title_text, font=FONT_TITLE, fill="white", spacing=8)

    # Bottom caption card (big)
    lines = textwrap.wrap((caption or "").strip(), width=26)[:3] or [""]
    line_h = CAPTION_FONT_SIZE + 12
    box_h = 60 + len(lines) * line_h

    y2 = HEIGHT - BOTTOM_MARGIN
    y1 = y2 - box_h
    x1, x2 = 70, WIDTH - 70

    draw.rounded_rectangle((x1, y1, x2, y2), radius=42, fill=(10, 10, 12))
    y = y1 + 30
    for line in lines:
        draw.text((x1 + 50, y), line, font=FONT_CAPTION, fill="white")
        y += line_h

    return img

def make_placeholder(caption: str, title: str, out: Path) -> Path:
    img = Image.new("RGB", (WIDTH, HEIGHT), (22, 22, 26))
    img = draw_caption_on_image(img, caption, title)
    img.save(out, quality=95)
    return out

def get_scene_images(scene_text: str, title: str, reel_id: str, scene_i: int) -> list[Path]:
    """
    Returns exactly IMAGES_PER_SCENE images.
    If Pexels fails or returns empty, returns placeholders.
    """
    paths = []
    try:
        photos = pexels_search(scene_text)
    except Exception:
        photos = []

    picks = photos[:IMAGES_PER_SCENE] if photos else []
    for j in range(1, IMAGES_PER_SCENE + 1):
        out = IMG_DIR / f"{reel_id}_s{scene_i:02d}_i{j:02d}.jpg"
        if j <= len(picks):
            src = picks[j - 1].get("src", {})
            url = src.get("portrait") or src.get("large2x") or src.get("large")
            if url:
                try:
                    fetch_image(url, out)
                    img = Image.open(out)
                    img = draw_caption_on_image(img, scene_text, title)
                    img.save(out, quality=95)
                    paths.append(out)
                    continue
                except Exception:
                    pass
        paths.append(make_placeholder(scene_text, title, out))
    return paths

def concatenate_with_crossfade(clips, d: float):
    """
    Smooth transition WITHOUT black gaps:
    - apply crossfadein on each clip (except first)
    - concatenate with negative padding (overlap)
    """
    if not clips:
        raise ValueError("No clips to concatenate.")

    d = max(0.0, float(d))
    if d <= 0:
        return concatenate_videoclips(clips, method="compose")

    out = [clips[0]]
    for c in clips[1:]:
        out.append(c.crossfadein(d))

    # padding=-d overlaps clips by d seconds -> smooth dissolve
    return concatenate_videoclips(out, method="compose", padding=-d)

def build_video_from_images_and_audio(image_paths: list[Path], audio_path: Path, crossfade: float) -> Path:
    audio = AudioFileClip(str(audio_path))
    total_dur = float(audio.duration)

    n = max(1, len(image_paths))
    per_img = total_dur / n

    clips = []
    for p in image_paths:
        c = ImageClip(str(p)).set_duration(per_img)
        clips.append(c)

    video = concatenate_with_crossfade(clips, d=min(crossfade, per_img * 0.45))
    video = video.set_audio(audio)

    out = VID_DIR / f"reel_{int(time.time())}.mp4"
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

    try:
        video.close()
        audio.close()
    except Exception:
        pass

    return out

def build_reel(topic: str, target_seconds: int, pexels_delay: float = 0.2) -> Path:
    # 1) Script
    base_script = split_sentences(topic)
    script = expand_script_to_target(base_script, target_seconds=target_seconds)

    # 2) Voiceover
    narration = " ".join(script)
    audio_path = AUD_DIR / f"voice_{int(time.time())}.mp3"
    gTTS(narration).save(str(audio_path))

    # 3) Images (2 per scene)
    reel_id = slugify(topic) + "_" + str(int(time.time()))
    all_imgs: list[Path] = []
    for i, scene_text in enumerate(script, start=1):
        all_imgs.extend(get_scene_images(scene_text, topic, reel_id, i))
        time.sleep(pexels_delay)

    # 4) Video (duration == narration duration; transitions crossfade)
    return build_video_from_images_and_audio(all_imgs, audio_path, crossfade=CROSSFADE_SECONDS)

# =============================
# UI
# =============================
st.title("YouTube Reel Generator (Transitions + Captions + Correct Duration)")

topic = st.text_input("Topic", value="Why does fire have no shadow?")
target_seconds = st.slider("Target reel length (seconds)", 15, 75, 60, 5)
pexels_delay = st.slider("Delay between Pexels calls (seconds)", 0.0, 1.5, 0.2, 0.1)
crossfade = st.slider("Transition smoothness (crossfade seconds)", 0.0, 1.0, 0.5, 0.05)
CROSSFADE_SECONDS = crossfade  # apply user setting

if st.button("Generate Reel"):
    with st.spinner("Generating script, images, audio, and video..."):
        mp4 = build_reel(topic, target_seconds=target_seconds, pexels_delay=pexels_delay)
    st.success("Done!")
    st.video(str(mp4))
    st.download_button("Download MP4", open(mp4, "rb"), file_name=Path(mp4).name, mime="video/mp4")
