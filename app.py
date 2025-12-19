import json
from pathlib import Path

import streamlit as st
from google import genai
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS

# -------------------------------------------------
# MoviePy import (Cloud + Local compatible)
# -------------------------------------------------
try:
    # MoviePy v2 (local / newer)
    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips
    MOVIEPY_V2 = True
except ImportError:
    # MoviePy v1 (Streamlit Cloud)
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
st.title("YouTube Reel Generator – Full MP4 Builder")

# -----------------------
# Secrets + GenAI client
# -----------------------
api_key = st.secrets.get("GEMINI_API_KEY", "")
if not api_key:
    st.error("Missing GEMINI_API_KEY. Add it in Streamlit Cloud → Manage app → Settings → Secrets.")
    st.stop()

client = genai.Client(api_key=api_key)
MODEL_NAME = "gemini-2.5-flash"

# -----------------------
# User input
# -----------------------
topic = st.text_input(
    "Enter a topic for the YouTube Reel",
    value="Why does fire have no shadow?"
)
num_scenes = st.slider("Number of scenes", 5, 10, 7)

# -----------------------
# Placeholder image
# -----------------------
def make_placeholder_image(text: str, idx: int) -> Path:
    img = Image.new("RGB", (1080, 1920), (15, 15, 20))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 64)
    except Exception:
        font = ImageFont.load_default()

    draw.text((80, 120), f"Scene {idx}", fill=(200, 200, 200), font=font)

    # simple wrap
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

# -----------------------
# Generate everything
# -----------------------
if st.button("Generate Final MP4 Reel", type="primary"):

    prompt = f"""
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

    # ---- Gemini call (google-genai)
    result = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
    )
    raw = (getattr(result, "text", "") or "").strip()

    if not raw:
        st.error("Gemini returned empty output. Click again or change the topic.")
        st.stop()

    raw = raw.replace("```json", "").replace("```", "").strip()

    # ---- Robust JSON parse
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            data = json.loads(raw[start:end + 1])
        else:
            st.error("Gemini did not return valid JSON. Raw output:")
            st.code(raw)
            st.stop()

    # ---- Display + assets
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
    for i, scene in enumerate(scenes, start=1):
        img_path = make_placeholder_image(str(scene), i)
        images.append(img_path)
        narration_parts.append(str(scene))
        st.write(f"**Scene {i}:** {scene}")
        st.image(str(img_path), use_container_width=True)

    st.subheader("CTA")
    st.info(cta)
    narration_parts.append(cta)

    narration = ". ".join([p.strip() for p in narration_parts if p and str(p).strip()]) + "."

    # ---- Voiceover (gTTS)
    audio_path = AUD_DIR / "voiceover.mp3"
    gTTS(narration, lang="en", slow=False).save(str(audio_path))
    st.subheader("Voiceover Preview")
    st.audio(str(audio_path))

    # ---- Video render (MoviePy v1/v2 compatible)
    audio = AudioFileClip(str(audio_path))
    per_image = max(0.8, audio.duration / len(images))

    if MOVIEPY_V2:
        # v2: duration in constructor + with_audio
        clips = [ImageClip(str(img), duration=per_image) for img in images]
        video = concatenate_videoclips(clips, method="compose").with_audio(audio)
    else:
        # v1: set_duration + set_audio
        clips = [ImageClip(str(img)).set_duration(per_image) for img in images]
        video = concatenate_videoclips(clips, method="compose").set_audio(audio)

    out_video = VID_DIR / "final_reel.mp4"
    video.write_videofile(
        str(out_video),
        fps=30,
        codec="libx264",
        audio_codec="aac",
    )

    st.success("Final MP4 ready")
    st.video(str(out_video))
    st.download_button(
        "Download MP4",
        data=open(out_video, "rb"),
        file_name="final_reel.mp4",
        mime="video/mp4",
    )
