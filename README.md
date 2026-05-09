# cut.ai

Auto-edits TikTok/UGC videos — removes silence, cuts dead space, burns captions.
Runs FFmpeg natively on a real server. No browser processing.

---

## Deploy to Railway (free, 5 minutes)

### 1. Create a GitHub repo

1. Go to github.com → New repository → name it `cutai`
2. Upload all files in this folder to the repo

### 2. Deploy on Railway

1. Go to railway.app → sign up free
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your `cutai` repo
4. Railway auto-detects Python and installs FFmpeg via nixpacks.toml
5. Click **Deploy** — done in ~2 minutes
6. Go to **Settings → Networking → Generate Domain** to get your URL

### 3. That's it

Your app is live. Share the URL with anyone.

---

## How it works

1. User drops a video on the site
2. Browser sends it to the server (Python + FastAPI)
3. Server extracts audio with FFmpeg and finds silence using pydub
4. FFmpeg cuts the silent segments frame-accurately
5. If captions enabled: Whisper transcribes, FFmpeg burns text onto video
6. Server returns a clean H.264 MP4 with AAC audio
7. User downloads — plays everywhere, correct framerate, correct duration

---

## Local development

```bash
# Install FFmpeg first
brew install ffmpeg        # Mac
sudo apt install ffmpeg    # Linux

# Python setup
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run
python -m uvicorn main:app --reload --port 8000
# Open http://localhost:8000
```
