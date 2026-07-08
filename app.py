import json, time, re, os, asyncio
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Any
import zipfile

import streamlit as st
import requests
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Pillow >= 10 removed ANTIALIAS; moviepy 1.x still references it.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    concatenate_videoclips,
    concatenate_audioclips,
    AudioClip,
)

from google import genai
from google.genai import types

# -------------------- CONFIG --------------------
W, H = 1080, 1920
FPS = 30
IMAGES_PER_SCENE = 2
CROSSFADE = 0.5
MIN_SCENE_SECONDS = 4.0        # floor so a very short line doesn't flash by
MAX_SCENE_SECONDS = 14.0       # ceiling so one long line can't blow the pacing
AUDIO_FPS = 44100

FONT_SIZE = 62                 # 84 overflowed 1080px at 30-char wraps
SUB_MAX_TEXT_W = W - 220       # pixel budget for a subtitle line
SUB_BOTTOM_MARGIN = 340        # keep captions above the YouTube Shorts UI overlay

# TTS voice (edge-tts neural voice; falls back to gTTS if unavailable)
EDGE_VOICE = "en-US-ChristopherNeural"
EDGE_RATE = "+8%"

BASE = Path(__file__).parent
IMG = BASE / "images"
AUD = BASE / "audio"
VID = BASE / "video"
CACHE = BASE / "cache"
for d in (IMG, AUD, VID, CACHE):
    d.mkdir(exist_ok=True)

TOPIC_HISTORY = CACHE / "topics_history.json"
REELS_DB = CACHE / "reels_db.json"

# -------------------- KEYS --------------------
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
PEXELS_KEY = st.secrets["PEXELS_API_KEY"]
client = genai.Client(api_key=GEMINI_API_KEY)
TEXT_MODEL = "models/gemini-2.5-flash"

# -------------------- STORAGE --------------------
def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def history_list() -> List[str]:
    return load_json(TOPIC_HISTORY, [])

def db_obj() -> Dict[str, Any]:
    return load_json(REELS_DB, {"reels": {}})

# -------------------- HELPERS --------------------
def slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (t or "").lower()).strip("_")[:60] or "reel"

def silence(d: float):
    return AudioClip(lambda t: np.zeros((1,), dtype=np.float32), duration=d, fps=AUDIO_FPS)

