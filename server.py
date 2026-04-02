"""
StreamBox - Personal Media Streaming Server
Phase 2: FFmpeg on-the-fly transcoding for .mkv, .avi, .mov, and more

Usage:
    pkg install python ffmpeg   (Termux)
    pip install flask
    python server.py

    Then open http://localhost:5000 in your browser.
    Remote access: http://<your-ip>:5000
"""

import os
import json
import html
import shutil
import subprocess
import mimetypes
from pathlib import Path
from flask import Flask, Response, request, abort, render_template, redirect, url_for

app = Flask(__name__)

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
MEDIA_ROOT        = Path("./media")
CHUNK_SIZE        = 1024 * 1024   # 1 MB read chunks for native files
TRANSCODE_BITRATE = "1500k"       # Video bitrate (lower = less CPU on phone)
AUDIO_BITRATE     = "128k"        # Audio bitrate
HIDDEN_NAMES      = {".gitkeep"}

# Extensions the browser CANNOT play natively → must transcode
TRANSCODE_EXTS = {
    ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".m4v", ".3gp", ".ts",  ".vob", ".divx",
    ".rm",  ".rmvb", ".asf", ".f4v",
}

# Extensions the browser CAN play natively → serve with range requests
NATIVE_VIDEO_EXTS = {".mp4", ".webm", ".ogv"}
NATIVE_AUDIO_EXTS = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac", ".opus"}


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def safe_path(relative: str) -> Path:
    """Resolve path and block directory traversal attacks."""
    base   = MEDIA_ROOT.resolve()
    target = (base / relative).resolve()
    if not str(target).startswith(str(base)):
        abort(403)
    if target.name in HIDDEN_NAMES:
        abort(404)
    if not target.exists():
        abort(404)
    return target


def get_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def needs_transcode(path: Path) -> bool:
    return path.suffix.lower() in TRANSCODE_EXTS


