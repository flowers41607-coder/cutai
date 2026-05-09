import os
import uuid
import asyncio
import subprocess
from pathlib import Path
from contextlib import asynccontextmanager

import aiofiles
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from video_processor import VideoProcessor

jobs: dict = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    for d in ("temp", "uploads", "outputs"):
        os.makedirs(d, exist_ok=True)
    # Log FFmpeg status on startup
    import shutil
    ff = shutil.which("ffmpeg")
    print(f"[startup] FFmpeg path: {ff}")
    if ff:
        r = subprocess.run([ff, "-version"], capture_output=True, text=True)
        print(f"[startup] FFmpeg version: {r.stdout.splitlines()[0] if r.stdout else 'unknown'}")
    else:
        print("[startup] WARNING: FFmpeg not found!")
    yield

app = FastAPI(title="cut.ai", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health():
    import shutil
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    return {
        "ffmpeg": ff or "NOT FOUND",
        "ffprobe": fp or "NOT FOUND",
        "pydub": True,
    }


@app.post("/api/upload")
async def upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    silence_threshold: float = Form(default=0.3),
    caption_style: str      = Form(default="classic"),
    caption_chunks: str     = Form(default="[]"),   # JSON string
):
    allowed = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
    ext = Path(file.filename or "video.mp4").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    job_id      = str(uuid.uuid4())
    upload_path = f"uploads/{job_id}{ext}"

    async with aiofiles.open(upload_path, "wb") as f:
        while chunk := await file.read(2 * 1024 * 1024):
            await f.write(chunk)

    import json
    try:
        chunks = json.loads(caption_chunks)
    except Exception:
        chunks = []

    jobs[job_id] = {
        "status":   "processing",
        "progress": 0,
        "step":     "Queued",
        "detail":   "",
        "error":    None,
        "output":   None,
        "stats":    {},
        "filename": file.filename,
    }

    settings = {
        "silence_threshold": silence_threshold,
        "caption_style":     caption_style,
        "caption_chunks":    chunks,
    }

    background_tasks.add_task(_run, job_id, upload_path, settings)
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return {k: v for k, v in jobs[job_id].items() if k != "output"}


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(400, "Not ready")
    path = job["output"]
    if not path or not os.path.exists(path):
        raise HTTPException(500, "Output file missing")
    fname = (job["filename"] or "video").replace(
        Path(job["filename"] or "video").suffix, ""
    ) + "_edited.mp4"
    return FileResponse(path, media_type="video/mp4", filename=fname)


async def _run(job_id: str, input_path: str, settings: dict):
    proc = VideoProcessor(job_id, jobs)
    await proc.process(input_path, settings)
    try:
        os.remove(input_path)
    except Exception:
        pass


@app.get("/")
async def root():
    return FileResponse("static/index.html")

app.mount("/", StaticFiles(directory="static", html=True), name="static")
