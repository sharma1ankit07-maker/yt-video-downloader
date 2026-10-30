"""
Download site for your own channel's videos.

Set CHANNEL_URL (e.g. https://www.youtube.com/@yourhandle) and run:
    uvicorn app:app --host 0.0.0.0 --port 8000

Only videos that belong to that channel can be listed or downloaded.
"""
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

CHANNEL_URL = os.getenv("CHANNEL_URL", "").rstrip("/")
MAX_VIDEOS = int(os.getenv("MAX_VIDEOS", "100"))
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "2"))
CACHE_SECONDS = 600

if not CHANNEL_URL:
    raise SystemExit("Set CHANNEL_URL, e.g. CHANNEL_URL=https://www.youtube.com/@yourhandle")

app = FastAPI(title="Channel downloads")
_slots = threading.Semaphore(MAX_PARALLEL)
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()

YT_URL = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)


def load_channel():
    """Channel name, id and recent uploads, cached for a few minutes."""
    with _lock:
        if _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
        opts = {"quiet": True, "extract_flat": True, "playlistend": MAX_VIDEOS}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(CHANNEL_URL + "/videos", download=False)
        except Exception:
            raise HTTPException(502, "Couldn't load the channel. Try again in a minute.")
        videos = [
            {
                "id": e["id"],
                "title": e.get("title") or "Untitled",
                "duration": e.get("duration"),
                "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
            }
            for e in (info.get("entries") or [])
            if e and e.get("id")
        ]
        _cache["data"] = {
            "channel": info.get("channel") or info.get("uploader") or "Channel",
            "channel_id": info.get("channel_id"),
            "videos": videos,
        }
        _cache["at"] = time.time()
        return _cache["data"]


def owned_video_info(url_or_id: str) -> dict:
    """Fetch video metadata and make sure it belongs to the configured channel."""
    channel_id = load_channel()["channel_id"]
    target = url_or_id if "/" in url_or_id else f"https://www.youtube.com/watch?v={url_or_id}"
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(target, download=False)
    except Exception:
        raise HTTPException(404, "Couldn't find that video.")
    if not channel_id or info.get("channel_id") != channel_id:
        raise HTTPException(403, "That video isn't from this channel.")
    return info


@app.get("/api/videos")
def videos():
    data = load_channel()
    return {"channel": data["channel"], "videos": data["videos"]}


@app.get("/api/check")
def check(url: str = Query(..., max_length=300)):
    if not YT_URL.match(url):
        raise HTTPException(400, "Paste a YouTube video link.")
    info = owned_video_info(url)
    heights = sorted(
        {
            f["height"]
            for f in info.get("formats", [])
            if f.get("height") and f.get("vcodec") not in (None, "none")
        },
        reverse=True,
    )
    return {
        "id": info["id"],
        "title": info.get("title"),
        "duration": info.get("duration"),
        "heights": heights,
        "thumb": f"https://i.ytimg.com/vi/{info['id']}/mqdefault.jpg",
    }


@app.get("/api/download")
def download(
    background: BackgroundTasks,
    id: str = Query(..., pattern=r"^[\w-]{11}$"),
    fmt: str = Query("mp4", pattern="^(mp4|mp3)$"),
    height: int = Query(1080, ge=144, le=2160),
):
    owned_video_info(id)

    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "Lots of downloads right now. Try again in a minute.")

    tmp = tempfile.mkdtemp(prefix="dl_")
    try:
        opts = {
            "quiet": True,
            "noplaylist": True,
            "outtmpl": f"{tmp}/%(title).120B [%(id)s].%(ext)s",
        }
        if fmt == "mp3":
            opts.update(
                format="bestaudio/best",
                postprocessors=[
                    {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                ],
            )
        else:
            opts.update(
                format=(
                    f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]"
                    f"/b[height<={height}][ext=mp4]/b[height<={height}]/b"
                ),
                merge_output_format="mp4",
            )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={id}"])

        files = sorted(Path(tmp).iterdir())
        if not files:
            raise RuntimeError("no output")
        path = files[0]
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, "The download failed. Try again.")
    finally:
        _slots.release()

    background.add_task(shutil.rmtree, tmp, True)
    return FileResponse(path, filename=path.name)


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
