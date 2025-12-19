import json
import random
from io import BytesIO
from pathlib import Path

import requests
import streamlit as st
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from PIL import Image, ImageDraw, ImageFont, ImageOps

# --- FIX for MoviePy on new Pillow (Image.ANTIALIAS removed) ---
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

from gtts import gTTS

# -------------------------------------------------
# MoviePy import (Cloud + Local compatible)
# -------------------------------------------------
try:
    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips, vfx
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips, vfx
    MOVIEPY_V2 = False

# -----------------------
# Paths
# -----------------------
BASE = Path(__file__).parent
IMG_DIR = BASE / "images"
AUD_DIR = BASE / "audio"
VID_DIR = BASE / "video"
IMG_DIR.mkdir(exist_ok=True)
AUD_DIR.mkdir(exist_ok=True)
VID_DIR.mkdir(exist_ok=True)

# -----------------------
# Page setup
# -----------------------
st.set_page_config(page_title="Reel Generator", layout="wide", page_icon="🎬")
st.title("YouTube Reel Generator – MP4 Builder (Free Images + Transitions)")

# -----------------------
# Secrets
# -----------------------
gemini_key = st.secrets.get("GEMINI_API_KEY", "")
PEXELS_KEY = st.secrets.get("PEXELS_API_KEY", "")

# -----------------------
# UI inputs
# -----------------------
topic = st.text_input("Topic", value="Why does fire have no shadow?")
num_scenes = st.slider("Scenes (script sections)", 5, 10, 7)

target_seconds = st.slider("Target reel length (seconds)", 40, 75, 60)
imgs_per_scene = st.slider("Images per scene", 2, 10, 7)

transition_sec = st.slider("Transition (crossfade) seconds", 0.0, 1.5, 0.5, 0.1)
enable_kenburns = st.toggle("Enable subtle zoom (Ken Burns)", value=True)
kenburns_zoom = st.slider("Zoom strength", 1.00, 1.10, 1.04, 0.01)

image_source = st.selectbox("Image source", ["Pexels (free key)", "Placeholders only"], index=0)
show_debug = st.toggle("Show debug (errors, URLs)", value=True)

if image_source.startswith("Pexels") and not PEXELS_KEY:
    st.warning("PEXELS_API_KEY is missing. Add it in Streamlit Cloud → Manage app → Settings → Secrets.")

# -----------------------
# Helpers
# -----------------------
def _load_font(size: int = 64):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()

def make_placeholder_image(text: str, idx: int) -> Path:
    img = Image.new("RGB", (1080, 1920), (15, 15, 20))
    draw = ImageDraw.Draw(img)
    font = _load_font(64)
    draw.text((80, 120), f"Clip {idx}", fill=(200, 200, 200), font=font)

    words = text.split()
    lines, line = [], ""
    for w in words:
        if len((line + " " + w).strip()) <= 26:
            line = (line + " " + w).strip()
        else:
            lines.append(line)
            line = w
    if line:
        lines.append(line)

    y = 520
    for ln in lines[:9]:
        draw.text((80, y), ln, fill=(240, 240, 240), font=font)
        y += 90

    out = IMG_DIR / f"clip_{idx}.png"
    img.save(out)
    return out

@st.cache_data(show_spinner=False, ttl=3600)
def pexels_search_urls(query: str, k: int):
    if not PEXELS_KEY:
        return [], None, "PEXELS_API_KEY missing"

    headers = {"Authorization": PEXELS_KEY}
    params = {"query": query, "per_page": 80, "orientation": "portrait", "size": "large"}
    r = requests.get("https://api.pexels.com/v1/search", headers=headers, params=params, timeout=25)
    if r.status_code != 200:
        return [], r.status_code, r.text[:500]

    data = r.json()
    photos = data.get("photos", [])
    if not photos:
        return [], 200, "No photos found"

    urls = []
    for p in photos:
        src = p.get("src", {})
        if src.get("portrait"):
            urls.append(src["portrait"])
        elif src.get("large"):
            urls.append(src["large"])

    urls = list(dict.fromkeys(urls))
    random.shuffle(urls)
    return urls[:k], 200, "OK"

def download_and_fit_9x16(img_url: str, out_path: Path):
    r = requests.get(img_url, timeout=30)
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    img = ImageOps.exif_transpose(img)

    target_w, target_h = 1080, 1920
    w, h = img.size
    target_ratio = target_w / target_h
    src_ratio = w / h

    if src_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
    img.save(out_path, format="PNG")

def build_images_for_scene(scene_text: str, clip_start_idx: int, k: int):
    paths = []
    if image_source.startswith("Pexels"):
        urls, status, msg = pexels_search_urls(scene_text, k)
        if show_debug:
            st.caption(f"Pexels: status={status} msg={msg} (scene '{scene_text[:40]}...')")

        for j, url in enumerate(urls, start=0):
            out = IMG_DIR / f"clip_{clip_start_idx + j}.png"
            try:
                download_and_fit_9x16(url, out)
                paths.append(out)
            except Exception as e:
                if show_debug:
                    st.write("Image download error:", str(e))
                continue

    while len(paths) < k:
        idx = clip_start_idx + len(paths)
        paths.append(make_placeholder_image(scene_text, idx))

    return paths