def get_font(size: int = FONT_SIZE):
    for p in (
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()

FONT = get_font()

# -------------------- TTS --------------------
def tts_save(text: str, out_path: Path):
    """Neural voice via edge-tts; gTTS as fallback."""
    text = (text or "").strip() or "..."
    try:
        import edge_tts

        async def _run():
            await edge_tts.Communicate(text, EDGE_VOICE, rate=EDGE_RATE).save(str(out_path))

        asyncio.run(_run())
        if out_path.exists() and out_path.stat().st_size > 1000:
            return
    except Exception:
        pass
    from gtts import gTTS
    gTTS(text).save(str(out_path))

# -------------------- SUBTITLES --------------------
def wrap_by_pixels(text: str, font, max_w: int, max_lines: int = 3) -> List[str]:
    words = (text or "").split()
    lines, cur = [], []
    for w in words:
        test = " ".join(cur + [w])
        if font.getlength(test) <= max_w or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    if len(lines) > max_lines:
        # merge overflow into the last allowed line rather than dropping words
        lines = lines[: max_lines - 1] + [" ".join(lines[max_lines - 1 :])]
    return lines or [""]

def subtitle_png(text: str, out: Path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    lines = wrap_by_pixels(text, FONT, SUB_MAX_TEXT_W)

    spacing = 14
    line_h = FONT_SIZE + 10
    tw = max(int(FONT.getlength(l)) for l in lines)
    th = line_h * len(lines) + spacing * (len(lines) - 1)

    pad_x, pad_y = 48, 30
    box_w = min(tw + 2 * pad_x, W - 80)
    box_h = th + 2 * pad_y

    x1 = (W - box_w) // 2
    y2 = H - SUB_BOTTOM_MARGIN
    y1 = y2 - box_h
    x2 = x1 + box_w

    d.rounded_rectangle((x1, y1, x2, y2), radius=28, fill=(0, 0, 0, 170))

    y = y1 + pad_y
    for line in lines:
        lw = FONT.getlength(line)
        lx = (W - lw) // 2
        d.text(
            (lx, y),
            line,
            fill=(255, 255, 255, 255),
            font=FONT,
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
        y += line_h + spacing

    img.save(out)

# -------------------- IMAGES --------------------
def pexels(query: str, per_page: int = 15):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_KEY},
        params={"query": query, "orientation": "portrait", "per_page": per_page, "size": "large"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("photos", [])

def get_image(url: str, out: Path):
    r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGB")
    # fit slightly larger than frame so Ken Burns zoom never reveals edges
    img = ImageOps.fit(img, (int(W * 1.15), int(H * 1.15)))
    img.save(out, quality=92)

def placeholder(out: Path, text: str):
    im = Image.new("RGB", (int(W * 1.15), int(H * 1.15)), (24, 26, 32))
    d = ImageDraw.Draw(im)
    d.text((60, 80), "PLACEHOLDER", fill=(240, 240, 240), font=FONT)
    d.text((60, 200), (text or "")[:120], fill=(200, 200, 200), font=get_font(40))
    im.save(out, quality=92)

def ken_burns(img_path: Path, duration: float, zoom_in: bool = True) -> CompositeVideoClip:
    """Slow zoom on a still image. Alternating direction keeps it from feeling mechanical."""
    z0, z1 = (1.0, 1.10) if zoom_in else (1.10, 1.0)
    base = ImageClip(str(img_path)).set_duration(duration)
    dur = max(duration, 0.01)
    clip = base.resize(lambda t: z0 + (z1 - z0) * (t / dur)).set_position("center")
    return CompositeVideoClip([clip], size=(W, H)).set_duration(duration)

# -------------------- JSON SAFE PARSE --------------------
def parse_json_strict(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(raw)
    except Exception:
        s = raw.find("{")
        e = raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(raw[s : e + 1])
        raise

# -------------------- GEMINI --------------------
def generate_topics(n: int) -> List[str]:
    existing = {t.lower() for t in history_list()}
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Generate {n} unique YouTube Shorts science/curiosity topics.
Rules:
- Each is a short, punchy question (max 10 words) a viewer would stop scrolling for.
- Prefer counterintuitive or "wait, really?" angles over textbook questions.
- No repeats or near-repeats.
- Do NOT include any of these existing topics:
{list(existing)[:400]}

Format exactly:
{{"topics": ["..."]}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = parse_json_strict(r.text)
    topics = []
    for t in data.get("topics", []):
        t = (t or "").strip()
        if not t or t.lower() in existing:
            continue
        topics.append(t)
        existing.add(t.lower())
        if len(topics) >= n:
            break

    hist = history_list()
    hist.extend([t.lower() for t in topics])
    hist = list(dict.fromkeys(hist))
    save_json(TOPIC_HISTORY, hist)
    return topics

def generate_script(topic: str, scenes: int) -> List[Dict[str, str]]:
    prompt = f"""
Return ONLY valid JSON (no markdown, no commentary).

Write a YouTube Shorts voiceover script for:
Topic: "{topic}"

Hard rules:
- EXACTLY {scenes} scenes.
- Scene 1 is a HOOK: open a curiosity gap in one bold sentence. No "welcome", no "today we".
- Middle scenes each deliver ONE concrete fact or step that builds on the last.
- Final scene is the PAYOFF: resolve the hook, then one short line inviting a follow/comment.
- Each scene's subtitle is 16-24 spoken words (about 8-10 seconds of speech). Conversational, simple words.
- image_query must be a CONCRETE visual phrase of 2-4 nouns a stock-photo site would have
  (e.g. "lightning storm night sky", not "the physics of electricity").

Format exactly:
{{
  "scenes": [
    {{"subtitle":"...", "image_query":"..."}}
  ]
}}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    data = parse_json_strict(r.text)
    scenes_list = data.get("scenes", [])
    if not isinstance(scenes_list, list):
        scenes_list = []

    scenes_list = scenes_list[:scenes]
    while len(scenes_list) < scenes:
        scenes_list.append({"subtitle": "Here is the key idea in simple terms.", "image_query": topic})

    cleaned = []
    for s in scenes_list:
        cleaned.append(
            {
                "subtitle": str(s.get("subtitle", "")).strip() or "Here is the key idea in simple terms.",
                "image_query": str(s.get("image_query", "")).strip() or topic,
            }
        )
    return cleaned

# -------------------- BUILD REEL --------------------
def build_reel(topic: str, scenes_list: List[Dict[str, str]], idx: int, cb=None) -> Path:
    reel_id = f"reel{idx:02d}_{slug(topic)}_{int(time.time())}"
    scene_clips = []
    used_photo_ids = set()  # avoid the same stock photo appearing twice in one reel

    total = len(scenes_list)
    for si, sc in enumerate(scenes_list, 1):
        if cb:
            cb((si - 1) / max(1, total) * 0.8, f"Scene {si}/{total}: voice + images")

        # ---- voiceover first: it decides the scene length ----
        mp3 = AUD / f"{reel_id}_scene_{si:02d}.mp3"
        tts_save(sc["subtitle"], mp3)
        voice = AudioFileClip(str(mp3))

        # tail padding must exceed the crossfade so speech never bleeds into the next scene
        scene_dur = voice.duration + CROSSFADE + 0.25
        scene_dur = min(max(scene_dur, MIN_SCENE_SECONDS), MAX_SCENE_SECONDS)
        if voice.duration > scene_dur - 0.1:
            voice = voice.subclip(0, scene_dur - 0.1)
        audio = concatenate_audioclips([voice, silence(scene_dur - voice.duration)]).set_duration(scene_dur)

        # ---- images ----
        try:
            photos = [p for p in pexels(sc["image_query"], per_page=20) if p.get("id") not in used_photo_ids]
        except Exception:
            photos = []

        img_paths = []
        for j in range(IMAGES_PER_SCENE):
            out = IMG / f"{reel_id}_s{si:02d}_i{j+1:02d}.jpg"
            if j < len(photos):
                p = photos[j]
                url = p.get("src", {}).get("portrait") or p.get("src", {}).get("large2x")
                try:
                    if url:
                        get_image(url, out)
                        used_photo_ids.add(p.get("id"))
                    else:
                        placeholder(out, f"{topic} / scene {si}")
                except Exception:
                    placeholder(out, f"{topic} / scene {si}")
            else:
                placeholder(out, f"{topic} / scene {si}")
            img_paths.append(out)

        # two images per scene, alternating zoom direction, crossfaded
        half = (scene_dur + CROSSFADE) / 2
        c1 = ken_burns(img_paths[0], half, zoom_in=(si % 2 == 1))
        c2 = ken_burns(img_paths[1], half, zoom_in=(si % 2 == 0)).crossfadein(CROSSFADE)
        scene_vid = concatenate_videoclips([c1, c2], method="compose", padding=-CROSSFADE).set_duration(scene_dur)

        # subtitle overlay
        sub_path = IMG / f"{reel_id}_sub_{si:02d}.png"
        subtitle_png(sc["subtitle"], sub_path)
        sub = ImageClip(str(sub_path)).set_duration(scene_dur)

        composed = (
            CompositeVideoClip([scene_vid, sub], size=(W, H))
            .set_duration(scene_dur)
            .set_audio(audio)
        )
        scene_clips.append(composed)

    if cb:
        cb(0.85, "Stitching scenes...")

    # crossfade between scenes; audio rides inside each clip so sync is preserved.
    for k in range(1, len(scene_clips)):
        scene_clips[k] = scene_clips[k].crossfadein(CROSSFADE)
    video = concatenate_videoclips(scene_clips, method="compose", padding=-CROSSFADE)

    out = VID / f"{reel_id}.mp4"
    if cb:
        cb(0.9, "Exporting MP4 (this is the slow part)...")

    video.write_videofile(
        str(out),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        audio_bitrate="192k",
        preset="medium",
        threads=os.cpu_count() or 2,
        ffmpeg_params=["-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        logger=None,
    )

    if cb:
        cb(1.0, "Done.")

    try:
        video.close()
    except Exception:
        pass
    return out

# -------------------- UI --------------------
st.set_page_config(page_title="Reel Factory", layout="wide")
st.title("Reel Factory — Gemini Script + Pexels Images + Synced Subtitles")

col1, col2 = st.columns([1.1, 0.9])

if "topics" not in st.session_state:
    st.session_state["topics"] = []
if "selected" not in st.session_state:
    st.session_state["selected"] = None

with col1:
    st.subheader("1) Topics")
    how_many = st.slider("How many new topics?", 1, 20, 10)
    scenes_n = st.selectbox("Scenes per reel", [5, 6, 8], index=1)
    st.caption(f"Scene length now follows the voiceover (~8-12s each), so {scenes_n} scenes ≈ {scenes_n*10}s.")

    if st.button("Generate Topics"):
        with st.spinner("Generating topics..."):
            st.session_state["topics"] = generate_topics(how_many)
            st.session_state["selected"] = st.session_state["topics"][0] if st.session_state["topics"] else None

    if st.session_state["topics"]:
        st.session_state["selected"] = st.selectbox("Pick one topic", st.session_state["topics"])

with col2:
    st.subheader("2) Build Reel(s)")
    topic = st.session_state.get("selected")
    if not topic:
        st.info("Generate topics and select one.")
    else:
        reels_to_build = st.slider("How many reels to build now?", 1, min(20, len(st.session_state["topics"])), 1)
        build_list = st.session_state["topics"][:reels_to_build]

        if st.button("Build Now"):
            vids = []
            overall = st.progress(0)
            status = st.empty()
            eta = st.empty()

            start_all = time.time()
            for i, tp in enumerate(build_list, 1):
                t0 = time.time()
                status.write(f"Reel {i}/{len(build_list)}: {tp}")

                def cb(p, msg):
                    overall.progress((i - 1 + p) / len(build_list))
                    elapsed = time.time() - start_all
                    done_frac = (i - 1 + p) / len(build_list)
                    if done_frac > 0:
                        remaining = (elapsed / done_frac) - elapsed
                        eta.write(f"ETA ~ {int(max(0, remaining))}s")
                    status.write(f"Reel {i}/{len(build_list)} — {int(p*100)}%: {msg}")

                scenes_list = generate_script(tp, scenes_n)
                out = build_reel(tp, scenes_list, i, cb=cb)

                db = db_obj()
                db["reels"][tp.lower()] = {
                    "topic": tp,
                    "scenes": scenes_list,
                    "video_path": str(out),
                    "updated_at": int(time.time()),
                }
                save_json(REELS_DB, db)

                vids.append(out)
                status.write(f"✅ Done reel {i} in {int(time.time()-t0)}s")

            st.success("All reels completed.")

            for v in vids:
                st.video(str(v))
                st.download_button(
                    f"Download {v.name}",
                    v.read_bytes(),
                    file_name=v.name,
                    mime="video/mp4",
                )

            if len(vids) > 1:
                zip_path = VID / f"reels_{int(time.time())}.zip"
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                    for v in vids:
                        z.write(v, arcname=v.name)
                st.download_button(
                    "Download ALL as ZIP",
                    zip_path.read_bytes(),
                    file_name=zip_path.name,
                    mime="application/zip",
                )
