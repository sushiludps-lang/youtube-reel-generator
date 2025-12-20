import json, time, re, os, zipfile
from pathlib import Path
from typing import List
import requests
import numpy as np
import streamlit as st
from gtts import gTTS
from PIL import Image, ImageDraw, ImageFont, ImageOps

# MoviePy (Cloud-safe)
from moviepy.editor import (
    ImageClip, AudioFileClip, CompositeVideoClip,
    concatenate_videoclips, concatenate_audioclips, AudioClip, vfx
)

# Gemini
from google import genai
from google.genai import types

# ---------------- CONFIG ----------------
W, H = 1080, 1920
FPS = 30
SCENE_SECONDS = 10
IMAGES_PER_SCENE = 2
IMG_SECONDS = SCENE_SECONDS / IMAGES_PER_SCENE
CROSSFADE = 0.6
AUDIO_FPS = 44100

BASE = Path(__file__).parent
IMG = BASE / "images"
AUD = BASE / "audio"
VID = BASE / "video"
for d in (IMG, AUD, VID): d.mkdir(exist_ok=True)

# ---------------- SECRETS ----------------
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
PEXELS_API_KEY = st.secrets["PEXELS_API_KEY"]
client = genai.Client(api_key=GEMINI_API_KEY)
TEXT_MODEL = "models/gemini-2.5-flash"

# ---------------- HELPERS ----------------
def slug(t): return re.sub(r"[^a-z0-9]+", "_", t.lower())[:50]

def pexels(q):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": PEXELS_API_KEY},
        params={"query": q, "orientation": "portrait", "per_page": 10},
        timeout=20
    )
    return r.json().get("photos", [])

def download_img(url, out):
    out.write_bytes(requests.get(url, timeout=20).content)
    img = Image.open(out).convert("RGB")
    img = ImageOps.fit(img, (W, H))
    img.save(out)

def subtitle_png(text, out):
    img = Image.new("RGBA", (W, H), (0,0,0,0))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    box = d.textbbox((0,0), text, font)
    x = (W - (box[2]-box[0]))//2
    y = H - 200
    d.rectangle((x-30,y-20,x+box[2]+30,y+box[3]+20), fill=(0,0,0,180))
    d.text((x,y), text, fill="white", font=font)
    img.save(out)

def silence(dur):
    return AudioClip(lambda t: np.zeros((1,),dtype=np.float32), duration=dur, fps=AUDIO_FPS)

def fit_audio(a, dur):
    if a.duration > dur: return a.subclip(0,dur)
    if a.duration < dur: return concatenate_audioclips([a, silence(dur-a.duration)])
    return a

# ---------------- GEMINI ----------------
def gen_topics(existing, n):
    prompt = f"""
Return JSON only:
{{"topics": ["..."]}}

Generate {n} unique science curiosity questions.
Avoid duplicates with: {existing}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(r.text)["topics"]

def gen_script(topic, scenes):
    prompt = f"""
Return JSON only.
Exactly {scenes} scenes.
Each scene spoken ~10 seconds.

{{"scenes":[{{"subtitle":"...","query":"..."}}]}}
Topic: {topic}
"""
    r = client.models.generate_content(
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(r.text)["scenes"]

# ---------------- BUILD REEL ----------------
def build_reel(topic, scenes, idx, cb):
    start=time.time()
    def prog(p,m): cb(p,m,int(time.time()-start))

    prog(0.05,"Images")
    imgs=[]
    for i,s in enumerate(scenes,1):
        photos=pexels(s["query"])
        pair=[]
        for j in range(IMAGES_PER_SCENE):
            out=IMG/f"{idx}_{i}_{j}.jpg"
            try:
                download_img(photos[j]["src"]["portrait"],out)
            except:
                Image.new("RGB",(W,H),(20,20,20)).save(out)
            pair.append(out)
        imgs.append(pair)

    prog(0.35,"Audio")
    aud=[]
    for i,s in enumerate(scenes,1):
        mp3=AUD/f"{idx}_{i}.mp3"
        gTTS(s["subtitle"]).save(mp3)
        aud.append(fit_audio(AudioFileClip(mp3),SCENE_SECONDS))
    full_audio=concatenate_audioclips(aud)

    prog(0.6,"Video")
    clips=[]
    for i,(pair,s) in enumerate(zip(imgs,scenes),1):
        c1=ImageClip(str(pair[0])).set_duration(IMG_SECONDS).fx(vfx.resize,1.03)
        c2=ImageClip(str(pair[1])).set_duration(IMG_SECONDS).fx(vfx.resize,1.03).crossfadein(CROSSFADE)
        base=concatenate_videoclips([c1,c2],padding=-CROSSFADE).set_duration(SCENE_SECONDS)

        sub=IMG/f"{idx}_{i}_sub.png"
        subtitle_png(s["subtitle"],sub)
        subclip=ImageClip(str(sub)).set_duration(SCENE_SECONDS)

        scene=CompositeVideoClip([base,subclip]).set_audio(aud[i-1])
        clips.append(scene)

    final=concatenate_videoclips(clips,padding=-CROSSFADE).set_audio(full_audio)
    out=VID/f"{slug(topic)}_{idx}.mp4"
    final.write_videofile(out,fps=FPS,codec="libx264",audio_codec="aac",logger=None)
    prog(1,"Done")
    return out

# ---------------- UI ----------------
st.set_page_config("Reel Factory",layout="wide")
st.title("🎬 Reel Factory — Batch YouTube Shorts Generator")

scenes_n=st.selectbox("Scenes per reel (10s each)",[6,8])
reels_n=st.number_input("How many reels to generate?",1,20,1)

if st.button("Generate Topics"):
    st.session_state.topics=gen_topics([],20)

if "topics" in st.session_state:
    topics=st.session_state.topics[:reels_n]
    st.write(topics)

    if st.button("Build Reels"):
        vids=[]
        p=st.progress(0); t=st.empty()
        for i,tp in enumerate(topics,1):
            sc=gen_script(tp,scenes_n)
            def cb(pc,msg,eta):
                p.progress(int(pc*100))
                t.write(f"{msg} — ETA {eta}s")
            vids.append(build_reel(tp,sc,i,cb))

        if len(vids)==1:
            st.video(str(vids[0]))
            st.download_button("Download MP4",open(vids[0],"rb"),vids[0].name)
        else:
            zipf=VID/"reels.zip"
            with zipfile.ZipFile(zipf,"w") as z:
                for v in vids: z.write(v,v.name)
            st.download_button("Download ALL (ZIP)",open(zipf,"rb"),"reels.zip")
