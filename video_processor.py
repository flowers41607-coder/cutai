import os
import json
import subprocess
import asyncio
import shutil
from pathlib import Path

try:
    from pydub import AudioSegment
    from pydub.silence import detect_nonsilent
    PYDUB_OK = True
except ImportError:
    PYDUB_OK = False


def ffmpeg_path():
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_path():
    return shutil.which("ffprobe") or "ffprobe"


def probe(path: str) -> dict:
    cmd = [ffprobe_path(), "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def get_duration(path: str) -> float:
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except Exception:
        return 0.0


class VideoProcessor:
    def __init__(self, job_id: str, jobs: dict):
        self.job_id   = job_id
        self.jobs     = jobs
        self.temp_dir = f"temp/{job_id}"
        os.makedirs(self.temp_dir, exist_ok=True)

    def update(self, progress: int, step: str, detail: str = ""):
        self.jobs[self.job_id].update({
            "progress": progress,
            "step":     step,
            "detail":   detail,
        })

    async def process(self, input_path: str, settings: dict):
        try:
            silence_s      = float(settings.get("silence_threshold", 0.3))
            caption_style  = settings.get("caption_style", "classic")
            caption_chunks = settings.get("caption_chunks", [])   # pre-computed by frontend
            os.makedirs("outputs", exist_ok=True)
            final_path = f"outputs/{self.job_id}.mp4"

            # 1 — probe original
            self.update(8, "Reading video", "Checking format and duration")
            orig_dur = get_duration(input_path)
            await asyncio.sleep(0.2)

            # 2 — detect speech segments
            self.update(22, "Analyzing audio", "Detecting silence and dead space")
            audio_wav = f"{self.temp_dir}/audio.wav"
            await asyncio.to_thread(self._extract_wav, input_path, audio_wav)
            segments  = await asyncio.to_thread(self._detect_segments, audio_wav, silence_s)

            if not segments:
                shutil.copy(input_path, final_path)
                self._finish(orig_dur, orig_dur, 0, 0.0, final_path)
                return

            # 3 — cut with FFmpeg
            self.update(45, "Cutting video", f"Removing silence — {len(segments)} segments kept")
            cut_path = f"{self.temp_dir}/cut.mp4"
            await asyncio.to_thread(self._cut_video, input_path, cut_path, segments)

            # 4 — burn captions if provided
            if caption_chunks:
                self.update(72, "Burning captions", "Drawing captions onto video")
                capped_path = f"{self.temp_dir}/capped.mp4"
                await asyncio.to_thread(
                    self._burn_captions, cut_path, capped_path,
                    caption_chunks, segments, caption_style
                )
                source = capped_path
            else:
                source = cut_path

            # 5 — final encode
            self.update(88, "Encoding", "Exporting MP4")
            await asyncio.to_thread(self._final_encode, source, final_path)

            final_dur = get_duration(final_path)
            removed   = max(0.0, orig_dur - final_dur)

            self._finish(orig_dur, final_dur, len(segments), removed, final_path)

        except Exception as e:
            self.jobs[self.job_id].update({
                "status": "error",
                "step":   "Error",
                "error":  str(e),
            })
        finally:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _finish(self, orig, final, cuts, removed, path):
        self.jobs[self.job_id].update({
            "status":   "done",
            "progress": 100,
            "step":     "Done",
            "output":   path,
            "stats": {
                "original_duration": round(orig, 1),
                "final_duration":    round(final, 1),
                "removed_seconds":   round(removed, 1),
                "cuts":              cuts,
            },
        })

    def _extract_wav(self, video_path: str, wav_path: str):
        subprocess.run(
            [ffmpeg_path(), "-i", video_path,
             "-ar", "16000", "-ac", "1", "-y", wav_path],
            capture_output=True, check=True,
        )

    def _detect_segments(self, wav_path: str, silence_s: float) -> list:
        if not PYDUB_OK:
            return []
        audio     = AudioSegment.from_wav(wav_path)
        total_ms  = len(audio)
        thresh    = audio.dBFS - 16
        min_sil   = int(silence_s * 1000)
        padding   = 25   # ms — tight cuts

        nonsilent = detect_nonsilent(
            audio, min_silence_len=min_sil,
            silence_thresh=thresh, seek_step=10,
        )
        if not nonsilent:
            return []

        padded = [
            [max(0, s - padding), min(total_ms, e + padding)]
            for s, e in nonsilent
        ]
        # merge overlapping
        merged = []
        for seg in padded:
            if merged and seg[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], seg[1])
            else:
                merged.append(seg)

        return [[s / 1000.0, e / 1000.0] for s, e in merged if (e - s) > 150]

    def _cut_video(self, input_path: str, output_path: str, segments: list):
        n  = len(segments)
        fv = [f"[0:v]trim={s:.4f}:{e:.4f},setpts=PTS-STARTPTS[v{i}]"
              for i, (s, e) in enumerate(segments)]
        fa = [f"[0:a]atrim={s:.4f}:{e:.4f},asetpts=PTS-STARTPTS[a{i}]"
              for i, (s, e) in enumerate(segments)]
        vc = "".join(f"[v{i}]" for i in range(n))
        ac = "".join(f"[a{i}]" for i in range(n))
        fc = (
            ";".join(fv) + ";" +
            ";".join(fa) + ";" +
            f"{vc}concat=n={n}:v=1:a=0[outv];" +
            f"{ac}concat=n={n}:v=0:a=1[outa]"
        )
        cmd = [
            ffmpeg_path(), "-i", input_path,
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-y", output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg cut failed: {result.stderr[-500:]}")

    def _remap_time(self, t: float, segments: list) -> float:
        out = 0.0
        for s, e in segments:
            if t < s:
                return out
            if t <= e:
                return out + (t - s)
            out += (e - s)
        return out

    def _burn_captions(self, input_path: str, output_path: str,
                        chunks: list, segments: list, style: str):
        # Remap timestamps to post-cut timeline
        remapped = []
        for c in chunks:
            rs = self._remap_time(c["start"], segments)
            re = self._remap_time(c["end"],   segments)
            if re - rs > 0.05:
                remapped.append({"text": c["text"], "start": rs, "end": re})

        if not remapped:
            shutil.copy(input_path, output_path)
            return

        style_params = {
            "classic":   "fontcolor=white:box=1:boxcolor=black@0.82:boxborderw=18",
            "outline":   "fontcolor=white:bordercolor=black:borderw=4",
            "highlight": "fontcolor=FFE000:box=1:boxcolor=black@0.85:boxborderw=18",
            "minimal":   "fontcolor=white@0.92:box=1:boxcolor=black@0.45:boxborderw=18",
        }.get(style, "fontcolor=white:box=1:boxcolor=black@0.82:boxborderw=18")

        parts = []
        for c in remapped:
            safe = (
                c["text"]
                .replace("\\", "\\\\")
                .replace("'",  "\u2019")
                .replace(":",  "\\:")
                .replace("%",  "\\%")
                .replace("[",  "\\[")
                .replace("]",  "\\]")
            )
            parts.append(
                f"drawtext=text='{safe}':fontsize=52:{style_params}"
                f":x=(w-text_w)/2:y=h*0.72"
                f":enable='between(t\\,{c['start']:.3f}\\,{c['end']:.3f})'"
            )

        vf = ",".join(parts)
        cmd = [
            ffmpeg_path(), "-i", input_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-y", output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            shutil.copy(input_path, output_path)

    def _final_encode(self, input_path: str, output_path: str):
        cmd = [
            ffmpeg_path(), "-i", input_path,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-y", output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            shutil.copy(input_path, output_path)
