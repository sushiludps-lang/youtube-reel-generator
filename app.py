import textwrap
import requests
from pathlib import Path

import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

from moviepy import ImageClip, AudioFileClip, CompositeVideoClip

try:
    from moviepy import vfx
except Exception:
    vfx = None

# ===============================
# CONFIG / FOLDERS
# ===============================
WIDTH, HEIGHT = 1080, 1920
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
for d in (IMG_DIR, AUD_DIR, VID_DIR):
    d.mkdir(exist_ok=True)

PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]
IMAGES_PER_SCENE = 2  # fixed

# ===============================
# UI
# ===============================
st.title("YouTube Reel Generator — True Crossfade + Synced to Narration")

topic = st.text_input("Topic", "Why does fire have no shadow?")

target_scene_seconds = st.slider("Target seconds per scene", 4, 12, 7)
min_video_seconds = st.slider("Minimum video length (seconds)", 10, 60, 30)
min_scenes = st.slider("Minimum scenes", 2, 12, 6)
max_scenes = st.slider("Maximum scenes", 3, 20, 10)

ENABLE_CAPTIONS = st.toggle("Burn captions on images", True)
CAPTION_FONT_SIZE = st.slider("Caption font size", 42, 84, 64)
CAPTION_BOX_OPACITY = st.slider("Caption box opacity", 80, 220, 160)

crossfade_seconds = st.slider("Crossfade seconds", 0.2, 1.2, 0.6)
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
# PEXELS FETCH
# ===============================
def fetch_images(scene_text):
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": scene_text, "per_page": 30, "orientation": "portrait", "size": "large"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=20)
    r.raise_for_status()
    photos = r.json().get("photos", [])

    paths = []
    for i, p in enumerate(photos[:IMAGES_PER_SCENE]):
        url = p["src"].get("portrait") or p["src"].get("large")
        if not url:
            continue

        img_path = IMG_DIR / f"img_{abs(hash((scene_text, i)))}.jpg"
        img_path.write_bytes(requests.get(url, timeout=20).content)

        img = Image.open(img_path).convert("RGB")
        img = ImageOps.exif_transpose(img)
        img = img.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)

        if ENABLE_CAPTIONS:
            img = burn_caption(img, scene_text)

        img.save(img_path, quality=95)
        paths.append(img_path)

    if not paths:
        return []
    while len(paths) < IMAGES_PER_SCENE:
        paths.append(paths[-1])
    return paths[:IMAGES_PER_SCENE]

# ===============================
# SCRIPT POOL
# ===============================
def script_pool(topic: str):
    return [
        f"{topic} — quick answer.",
        "A shadow needs a strong background light and something that blocks it.",
        "Fire isn’t a solid object; it’s hot gas plus glowing soot particles.",
        "Because fire emits light, it can ‘fill in’ the dark area you expect.",
        "Flames are also partly transparent, so they don’t block all light strongly.",
        "That’s why a candle often won’t cast a sharp shadow on a wall.",
        "But a brighter light behind the flame can force a visible shadow.",
        "Try it: flashlight behind a lighter, then look at the wall edge.",
        "If the flame is dim and the background light is strong, shadow becomes clearer.",
        "If the flame is bright, it washes out the shadow contrast.",
        "Moving flames blur edges, which makes shadows look weak.",
        "So it’s not ‘zero shadow’—it’s usually ‘no crisp shadow’ in normal lighting.",
        "You can tune light intensity to make it appear or disappear.",
        "That’s the physics: emission + transparency + contrast.",
        "Follow for more quick science reels.",
    ]

# ===============================
# BUILD SCRIPT UNTIL LONG ENOUGH
# ===============================
def tts_duration_for(script_lines):
    narration = " ".join(script_lines)
    tmp_mp3 = AUD_DIR / "tmp_voice.mp3"
    gTTS(narration).save(str(tmp_mp3))
    a = AudioFileClip(str(tmp_mp3))
    return tmp_mp3, a.duration

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
# TRUE CROSSFADE (SYNCED TO AUDIO)
# ===============================
def build_crossfade_video_synced(image_paths, audio_duration, xfade):
    n = len(image_paths)
    if n == 0:
        raise ValueError("No images to build video.")

    # Ensure crossfade isn't too big
    xfade = max(0.0, min(xfade, 2.0))

    # Compute per-image duration so that FINAL video duration == audio_duration
    # final = n*D - (n-1)*xfade  =>  D = (audio + (n-1)*xfade)/n
    D = (audio_duration + (n - 1) * xfade) / n
    # Safety clamp: if D becomes too small, reduce xfade
    if D <= xfade * 1.1:
        xfade = D * 0.3
        D = (audio_duration + (n - 1) * xfade) / n

    step = D - xfade  # start distance between clips

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
    return video, D, xfade, step

# ===============================
# BUILD VIDEO
# ===============================
if st.button("Generate Final MP4 Reel"):
    script, voice_path, audio_duration = build_script(
        topic, target_scene_seconds, min_video_seconds, min_scenes, max_scenes
    )

    audio = AudioFileClip(str(voice_path))

    # Collect 2 images per scene
    image_paths = []
    used = 0
    for scene_text in script:
        imgs = fetch_images(scene_text)
        if not imgs:
            st.error("No images fetched from Pexels. Try a different topic or verify PEXELS_API_KEY.")
            st.stop()
        image_paths.extend(imgs)
        used += len(imgs)

    video, per_img_D, xfade_used, step_used = build_crossfade_video_synced(
        image_paths, audio.duration, crossfade_seconds
    )

    video = video.with_audio(audio) if hasattr(video, "with_audio") else video.set_audio(audio)

    out = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out), fps=30, codec="libx264", audio_codec="aac")

    if DEBUG:
        st.write("Audio duration (s):", round(audio.duration, 2))
        st.write("Scenes:", len(script))
        st.write("Images:", len(image_paths))
        st.write("Per-image duration D (s):", round(per_img_D, 3))
        st.write("Crossfade used (s):", round(xfade_used, 3))
        st.write("Step (s):", round(step_used, 3))
        st.write("Video duration (s):", round(video.duration, 2))

    st.success(f"Done. Length: {video.duration:.1f}s • Scenes: {len(script)} • Images: {used}")
    st.video(str(out))
    st.download_button("Download MP4", open(out, "rb"), "reel.mp4", mime="video/mp4")