def ken_burns(clip: ImageClip, zoom=1.04):
    return clip.fx(vfx.resize, lambda t: 1 + (zoom - 1) * (t / clip.duration))

def fallback_script(topic: str, n: int):
    # Always valid JSON-like structure (no Gemini needed)
    hook = f"{topic} — here’s the quick reason."
    scenes = [
        "A shadow forms when one strong, single light source is blocked.",
        "Fire is not a solid object; it’s glowing hot gas that emits light.",
        "Because the flame itself is a light source, it fills in its own shadow.",
        "Also, flames are partially transparent, so they don’t block all light.",
        "You can see a shadow only if a much brighter light is behind the flame.",
        "That’s why candle flames rarely cast a clear shadow in normal rooms.",
        "Try it: shine a phone flashlight behind a lighter and look on the wall."
    ]
    scenes = scenes[:max(5, min(n, len(scenes)))]
    cta = "Follow for more 60-second science."
    return {"hook": hook, "scenes": scenes, "cta": cta}

def generate_script_with_gemini(topic: str, n: int, target_sec: int):
    if not gemini_key:
        return None, "Missing GEMINI_API_KEY"

    client = genai.Client(api_key=gemini_key)

    prompt = f"""
Return ONLY valid JSON. No commentary. No markdown. No extra text.

Topic: {topic}
Total target length: ~{target_sec} seconds of narration (aim 120–150 words max).
Scenes: {n}

Rules:
- Hook: 1 sentence (<= 12 words)
- Each scene: 1 short sentence (<= 18 words)
- CTA: 1 sentence (<= 10 words)

Format EXACTLY:
{{
  "hook": "short hook",
  "scenes": ["scene text", "..."],
  "cta": "short CTA"
}}
""".strip()

    # IMPORTANT: google.genai usually expects model names WITHOUT "models/"
    model_candidates = ["gemini-2.5-flash", "models/gemini-2.5-flash"]

    last_err = None
    for m in model_candidates:
        try:
            resp = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            raw = (getattr(resp, "text", "") or "").strip().replace("```json", "").replace("```", "").strip()
            data = json.loads(raw)
            return data, None
        except (ClientError, json.JSONDecodeError) as e:
            last_err = f"Model {m} failed: {type(e).__name__}: {str(e)[:400]}"
            continue
        except Exception as e:
            last_err = f"Model {m} failed: {type(e).__name__}: {str(e)[:400]}"
            continue

    return None, last_err or "Unknown Gemini error"

# -----------------------
# Generate MP4
# -----------------------
if st.button("Generate Final MP4 Reel", type="primary"):

    with st.spinner("Generating script..."):
        data, err = generate_script_with_gemini(topic, num_scenes, target_seconds)

    if err:
        st.warning("Gemini script generation failed. Using free fallback script.")
        if show_debug:
            st.code(err)
        data = fallback_script(topic, num_scenes)

    scenes = data.get("scenes", [])
    if not isinstance(scenes, list) or not scenes:
        st.error("No scenes returned; cannot build video.")
        st.stop()

    hook = data.get("hook", "")
    cta = data.get("cta", "")

    st.subheader("Script")
    st.write("**Hook:**", hook)
    for i, s in enumerate(scenes, 1):
        st.write(f"**Scene {i}:**", s)
    st.write("**CTA:**", cta)

    narration_parts = [hook] + [str(s) for s in scenes] + [cta]
    narration = ". ".join([p.strip() for p in narration_parts if p and p.strip()]) + "."

    with st.spinner("Generating voiceover..."):
        audio_path = AUD_DIR / "voiceover.mp3"
        gTTS(narration, lang="en", slow=False).save(str(audio_path))
        st.audio(str(audio_path))

    audio = AudioFileClip(str(audio_path))

    with st.spinner("Fetching images..."):
        all_image_paths = []
        clip_idx = 1
        for s in scenes:
            paths = build_images_for_scene(str(s), clip_idx, imgs_per_scene)
            all_image_paths.extend(paths)
            clip_idx += imgs_per_scene

    per_img = max(0.5, audio.duration / max(1, len(all_image_paths)))

    with st.spinner("Rendering video..."):
        clips = []
        for idx, p in enumerate(all_image_paths):
            if MOVIEPY_V2:
                c = ImageClip(str(p), duration=per_img)
            else:
                c = ImageClip(str(p)).set_duration(per_img)

            if enable_kenburns and kenburns_zoom > 1.0:
                c = ken_burns(c, zoom=kenburns_zoom)

            if transition_sec > 0 and idx > 0:
                if MOVIEPY_V2:
                    c = c.with_effects([vfx.CrossFadeIn(transition_sec)])
                else:
                    c = c.crossfadein(transition_sec)

            clips.append(c)

        if MOVIEPY_V2:
            video = concatenate_videoclips(clips, method="compose").with_audio(audio)
        else:
            video = concatenate_videoclips(clips, method="compose").set_audio(audio)

        out_video = VID_DIR / "final_reel.mp4"
        video.write_videofile(str(out_video), fps=30, codec="libx264", audio_codec="aac")

    st.success(
        f"Final MP4 ready • Images: {len(all_image_paths)} • Audio: {audio.duration:.1f}s • Per image: {per_img:.2f}s"
    )
    st.video(str(out_video))
    st.download_button("Download MP4", data=open(out_video, "rb"), file_name="final_reel.mp4", mime="video/mp4")
