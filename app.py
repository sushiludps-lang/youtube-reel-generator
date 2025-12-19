import json
from pathlib import Path

import streamlit as st
import google.generativeai as genai
from PIL import Image, ImageDraw, ImageFont
from gtts import gTTS
from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

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
# Gemini setup
# -----------------------
genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
model = genai.GenerativeModel("models/gemini-2.5-flash")

# -----------------------
# User input
# -----------------------
topic = st.text_input(
    "Enter a topic for the YouTube Reel",
    value="Why does fire have no shadow?"
)
num_scenes = st.slider("Number of scenes", 5, 10, 7)

# -----------------------
# Placeholder image generator
# -----------------------
def make_placeholder_image(text: str, idx: int) -> Path:
    img = Image.new("RGB", (1080, 1920), (15, 15, 20))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("arial.ttf", 64)
    except Exception:
        font = ImageFont.load_default()

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

# -----------------------
# Generate pipeline
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
"""

    # ---- Gemini call
    result = model.generate_content(prompt)
    raw = (getattr(result, "text", "") or "").strip()

    if not raw:
        st.error("Gemini returned empty output.")
        st.stop()

    raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            data = json.loads(raw[start:end + 1])
        else:
            st.error("Invalid JSON from Gemini")
            st.code(raw)
            st.stop()

    # ---- Display + build assets
    st.subheader("Hook")
    st.success(data["hook"])

    images = []
    narration_parts = [data["hook"]]

    st.subheader("Scenes")
    for i, scene in enumerate(data["scenes"], start=1):
        img_path = make_placeholder_image(scene, i)
        images.append(img_path)
        narration_parts.append(scene)
        st.write(f"**Scene {i}:** {scene}")
        st.image(str(img_path), use_container_width=True)

    st.subheader("CTA")
    st.info(data["cta"])
    narration_parts.append(data["cta"])

    narration = ". ".join(narration_parts) + "."

    # ---- Voiceover
    audio_path = AUD_DIR / "voiceover.mp3"
    gTTS(narration, lang="en").save(str(audio_path))
    st.subheader("Voiceover Preview")
    st.audio(str(audio_path))

    # ---- Video render (MoviePy v2 SAFE)
    audio = AudioFileClip(str(audio_path))
    per_image = max(0.8, audio.duration / len(images))

    clips = [ImageClip(str(img), duration=per_image) for img in images]

    video = concatenate_videoclips(clips, method="compose").with_audio(audio)

    out_video = VID_DIR / "final_reel.mp4"
    video.write_videofile(
        str(out_video),
        fps=30,
        codec="libx264",
        audio_codec="aac"
    )

    st.success("Final MP4 ready 🎉")
    st.video(str(out_video))
    st.download_button(
        "Download MP4",
        data=open(out_video, "rb"),
        file_name="final_reel.mp4",
        mime="video/mp4",
    )
