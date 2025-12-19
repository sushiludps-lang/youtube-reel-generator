import json
import base64
from pathlib import Path

import streamlit as st
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS

# -------------------------------------------------
# MoviePy import (Cloud + Local compatible)
# -------------------------------------------------
try:
    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
    MOVIEPY_V2 = True
except ImportError:
    from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
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
st.title("YouTube Reel Generator – Full MP4 Builder (AI Images)")

# -----------------------
# Secrets + GenAI client
# -----------------------
api_key = st.secrets.get("GEMINI_API_KEY", "")
if not api_key:
    st.error("Missing GEMINI_API_KEY. Add it in Streamlit Cloud → Manage app → Settings → Secrets.")
    st.stop()

client = genai.Client(api_key=api_key)

TEXT_MODEL = "gemini-2.5-flash"

# Try multiple image models (your earlier model list included these)
IMAGE_MODELS_TO_TRY = [
    "gemini-2.5-flash-image",
    "gemini-2.5-flash-image-preview",
    "gemini-2.0-flash-exp-image-generation",
]

# -----------------------
# UI inputs
# -----------------------
topic = st.text_input("Enter a topic for the YouTube Reel", value="Why does fire have no shadow?")
num_scenes = st.slider("Number of scenes", 5, 10, 7)

use_ai_images = st.toggle("Use AI-generated images", value=True)
show_image_debug = st.toggle("Show AI image debug (recommended)", value=True)

image_style = st.selectbox(
    "Image style",
    ["cinematic", "photorealistic", "minimal infographic", "3D render", "anime"],
    index=0
)

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

    draw.text((80, 120), f"Scene {idx}", fill=(200, 200, 200), font=font)

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

    y = 500
    for ln in lines[:8]:
        draw.text((80, y), ln, fill=(240, 240, 240), font=font)
        y += 90

    out = IMG_DIR / f"scene_{idx}.png"
    img.save(out)
    return out

def _extract_first_image_bytes(resp) -> bytes | None:
    try:
        parts = resp.candidates[0].content.parts
    except Exception:
        return None

    for part in parts:
        inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
        if not inline:
            continue
        data = getattr(inline, "data", None)
        if not data:
            continue
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        if isinstance(data, str):
            return base64.b64decode(data)
    return None

def try_generate_ai_image(scene_text: str, idx: int):
    """
    Returns (img_path_or_none, debug_lines)
    """
    prompt = (
        f"Create a single vertical 9:16 image for a YouTube Shorts reel.\n"
        f"Style: {image_style}.\n"
        f"Scene: {scene_text}\n"
        f"Do not add any text overlays or subtitles."
    )

    debug = []
    for model_name in IMAGE_MODELS_TO_TRY:
        try:
            debug.append(f"Trying model: {model_name}")
            resp = client.models.generate_content(
                model=model_name,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    image_config=types.ImageConfig(aspect_ratio="9:16")
                ),
            )
            img_bytes = _extract_first_image_bytes(resp)
            if img_bytes:
                out = IMG_DIR / f"scene_{idx}.png"
                out.write_bytes(img_bytes)
                debug.append(f"✅ Image bytes received ({len(img_bytes)} bytes)")
                return out, debug
            else:
                debug.append("❌ No image bytes returned (response had no inline image data)")
        except Exception as e:
            debug.append(f"❌ Error: {type(e).__name__}: {e}")

    return None, debug

# -----------------------
# QUICK TEST BUTTON
# -----------------------
st.divider()
if st.button("Test AI image generation"):
    test_scene = "A cinematic close-up of a candle flame in a dark room, soft glow, realistic lighting."
    img, debug = try_generate_ai_image(test_scene, 999)
    st.subheader("AI Image Test Result")
    if img:
        st.success("AI image generation worked.")
        st.image(str(img), use_container_width=True)
    else:
        st.error("AI image generation FAILED. See debug below (this is the reason you only get placeholders).")
    st.code("\n".join(debug))
st.divider()

# -----------------------
# Generate everything
# -----------------------
if st.button("Generate Final MP4 Reel", type="primary"):

    script_prompt = f"""
Return ONLY valid JSON. No commentary. No markdown. No extra text.

Topic: {topic}
Scenes: {num_scenes}

Format EXACTLY:
{{
  "hook": "short hook",
  "scenes": ["scene text", "..."],
  "cta": "short CTA"
}}
""".strip()

    # Force JSON
    result = client.models.generate_content(
        model=TEXT_MODEL,
        contents=script_prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )

    raw = (getattr(result, "text", "") or "").strip()
    if not raw:
        st.error("Gemini returned empty output.")
        st.stop()

    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        st.error("Gemini did not return valid JSON. Raw output below:")
        st.code(raw)
        st.stop()

    st.subheader("Hook")
    st.success(data.get("hook", ""))

    scenes = data.get("scenes", [])
    cta = data.get("cta", "")

    if not isinstance(scenes, list) or len(scenes) == 0:
        st.error("No scenes returned; cannot build video.")
        st.stop()

    images = []
    narration_parts = [data.get("hook", "")]

    st.subheader("Scenes")
    progress = st.progress(0)

    for i, scene in enumerate(scenes, start=1):
        scene_text = str(scene)
        narration_parts.append(scene_text)

        img_path = None
        debug_lines = []

        if use_ai_images:
            img_path, debug_lines = try_generate_ai_image(scene_text, i)

        if not img_path:
            img_path = make_placeholder_image(scene_text, i)

        images.append(img_path)

        st.write(f"**Scene {i}:** {scene_text}")
        st.image(str(img_path), use_container_width=True)

        if show_image_debug and use_ai_images:
            with st.expander(f"AI image debug (Scene {i})"):
                st.code("\n".join(debug_lines) if debug_lines else "No debug output.")

        progress.progress(i / len(scenes))

    st.subheader("CTA")
    st.info(cta)
    narration_parts.append(cta)

    narration = ". ".join([p.strip() for p in narration_parts if p and p.strip()]) + "."

    audio_path = AUD_DIR / "voiceover.mp3"
    gTTS(narration, lang="en", slow=False).save(str(audio_path))
    st.subheader("Voiceover Preview")
    st.audio(str(audio_path))

    audio = AudioFileClip(str(audio_path))
    per_image = max(0.8, audio.duration / len(images))

    if MOVIEPY_V2:
        clips = [ImageClip(str(img), duration=per_image) for img in images]
        video = concatenate_videoclips(clips, method="compose").with_audio(audio)
    else:
        clips = [ImageClip(str(img)).set_duration(per_image) for img in images]
        video = concatenate_videoclips(clips, method="compose").set_audio(audio)

    out_video = VID_DIR / "final_reel.mp4"
    video.write_videofile(str(out_video), fps=30, codec="libx264", audio_codec="aac")

    st.success("Final MP4 ready")
    st.video(str(out_video))
    st.download_button(
        "Download MP4",
        data=open(out_video, "rb"),
        file_name="final_reel.mp4",
        mime="video/mp4",
    )
