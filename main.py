import os
import uuid
import subprocess
import signal
import sys
import threading
import re
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from concurrent.futures import ThreadPoolExecutor

OUTPUT_DIR = "downloads"
STATIC_DIR = "static"
MAX_WORKERS = 4
MAX_FILE_SIZE_MB = 500
DOWNLOAD_TIMEOUT = 600  # 10 min max per download

app = FastAPI()
Path(OUTPUT_DIR).mkdir(exist_ok=True)
Path(STATIC_DIR).mkdir(exist_ok=True)

downloads: dict = {}
active_processes: set = set()
processes_lock = threading.Lock()  # ✅ Fix: thread-safe set access
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ─── URL validation ────────────────────────────────────────────────────────────

ALLOWED_DOMAINS = re.compile(
    r"^https?://(www\.)?"
    r"(youtube\.com|youtu\.be|vimeo\.com|twitter\.com|x\.com|"
    r"instagram\.com|tiktok\.com|dailymotion\.com|twitch\.tv|"
    r"reddit\.com|facebook\.com|soundcloud\.com)",
    re.IGNORECASE,
)


class DownloadRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not ALLOWED_DOMAINS.match(v):
            raise ValueError("URL must be from a supported platform (YouTube, Vimeo, TikTok, etc.)")
        return v


# ─── Graceful shutdown ─────────────────────────────────────────────────────────

def cleanup(*args):
    print("\n🛑 Shutting down... cleaning processes")
    with processes_lock:
        for p in list(active_processes):
            try:
                p.kill()
            except Exception:
                pass
    executor.shutdown(wait=False)
    print("✅ Clean exit")
    sys.exit(0)


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    if "youtube.com/shorts/" in url:
        vid = url.split("/")[-1].split("?")[0]
        return f"https://www.youtube.com/watch?v={vid}"
    return url


def safe_output_path(filename: str) -> Path:
    """Ensure the resolved path stays inside OUTPUT_DIR."""
    base = Path(OUTPUT_DIR).resolve()
    target = (base / filename).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError("Path traversal detected")
    return target


def run_cmd_with_progress(cmd, task_id, timeout=DOWNLOAD_TIMEOUT):
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    with processes_lock:
        active_processes.add(process)

    def _kill_after_timeout():
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            downloads[task_id]["status"] = "error"
            downloads[task_id]["error"] = "Download timed out"

    timer = threading.Thread(target=_kill_after_timeout, daemon=True)
    timer.start()

    for line in process.stdout:
        line = line.strip()
        if "%" in line:
            try:
                percent = line.split("%")[0].split()[-1]
                downloads[task_id]["progress"] = percent + "%"
            except Exception:
                pass

    process.wait()

    with processes_lock:
        active_processes.discard(process)

    return process.returncode


def compress_video(input_path: str, output_path: str) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vcodec", "libx264",
        "-crf", "28",
        "-preset", "fast",
        "-acodec", "aac",
        "-b:a", "96k",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ffmpeg error] {result.stderr[-500:]}")  # log last 500 chars
        return False
    return True


def check_disk_space() -> bool:
    import shutil
    free_mb = shutil.disk_usage(OUTPUT_DIR).free / (1024 * 1024)
    return free_mb > MAX_FILE_SIZE_MB  # require headroom equal to max file size


# ─── Download worker ───────────────────────────────────────────────────────────

def download_worker(task_id: str, url: str):
    downloads[task_id]["status"] = "downloading"
    downloads[task_id]["progress"] = "0%"

    raw_file = None
    final_file = None

    try:
        if not check_disk_space():
            raise Exception("Not enough disk space to download")

        url = normalize_url(url)

        raw_name = f"{uuid.uuid4()}_raw.mp4"
        final_name = f"{uuid.uuid4()}.mp4"
        raw_file = str(safe_output_path(raw_name))
        final_file = str(safe_output_path(final_name))

        base_cmd = [
            "yt-dlp",
            "--no-playlist",
            "--merge-output-format", "mp4",
            "--retries", "5",
            "--fragment-retries", "5",
            "--no-cache-dir",
            "-o", raw_file,
            "--add-header", "user-agent:Mozilla/5.0",
            "--force-ipv4",
        ]

        strategies = [
            base_cmd + ["--extractor-args", "youtube:player_client=android", "-f", "best", url],
            base_cmd + ["--extractor-args", "youtube:player_client=tv_embedded", "-f", "best", url],
            base_cmd + ["-f", "best", url],
        ]

        code = 1
        for cmd in strategies:
            code = run_cmd_with_progress(cmd, task_id)
            if code == 0:
                break

        if code != 0:
            raise Exception("All download strategies failed")

        # ✅ Check downloaded file size
        raw_size_mb = os.path.getsize(raw_file) / (1024 * 1024)
        if raw_size_mb > MAX_FILE_SIZE_MB:
            raise Exception(f"File too large ({raw_size_mb:.0f} MB > {MAX_FILE_SIZE_MB} MB limit)")

        downloads[task_id]["status"] = "compressing"
        downloads[task_id]["progress"] = "100%"

        ok = compress_video(raw_file, final_file)
        if not ok:
            raise Exception("Compression failed")

        downloads[task_id]["status"] = "completed"
        downloads[task_id]["file"] = final_file
        downloads[task_id]["filename"] = final_name

    except Exception as e:
        downloads[task_id]["status"] = "error"
        downloads[task_id]["error"] = str(e)

    finally:
        # Always clean up raw file
        if raw_file and os.path.exists(raw_file):
            try:
                os.remove(raw_file)
            except Exception:
                pass


# ─── API routes ────────────────────────────────────────────────────────────────

@app.post("/download")
def download_video(req: DownloadRequest):
    task_id = str(uuid.uuid4())
    downloads[task_id] = {
        "url": req.url,
        "status": "queued",
        "progress": "0%",
    }
    executor.submit(download_worker, task_id, req.url)
    return {"task_id": task_id}


@app.get("/status/{task_id}")
def status(task_id: str):
    task = downloads.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/all")
def all_downloads():
    return downloads


@app.get("/files")
def files():
    return [
        {"path": os.path.join(OUTPUT_DIR, f), "name": f, "size_mb": round(os.path.getsize(os.path.join(OUTPUT_DIR, f)) / (1024 * 1024), 2)}
        for f in os.listdir(OUTPUT_DIR)
        if f.endswith(".mp4")
    ]


@app.get("/download-file")
def download_file(path: str = Query(...)):
    # ✅ Fix path traversal: validate path stays inside OUTPUT_DIR
    try:
        resolved = Path(path).resolve()
        base = Path(OUTPUT_DIR).resolve()
        if not str(resolved).startswith(str(base)):
            raise HTTPException(status_code=403, detail="Access denied")
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(str(resolved), media_type="video/mp4", filename=resolved.name)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")


@app.delete("/delete")
def delete(path: str = Query(...)):
    try:
        resolved = Path(path).resolve()
        base = Path(OUTPUT_DIR).resolve()
        # ✅ Fix: validate path is inside OUTPUT_DIR before deleting
        if not str(resolved).startswith(str(base)):
            raise HTTPException(status_code=403, detail="Access denied")
        if resolved.exists():
            resolved.unlink()
            return {"status": "deleted"}
        raise HTTPException(status_code=404, detail="File not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Serve frontend ────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(f"{STATIC_DIR}/index.html")


# ─── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)