def is_native_streamable(path: Path, mime: str) -> bool:
    ext = path.suffix.lower()
    return (
        ext in NATIVE_VIDEO_EXTS or
        ext in NATIVE_AUDIO_EXTS or
        mime.startswith(("image/", "text/"))
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def get_video_duration(path: Path):
    """Use ffprobe to get video duration in seconds."""
    if not ffprobe_available():
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=10
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return None


def seconds_to_hms(seconds: float) -> str:
    """Convert float seconds to HH:MM:SS.mmm for FFmpeg -ss argument."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def format_size(num_bytes: int) -> str:
    if num_bytes < 1_048_576:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1_048_576:.1f} MB"


def display_title(path: Path) -> str:
    return path.stem or path.name


def media_kind(path: Path, mime: str) -> str:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/") or path.suffix.lower() in TRANSCODE_EXTS:
        return "video"
    return "file"


def build_media_context(rel_path: str, abs_path: Path) -> dict:
    mime = get_mime(abs_path)
    transcode = needs_transcode(abs_path)
    native = is_native_streamable(abs_path, mime)
    streamable = transcode or native
    parent = abs_path.parent.relative_to(MEDIA_ROOT.resolve())
    parent_path = "" if str(parent) == "." else str(parent)
    kind = media_kind(abs_path, mime)

    return {
        "name": abs_path.name,
        "title": display_title(abs_path),
        "path": rel_path,
        "parent_path": parent_path,
        "size": format_size(abs_path.stat().st_size),
        "format": (abs_path.suffix[1:] if abs_path.suffix else "file").upper(),
        "mime": mime,
        "streamable": streamable,
        "transcode": transcode,
        "is_image": kind == "image",
        "is_audio": kind == "audio",
        "is_video": kind == "video",
    }


def placeholder_thumbnail(label: str, tone: str) -> Response:
    safe_label = html.escape(label[:24] or "MEDIA")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1a201d" />
      <stop offset="100%" stop-color="#0c0f0d" />
    </linearGradient>
  </defs>
  <rect width="640" height="360" rx="32" fill="url(#bg)" />
  <circle cx="512" cy="96" r="132" fill="{tone}" opacity="0.18" />
  <circle cx="120" cy="320" r="160" fill="#d8ff5a" opacity="0.08" />
  <rect x="60" y="60" width="170" height="40" rx="20" fill="rgba(255,255,255,0.08)" />
  <text x="84" y="87" fill="#c9cec6" font-size="22" font-family="'IBM Plex Mono', monospace">StreamBox</text>
  <text x="60" y="210" fill="#f3f5f1" font-size="74" font-family="'Space Grotesk', sans-serif" font-weight="700">{safe_label}</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


# ─── STREAMING: NATIVE FILES (Range Request) ──────────────────────────────────

def stream_native(path: Path, mime: str) -> Response:
    """
    Serve a natively-playable file with HTTP Range Request support.
    Enables video seeking, audio scrubbing, and image inline display.
    """
    file_size    = path.stat().st_size
    range_header = request.headers.get("Range")

    if range_header:
        try:
            byte_range         = range_header.strip().split("=")[1]
            start_str, end_str = byte_range.split("-")
            start = int(start_str)
            end   = int(end_str) if end_str else file_size - 1
        except (ValueError, IndexError):
            abort(416)

        if start >= file_size or end >= file_size or start > end:
            abort(416)

        length = end - start + 1

        def generate():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return Response(generate(), status=206, headers={
            "Content-Range":       f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges":       "bytes",
            "Content-Length":      str(length),
            "Content-Type":        mime,
            "Content-Disposition": "inline",
        })

    else:
        def generate():
            with open(path, "rb") as f:
                while chunk := f.read(CHUNK_SIZE):
                    yield chunk

        return Response(generate(), status=200, headers={
            "Accept-Ranges":       "bytes",
            "Content-Length":      str(file_size),
            "Content-Type":        mime,
            "Content-Disposition": "inline",
        })


# ─── STREAMING: TRANSCODED FILES (FFmpeg pipe) ────────────────────────────────

def stream_transcode(path: Path, start_seconds: float = 0.0) -> Response:
    """
    Transcode a non-native video on-the-fly using FFmpeg.

    What happens here:
      1. We spawn FFmpeg as a subprocess.
      2. FFmpeg reads the source file, decodes it, and re-encodes to
         H.264/AAC inside a *fragmented* MP4 container.
      3. Fragmented MP4 (frag_keyframe + empty_moov) doesn't need a
         complete moov atom upfront — it streams chunk-by-chunk.
      4. We forward FFmpeg's stdout to the HTTP response as it arrives.
      5. If start_seconds > 0 (user seeked), we pass -ss to FFmpeg so
         it begins encoding from that timestamp.
    """
    if not ffmpeg_available():
        abort(503)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    # Input-side seek (fast: skips decoding the skipped portion)
    if start_seconds > 0:
        cmd += ["-ss", seconds_to_hms(start_seconds)]

    cmd += [
        "-i", str(path),

        # Video
        "-c:v", "libx264",
        "-preset", "ultrafast",     # Fastest encode — important on phone CPU
        "-tune", "zerolatency",     # Minimize encode latency
        "-b:v", TRANSCODE_BITRATE,
        "-maxrate", TRANSCODE_BITRATE,
        "-bufsize", "3000k",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # Even dimensions required by H.264

        # Audio
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,
        "-ac", "2",

        # Output: fragmented MP4 piped to stdout
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "pipe:1",
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def generate():
        try:
            while True:
                chunk = process.stdout.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        finally:
            # Clean up FFmpeg when client disconnects or stream ends
            process.stdout.close()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

    return Response(generate(), status=200, headers={
        "Content-Type":        "video/mp4",
        "Content-Disposition": "inline",
        "X-Accel-Buffering":   "no",
        "Cache-Control":       "no-cache",
    })


# ─── DIRECTORY LISTING ────────────────────────────────────────────────────────

def list_directory(rel_path: str, abs_path: Path):
    entries = []

    if rel_path not in ("", "/", "."):
        parent = str(Path(rel_path).parent)
        entries.append({
            "name": ".. (up)", "path": parent, "is_dir": True,
            "size": "", "mime": "", "icon": "folder-up",
            "streamable": False, "transcode": False,
        })

    visible_items = [item for item in abs_path.iterdir() if item.name not in HIDDEN_NAMES]

    for item in sorted(visible_items, key=lambda x: (not x.is_dir(), x.name.lower())):
        mime       = get_mime(item) if item.is_file() else ""
        transcode  = needs_transcode(item) if item.is_file() else False
        native     = is_native_streamable(item, mime) if item.is_file() else False
        streamable = transcode or native

        size = ""
        if item.is_file():
            b    = item.stat().st_size
            size = f"{b / 1024:.1f} KB" if b < 1_048_576 else f"{b / 1_048_576:.1f} MB"

        item_rel = str(Path(rel_path) / item.name) if rel_path else item.name

        entries.append({
            "name":       item.name,
            "path":       item_rel,
            "is_dir":     item.is_dir(),
            "size":       size,
            "mime":       "video/mp4" if transcode else mime,
            "icon":       _icon(item, mime),
            "streamable": streamable,
            "transcode":  transcode,
            "thumbable":  item.is_file() and (mime.startswith("image/") or item.suffix.lower() in NATIVE_VIDEO_EXTS),
        })

    return entries


def _icon(item: Path, mime: str) -> str:
    if item.is_dir():
        return "folder"
    ext = item.suffix.lower()
    if mime.startswith("video/") or ext in TRANSCODE_EXTS:
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("text/"):
        return "text"
    return "file"


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/", defaults={"rel_path": ""})
@app.route("/browse/<path:rel_path>")
def browse(rel_path):
    if rel_path:
        abs_path = safe_path(rel_path)
        if abs_path.is_file():
            return redirect(f"/stream/{rel_path}")
    else:
        abs_path = MEDIA_ROOT.resolve()
        abs_path.mkdir(parents=True, exist_ok=True)

    entries    = list_directory(rel_path, abs_path)
    ffmpeg_ok  = ffmpeg_available()
    return render_template(
        "index.html", entries=entries,
        page_mode="browse",
        media=None,
        related_entries=[],
        current_path=rel_path or "/",
        ffmpeg_ok=ffmpeg_ok
    )


@app.route("/stream/<path:rel_path>")
def stream(rel_path):
    """
    Smart streaming endpoint:
      - Native formats    → range-request streaming (full browser seeking)
      - Transcode formats → FFmpeg pipe (seekable via ?t=<seconds>)
    """
    abs_path = safe_path(rel_path)
    if not abs_path.is_file():
        abort(400)

    if needs_transcode(abs_path):
        start_t = float(request.args.get("t", 0))
        return stream_transcode(abs_path, start_seconds=start_t)

    mime = get_mime(abs_path)
    if is_native_streamable(abs_path, mime):
        return stream_native(abs_path, mime)

    # Non-streamable: force download
    response = stream_native(abs_path, mime)
    response.headers["Content-Disposition"] = f'attachment; filename="{abs_path.name}"'
    return response


@app.route("/info/<path:rel_path>")
def info(rel_path):
    """Returns video duration via ffprobe — used by the seek bar UI."""
    abs_path = safe_path(rel_path)
    duration = get_video_duration(abs_path)
    return {"duration": duration}


@app.route("/view/<path:rel_path>")
def view_media(rel_path):
    abs_path = safe_path(rel_path)
    if abs_path.is_dir():
        return redirect(url_for("browse", rel_path=rel_path))

    media = build_media_context(rel_path, abs_path)
    if not media["streamable"]:
        return redirect(url_for("stream", rel_path=rel_path))

    sibling_entries = list_directory(media["parent_path"], abs_path.parent)
    related_entries = [
        entry for entry in sibling_entries
        if entry["streamable"] and not entry["is_dir"] and entry["path"] != rel_path
    ]

    return render_template(
        "index.html",
        page_mode="detail",
        entries=[],
        media=media,
        related_entries=related_entries,
        current_path=media["parent_path"] or "/",
        ffmpeg_ok=ffmpeg_available(),
    )


@app.route("/thumb/<path:rel_path>")
def thumb(rel_path):
    abs_path = safe_path(rel_path)
    if not abs_path.is_file():
        abort(400)

    mime = get_mime(abs_path)
    kind = media_kind(abs_path, mime)
    if kind == "image":
        return stream_native(abs_path, mime)

    if kind == "video" and ffmpeg_available():
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", "00:00:01.000",
            "-i", str(abs_path),
            "-frames:v", "1",
            "-vf", "scale=640:-2",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0 and result.stdout:
                return Response(result.stdout, mimetype="image/jpeg", headers={
                    "Cache-Control": "public, max-age=300",
                })
        except Exception:
            pass

    tone = "#79c9ff" if kind == "video" else "#72f1c1" if kind == "audio" else "#f0a4ff"
    return placeholder_thumbnail((abs_path.suffix[1:] if abs_path.suffix else kind).upper(), tone)


@app.post("/delete/<path:rel_path>")
def delete(rel_path):
    """Delete a file from the media directory."""
    abs_path = safe_path(rel_path)
    if not abs_path.is_file():
        abort(400)

    abs_path.unlink()

    parent = abs_path.parent.relative_to(MEDIA_ROOT.resolve())
    parent_path = "" if str(parent) == "." else str(parent)
    if request.headers.get("X-Requested-With") == "fetch":
        return {"ok": True, "parent": parent_path}
    return redirect(url_for("browse", rel_path=parent_path))


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    MEDIA_ROOT.mkdir(exist_ok=True)
    print("=" * 55)
    print("  StreamBox — Phase 2 (Transcoding)")
    print(f"  Serving:  {MEDIA_ROOT.resolve()}")
    print(f"  FFmpeg:   {'OK' if ffmpeg_available() else 'NOT FOUND — pkg install ffmpeg'}")
    print(f"  FFprobe:  {'OK' if ffprobe_available() else 'not found (seek bar disabled)'}")
    print("  Local:    http://localhost:5000")
    print("  Remote:   http://<your-ip>:5000")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
