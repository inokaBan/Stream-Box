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
import shutil
import subprocess
import mimetypes
from pathlib import Path
from flask import Flask, Response, request, abort, render_template_string, redirect, url_for

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
    return render_template_string(
        HTML_TEMPLATE, entries=entries,
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


# ─── HTML TEMPLATE ────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>StreamBox</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Space+Grotesk:wght@500;700&display=swap');

    :root {
      --bg: #0f1110;
      --bg-elevated: #171a18;
      --surface: #1d211f;
      --surface-soft: #242927;
      --surface-strong: #0b0d0c;
      --border: #303732;
      --border-soft: #262d28;
      --text: #f3f5f1;
      --text-soft: #c9cec6;
      --muted: #8b938a;
      --accent: #d8ff5a;
      --accent-strong: #bfe62f;
      --accent2: #79c9ff;
      --success-bg: #17251a;
      --success-fg: #89f3a6;
      --danger-bg: #2a1d1a;
      --danger-fg: #ff9c7b;
      --shadow: 0 20px 50px rgba(0, 0, 0, 0.28);
      --radius-sm: 8px;
      --radius: 14px;
      --radius-lg: 20px;
      --space-1: 4px;
      --space-2: 8px;
      --space-3: 12px;
      --space-4: 16px;
      --space-5: 20px;
      --space-6: 24px;
      --space-7: 32px;
      --space-8: 40px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background:
        radial-gradient(circle at top, rgba(216, 255, 90, 0.08), transparent 32%),
        linear-gradient(180deg, #121513 0%, var(--bg) 22%, #0c0e0d 100%);
      color: var(--text);
      font-family: 'IBM Plex Mono', monospace;
      min-height: 100vh;
      line-height: 1.5;
    }

    header {
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(18px);
      background: rgba(15, 17, 16, 0.82);
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      padding: var(--space-5) var(--space-7);
      display: flex;
      align-items: center;
      gap: var(--space-4);
    }
    .logo {
      font-family: 'Space Grotesk', sans-serif;
      font-weight: 700;
      font-size: 1.45rem;
      letter-spacing: -0.04em;
      line-height: 1;
    }
    .logo span { color: var(--accent); }
    .header-right {
      margin-left: auto;
      display: flex;
      align-items: center;
      gap: var(--space-3);
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .breadcrumb {
      font-size: 0.76rem;
      color: var(--muted);
      padding: 10px 12px;
      border: 1px solid var(--border-soft);
      border-radius: var(--radius-sm);
      background: rgba(255, 255, 255, 0.03);
      max-width: min(100%, 480px);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .breadcrumb b { color: var(--text-soft); font-weight: 500; }
    .ffmpeg-badge {
      font-size: 0.64rem;
      padding: 9px 10px;
      border-radius: 999px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 500;
    }
    .ffmpeg-ok  { background: var(--success-bg); color: var(--success-fg); border: 1px solid #274230; }
    .ffmpeg-off { background: var(--danger-bg); color: var(--danger-fg); border: 1px solid #523129; }
    .view-toggle {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px;
      border: 1px solid rgba(255, 255, 255, 0.04);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.015);
    }
    .view-btn {
      border: none;
      background: transparent;
      color: var(--muted);
      width: 32px;
      height: 32px;
      padding: 0;
      border-radius: 999px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      opacity: 0.72;
      transition: background 0.1s, color 0.1s, opacity 0.1s, box-shadow 0.1s;
    }
    .view-btn svg {
      width: 14px;
      height: 14px;
      fill: currentColor;
    }
    .view-btn:hover {
      opacity: 0.95;
      color: var(--text-soft);
    }
    .view-btn.active {
      opacity: 1;
      color: var(--text-soft);
      background: rgba(255, 255, 255, 0.05);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.07);
    }

    main {
      padding: var(--space-8) var(--space-7) 56px;
      max-width: 1080px;
      margin: 0 auto;
    }

    /* ── Player wrapper ── */
    .player-wrap {
      display: none;
      margin-bottom: var(--space-7);
      position: relative;
      background:
        radial-gradient(circle at top, rgba(120, 178, 255, 0.14), transparent 40%),
        linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0));
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 28px;
      overflow: hidden;
      box-shadow: 0 30px 80px rgba(0, 0, 0, 0.42);
      isolation: isolate;
    }
    .player-wrap.active { display: block; }
    .player-wrap.player-fullscreen {
      width: 100vw;
      height: 100vh;
      border-radius: 0;
      margin: 0;
      border: 0;
      background: #000;
    }

    .player-stage {
      position: relative;
      background:
        radial-gradient(circle at center, rgba(255,255,255,0.08), transparent 55%),
        #040506;
      min-height: min(60vh, 760px);
    }
    .player-wrap.player-fullscreen .player-stage {
      min-height: 100vh;
      height: 100vh;
    }

    /* The actual <video> element — no native controls */
    #main-video,
    #img-viewer {
      width: 100%;
      display: block;
      max-height: 76vh;
      min-height: min(60vh, 760px);
      object-fit: contain;
      background: #000;
    }
    #img-viewer { display: none; }
    .player-wrap.player-fullscreen #main-video,
    .player-wrap.player-fullscreen #img-viewer {
      max-height: 100vh;
      min-height: 100vh;
    }

    .player-overlay {
      position: absolute;
      inset: 0;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      pointer-events: none;
      opacity: 1;
      transition: opacity 0.24s ease;
    }
    .player-wrap.chrome-hidden .player-overlay {
      opacity: 0;
    }
    .player-wrap.chrome-hidden {
      cursor: none;
    }

    .player-top,
    .player-bottom {
      pointer-events: auto;
      padding: 18px 22px;
      display: flex;
      align-items: center;
      gap: 14px;
      background: linear-gradient(180deg, rgba(0,0,0,0.72), rgba(0,0,0,0));
    }
    .player-bottom {
      flex-direction: column;
      align-items: stretch;
      gap: 16px;
      background: linear-gradient(0deg, rgba(0,0,0,0.84), rgba(0,0,0,0));
    }

    .player-title-group {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .player-eyebrow {
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      color: rgba(255,255,255,0.56);
    }
    .now-playing {
      font-size: clamp(1rem, 1.5vw, 1.18rem);
      color: #fff;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 500;
      max-width: min(70vw, 720px);
    }

    .player-pill {
      border: 1px solid rgba(255, 255, 255, 0.14);
      background: rgba(255, 255, 255, 0.08);
      color: rgba(255,255,255,0.9);
      border-radius: 999px;
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.18s ease, background 0.18s ease, border-color 0.18s ease;
    }
    .player-pill:hover {
      transform: translateY(-1px) scale(1.01);
      background: rgba(255,255,255,0.13);
      border-color: rgba(255,255,255,0.22);
    }

    .close-btn,
    .hud-btn,
    .transport-btn {
      cursor: pointer;
    }

    .close-btn,
    .hud-btn {
      width: 42px;
      height: 42px;
      padding: 0;
    }
    .close-btn {
      margin-left: auto;
      font-size: 1rem;
    }
    .hud-btn svg,
    .transport-btn svg {
      width: 20px;
      height: 20px;
      fill: currentColor;
    }

    .player-center {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 18px;
      pointer-events: auto;
      padding: 0 20px;
    }

    .transport-btn {
      width: 64px;
      height: 64px;
      padding: 0;
      color: #fff;
    }
    .transport-btn.primary {
      width: 78px;
      height: 78px;
      background: rgba(255,255,255,0.16);
      border-color: rgba(255,255,255,0.2);
    }
    .transport-btn svg {
      width: 24px;
      height: 24px;
    }
    .transport-btn.primary svg {
      width: 28px;
      height: 28px;
    }

    .time-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      color: rgba(255,255,255,0.82);
      font-size: 0.78rem;
      font-variant-numeric: tabular-nums;
      letter-spacing: 0.04em;
    }
    .time-meta {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }
    .time-label {
      white-space: nowrap;
      color: rgba(255,255,255,0.76);
    }
    .player-status {
      font-size: 0.68rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.52);
      white-space: nowrap;
    }

    .progress-row {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .prog-track {
      position: relative;
      flex: 1;
      height: 18px;
      cursor: pointer;
      display: flex;
      align-items: center;
    }
    .prog-rail {
      position: absolute;
      inset: 50% 0 auto;
      height: 6px;
      transform: translateY(-50%);
      border-radius: 999px;
      background: rgba(255,255,255,0.16);
      overflow: hidden;
    }
    .prog-buf,
    .prog-fill {
      position: absolute;
      top: 0;
      left: 0;
      height: 100%;
      border-radius: inherit;
      pointer-events: none;
    }
    .prog-buf { background: rgba(255,255,255,0.28); }
    .prog-fill {
      background: linear-gradient(90deg, #ffffff, #d9e9ff);
      box-shadow: 0 0 28px rgba(255,255,255,0.2);
    }
    .prog-thumb {
      position: absolute;
      top: 50%;
      width: 14px;
      height: 14px;
      margin-left: -7px;
      transform: translateY(-50%);
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 0 0 4px rgba(255,255,255,0.12);
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.18s ease, transform 0.18s ease;
    }
    .prog-track:hover .prog-thumb,
    .prog-track.dragging .prog-thumb {
      opacity: 1;
      transform: translateY(-50%) scale(1.04);
    }

    .bottom-controls {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    .bottom-left,
    .bottom-right {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }

    .hud-btn {
      width: 46px;
      height: 46px;
      padding: 0;
    }

    .vol-group {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 12px;
      min-height: 46px;
    }
    .vol-slider,
    .tc-slider {
      width: 108px;
      accent-color: #ffffff;
      cursor: pointer;
    }

    .tc-row {
      display: none;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(255,255,255,0.06);
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      flex-wrap: wrap;
    }
    .tc-row.show { display: flex; }
    .tc-label {
      font-size: 0.72rem;
      color: rgba(255,255,255,0.72);
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .tc-slider { flex: 1; min-width: 140px; }
    .tc-btn {
      min-height: 40px;
      padding: 0 16px;
      font-size: 0.74rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    /* ── File table ── */
    .view-panel { display: none; }
    .view-panel.active { display: block; }

    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      background: rgba(23, 26, 24, 0.84);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    thead th {
      font-size: 0.64rem;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--muted);
      padding: 18px 18px 14px;
      text-align: left;
      border-bottom: 1px solid var(--border-soft);
      background: rgba(255, 255, 255, 0.02);
    }
    tbody tr { transition: background 0.1s; }
    tbody tr:not(:last-child) td { border-bottom: 1px solid var(--border-soft); }
    tbody tr:hover { background: rgba(255, 255, 255, 0.03); }
    td { padding: 16px 18px; font-size: 0.82rem; vertical-align: middle; }

    .icon {
      width: 34px; height: 34px; border-radius: 10px;
      display: inline-flex; align-items: center; justify-content: center;
      font-size: 0.64rem; font-weight: 500; margin-right: 12px; vertical-align: middle;
      letter-spacing: 0.04em;
    }
    .icon-folder, .icon-folder-up { background: #34291d; color: #f0b15a; }
    .icon-video  { background: #182633; color: var(--accent2); }
    .icon-audio  { background: #173027; color: #72f1c1; }
    .icon-image  { background: #302037; color: #f0a4ff; }
    .icon-text, .icon-file { background: #222725; color: var(--muted); }

    .name-link {
      color: var(--text);
      text-decoration: none;
      cursor: pointer;
      transition: color 0.1s;
    }
    .name-link:hover { color: var(--accent); }

    .badge {
      font-size: 0.6rem;
      padding: 4px 8px;
      border-radius: 999px;
      margin-left: 10px;
      vertical-align: middle;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .badge-native    { background: var(--success-bg); color: var(--success-fg); border: 1px solid #274230; }
    .badge-transcode { background: #1b2430; color: var(--accent2); border: 1px solid #2b4256; }
    .badge-dl        { background: #23201d; color: var(--text-soft); border: 1px solid #3a352f; }

    .size { color: var(--muted); font-size: 0.76rem; }
    .actions {
      display: flex;
      align-items: center;
      gap: 14px;
      justify-content: flex-start;
      flex-wrap: wrap;
    }
    td.actions {
      min-width: 190px;
    }
    .actions a,
    .actions button {
      color: var(--text-soft);
      font-size: 0.71rem;
      transition: color 0.1s;
    }
    .actions form,
    .card-actions form {
      display: inline-flex;
      align-items: center;
      margin: 0;
    }
    .actions a {
      text-decoration: none;
    }
    .actions button {
      background: transparent;
      border: none;
      padding: 0;
      font-family: inherit;
      cursor: pointer;
      text-decoration: underline;
      text-decoration-color: transparent;
      transition: color 0.1s;
    }
    .actions a:hover,
    .actions button:hover {
      color: var(--accent);
    }
    .actions button.danger:hover {
      color: var(--danger-fg);
    }

    /* ── Grid view ── */
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: var(--space-5);
    }
    .card {
      background: rgba(23, 26, 24, 0.84);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      min-height: 280px;
    }
    .card-preview {
      position: relative;
      aspect-ratio: 16 / 10;
      background:
        radial-gradient(circle at top, rgba(121, 201, 255, 0.16), transparent 45%),
        linear-gradient(180deg, #1a1f1c 0%, #101311 100%);
      border-bottom: 1px solid var(--border-soft);
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }
    .card-preview img,
    .card-preview video {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .card-preview video {
      pointer-events: none;
      background: #000;
    }
    .card-icon {
      font-family: 'Space Grotesk', sans-serif;
      font-size: 0.86rem;
      letter-spacing: 0.08em;
      color: var(--text-soft);
      padding: 12px 14px;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 999px;
      background: rgba(0, 0, 0, 0.22);
    }
    .card-body {
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      flex: 1;
    }
    .card-title {
      color: var(--text);
      text-decoration: none;
      font-size: 0.85rem;
      word-break: break-word;
    }
    .card-title:hover { color: var(--accent); }
    .card-meta {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--muted);
      font-size: 0.72rem;
    }
    .card-actions {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px 14px;
      margin-top: auto;
    }
    .card-actions a,
    .card-actions button {
      color: var(--text-soft);
      text-decoration: none;
      font-size: 0.71rem;
      background: transparent;
      border: none;
      padding: 0;
      font-family: inherit;
      cursor: pointer;
      transition: color 0.1s;
    }
    .card-actions a:hover,
    .card-actions button:hover {
      color: var(--accent);
    }
    .card-actions button.danger:hover {
      color: var(--danger-fg);
    }

    .empty {
      text-align: center;
      padding: 88px 24px;
      color: var(--muted);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      background: rgba(23, 26, 24, 0.84);
      box-shadow: var(--shadow);
    }
    .empty h2 {
      font-family: 'Space Grotesk', sans-serif;
      font-size: 1.35rem;
      letter-spacing: -0.03em;
      margin-bottom: 8px;
      color: var(--text);
    }
    .empty p { font-size: 0.8rem; }
    .empty code { color: var(--accent); }

    @media (max-width: 800px) {
      header,
      main {
        padding-left: var(--space-5);
        padding-right: var(--space-5);
      }

      header {
        align-items: flex-start;
        flex-direction: column;
      }

      .header-right {
        width: 100%;
        margin-left: 0;
        justify-content: flex-start;
      }

      .view-toggle {
        width: 100%;
        justify-content: space-between;
      }

      .view-btn { flex: 1; }

      .now-playing {
        margin-left: 0;
        max-width: 100%;
        width: 100%;
      }

      .btn-row {
        gap: var(--space-3);
      }

      .vol-group {
        width: 100%;
        justify-content: space-between;
      }

      .vol-slider,
      .tc-slider {
        width: 100%;
      }
    }

    @media (max-width: 640px) {
      main {
        padding-top: var(--space-6);
      }

      table, thead, tbody, tr, th, td {
        display: block;
      }

      thead {
        display: none;
      }

      table {
        background: transparent;
        border: none;
        box-shadow: none;
      }

      tbody {
        display: grid;
        gap: var(--space-4);
      }

      tbody tr {
        background: rgba(23, 26, 24, 0.84);
        border: 1px solid var(--border);
        border-radius: var(--radius);
        box-shadow: var(--shadow);
        overflow: hidden;
      }

      tbody tr:not(:last-child) td {
        border-bottom: none;
      }

      td {
        padding: 14px 16px;
      }

      td + td {
        border-top: 1px solid var(--border-soft);
      }

      .actions {
        min-width: 0;
      }
    }
  </style>
</head>
<body>

<header>
  <div class="logo">Stream<span>Box</span></div>
  <div class="header-right">
    <div class="view-toggle" aria-label="View mode">
      <button type="button" class="view-btn" data-view="list" onclick="setViewMode('list')" aria-label="List view" title="List view">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h4v4H4V6zm6 1h10v2H10V7zM4 14h4v4H4v-4zm6 1h10v2H10v-2z"/></svg>
      </button>
      <button type="button" class="view-btn active" data-view="grid" onclick="setViewMode('grid')" aria-label="Grid view" title="Grid view">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h7v7H4V4zm9 0h7v7h-7V4zM4 13h7v7H4v-7zm9 0h7v7h-7v-7z"/></svg>
      </button>
    </div>
    {% if ffmpeg_ok %}
      <span class="ffmpeg-badge ffmpeg-ok">✓ ffmpeg</span>
    {% else %}
      <span class="ffmpeg-badge ffmpeg-off">✗ ffmpeg missing</span>
    {% endif %}
    <div class="breadcrumb">path: <b>{{ current_path }}</b></div>
  </div>
</header>

<main>

  <!-- ── Player ── -->
  <div class="player-wrap" id="player-wrap">
    <div class="player-stage" id="player-stage">
      <video id="main-video" preload="metadata"></video>
      <img id="img-viewer" alt="preview"/>

      <div class="player-overlay" id="player-overlay">
        <div class="player-top">
          <div class="player-title-group">
            <span class="player-eyebrow" id="player-eyebrow">Now Playing</span>
            <span class="now-playing" id="now-playing"></span>
          </div>
          <button class="close-btn player-pill" onclick="closePlayer()" title="Close player">✕</button>
        </div>

        <div class="player-center" id="player-center">
          <button class="transport-btn player-pill" onclick="skip(-10)" title="Back 10 seconds">
            <svg viewBox="0 0 24 24"><path d="M11.99 5V1l-5 5 5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6h-2c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/><text x="7.5" y="15.5" font-size="5" fill="currentColor" font-family="monospace">10</text></svg>
          </button>
          <button class="transport-btn primary player-pill" id="btn-play" onclick="togglePlay()" title="Play or pause">
            <svg id="icon-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            <svg id="icon-pause" viewBox="0 0 24 24" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
          </button>
          <button class="transport-btn player-pill" onclick="skip(10)" title="Forward 10 seconds">
            <svg viewBox="0 0 24 24"><path d="M12.01 5V1l5 5-5 5V7c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6h2c0 4.42-3.58 8-8 8s-8-3.58-8-8 3.58-8 8-8z"/><text x="7.5" y="15.5" font-size="5" fill="currentColor" font-family="monospace">10</text></svg>
          </button>
        </div>

        <div class="player-bottom" id="ctrl-bar">
          <div class="time-row">
            <div class="time-meta">
              <span class="time-label" id="time-cur">0:00</span>
              <span class="time-label" id="time-dur">--:--</span>
            </div>
            <span class="player-status" id="player-status">Ready</span>
          </div>

          <div class="progress-row">
            <div class="prog-track" id="prog-track">
              <div class="prog-rail">
                <div class="prog-buf" id="prog-buf"></div>
                <div class="prog-fill" id="prog-fill"></div>
              </div>
              <div class="prog-thumb" id="prog-thumb"></div>
            </div>
          </div>

          <div class="bottom-controls">
            <div class="bottom-left">
              <button class="hud-btn player-pill" id="btn-mute" onclick="toggleMute()" title="Mute">
                <svg id="icon-vol-high" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm10.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zm2.5 0c0 2.42-1.38 4.5-3.39 5.51v2.04C17.66 18.44 20 15.5 20 12s-2.34-6.44-5.89-7.55v2.04C17.12 7.5 18.5 9.58 18.5 12z"/></svg>
                <svg id="icon-vol-low" viewBox="0 0 24 24" style="display:none"><path d="M3 9v6h4l5 5V4L7 9H3zm10.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>
                <svg id="icon-mute" viewBox="0 0 24 24" style="display:none"><path d="M4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73 4.27 3zM12 4 9.91 6.09 12 8.18V4z"/></svg>
              </button>
              <div class="vol-group player-pill">
                <input type="range" class="vol-slider" id="vol-slider"
                       min="0" max="1" step="0.05" value="1"
                       oninput="setVolume(this.value)"/>
              </div>
            </div>

            <div class="bottom-right">
              <button class="hud-btn player-pill" onclick="toggleFullscreen()" title="Toggle fullscreen">
                <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
              </button>
            </div>
          </div>

          <div class="tc-row" id="tc-row">
            <span class="tc-label">Transcode seek</span>
            <span class="tc-label" id="tc-cur">0:00</span>
            <input type="range" class="tc-slider" id="tc-slider"
                   min="0" max="100" value="0" step="1"
                   oninput="document.getElementById('tc-cur').textContent = formatTime(this.value)"/>
            <span class="tc-label" id="tc-dur">--:--</span>
            <button class="tc-btn player-pill" onclick="seekTranscode()">Go</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  {% if entries %}
  <section class="view-panel" id="view-list">
    <table>
      <thead><tr><th>name</th><th>size</th><th>actions</th></tr></thead>
      <tbody>
      {% for e in entries %}
        <tr data-entry-path="{{ e.path }}">
          <td>
            <span class="icon icon-{{ e.icon }}">
              {% if e.icon in ['folder','folder-up'] %}DIR
              {% elif e.icon == 'video' %}VID
              {% elif e.icon == 'audio' %}AUD
              {% elif e.icon == 'image' %}IMG
              {% elif e.icon == 'text' %}TXT
              {% else %}FILE{% endif %}
            </span>
            {% if e.is_dir %}
              <a class="name-link" href="/browse/{{ e.path }}">{{ e.name }}</a>
            {% else %}
              <a class="name-link"
                 {% if e.streamable %}onclick='openPlayer({{ e.path|tojson }}, {{ e.mime|tojson }}, {{ "true" if e.transcode else "false" }})'
                 {% else %}href="/stream/{{ e.path }}"{% endif %}>
                {{ e.name }}
              </a>
              {% if e.transcode %}<span class="badge badge-transcode">transcode</span>
              {% elif e.streamable %}<span class="badge badge-native">native</span>
              {% else %}<span class="badge badge-dl">download</span>{% endif %}
            {% endif %}
          </td>
          <td class="size">{{ e.size }}</td>
          <td class="actions">
            {% if not e.is_dir %}
              <a href="/stream/{{ e.path }}" download>↓ download</a>
              {% if e.streamable %}<a href="/stream/{{ e.path }}" target="_blank">⬡ raw</a>{% endif %}
              <form method="post" action="/delete/{{ e.path }}" class="delete-form" data-entry-path="{{ e.path }}" style="display:inline" onsubmit='return confirmDelete({{ e.name|tojson }})'>
                <button type="submit" class="danger">✕ delete</button>
              </form>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </section>

  <section class="view-panel active" id="view-grid">
    <div class="grid">
    {% for e in entries %}
      <article class="card" data-entry-path="{{ e.path }}">
        <div class="card-preview">
          {% if e.is_dir %}
            <div class="card-icon">DIR</div>
          {% elif e.thumbable and e.mime.startswith('image/') %}
            <img src="/stream/{{ e.path }}" alt="{{ e.name }}" loading="lazy"/>
          {% elif e.thumbable and e.mime.startswith('video/') %}
            <video src="/stream/{{ e.path }}" muted preload="metadata"></video>
          {% elif e.icon == 'audio' %}
            <div class="card-icon">AUD</div>
          {% elif e.icon == 'video' %}
            <div class="card-icon">VID</div>
          {% elif e.icon == 'image' %}
            <div class="card-icon">IMG</div>
          {% elif e.icon == 'text' %}
            <div class="card-icon">TXT</div>
          {% else %}
            <div class="card-icon">FILE</div>
          {% endif %}
        </div>
        <div class="card-body">
          {% if e.is_dir %}
            <a class="card-title" href="/browse/{{ e.path }}">{{ e.name }}</a>
          {% elif e.streamable %}
            <a class="card-title" href="#" onclick='openPlayer({{ e.path|tojson }}, {{ e.mime|tojson }}, {{ "true" if e.transcode else "false" }}); return false;'>{{ e.name }}</a>
          {% else %}
            <a class="card-title" href="/stream/{{ e.path }}">{{ e.name }}</a>
          {% endif %}
          <div class="card-meta">
            <span>{{ e.size or 'folder' }}</span>
            {% if not e.is_dir %}
              {% if e.transcode %}<span class="badge badge-transcode">transcode</span>
              {% elif e.streamable %}<span class="badge badge-native">native</span>
              {% else %}<span class="badge badge-dl">download</span>{% endif %}
            {% endif %}
          </div>
          {% if not e.is_dir %}
            <div class="card-actions">
              <a href="/stream/{{ e.path }}" download>↓ download</a>
              {% if e.streamable %}<a href="/stream/{{ e.path }}" target="_blank">⬡ raw</a>{% endif %}
              <form method="post" action="/delete/{{ e.path }}" class="delete-form" data-entry-path="{{ e.path }}" style="display:inline" onsubmit='return confirmDelete({{ e.name|tojson }})'>
                <button type="submit" class="danger">✕ delete</button>
              </form>
            </div>
          {% endif %}
        </div>
      </article>
    {% endfor %}
    </div>
  </section>
  {% else %}
  <div class="empty">
    <h2>No files found</h2>
    <p>Add files to the <code>./media</code> folder and refresh.</p>
  </div>
  {% endif %}

</main>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentPath  = null;
let isTranscoded = false;
const video      = document.getElementById('main-video');
const playerWrap = document.getElementById('player-wrap');
const playerStage = document.getElementById('player-stage');
const imgViewer = document.getElementById('img-viewer');
const progressTrack = document.getElementById('prog-track');
const progressFill = document.getElementById('prog-fill');
const progressBuf = document.getElementById('prog-buf');
const progressThumb = document.getElementById('prog-thumb');
const playerStatus = document.getElementById('player-status');
const nowPlaying = document.getElementById('now-playing');
const playerEyebrow = document.getElementById('player-eyebrow');
let hideChromeTimer = null;
let isScrubbing = false;
let wasPlayingBeforeScrub = false;
const VIEW_MODE_KEY = 'streambox:view-mode';

applySavedViewMode();

// ── Open player ────────────────────────────────────────────────────────────
async function openPlayer(path, mime, transcode) {
  currentPath  = path;
  isTranscoded = transcode;

  const ctrlBar = document.getElementById('ctrl-bar');
  const filename = path.split('/').pop();

  nowPlaying.textContent = filename;
  playerEyebrow.textContent = mime.startsWith('image/') ? 'Preview' : (transcode ? 'Streaming with Transcode' : 'Now Playing');
  playerStatus.textContent = transcode ? 'Transcoding' : 'Ready';
  playerWrap.classList.remove('chrome-hidden');
  clearHideChromeTimer();

  if (mime.startsWith('image/')) {
    video.pause();
    video.src = '';
    video.style.display  = 'none';
    imgViewer.style.display  = 'block';
    imgViewer.src            = '/stream/' + path;
    document.getElementById('player-center').style.display = 'none';
    ctrlBar.style.display = 'none';
  } else {
    video.style.display  = 'block';
    imgViewer.style.display  = 'none';
    document.getElementById('player-center').style.display = '';
    ctrlBar.style.display = '';
    loadVideo('/stream/' + path, mime, transcode);
    armChromeAutoHide();
  }

  playerWrap.classList.add('active');
  playerWrap.scrollIntoView({ behavior: 'smooth' });
}

// ── Load video source ───────────────────────────────────────────────────────
function loadVideo(url, mime, transcode) {
  playerStatus.textContent = transcode ? 'Transcoding' : 'Loading';
  progressFill.style.width = '0%';
  progressBuf.style.width = '0%';
  progressThumb.style.left = '0%';
  document.getElementById('time-cur').textContent = '0:00';
  video.src = url;
  video.load();
  video.play().catch(() => {});  // autoplay

  const tcRow = document.getElementById('tc-row');
  if (transcode) {
    tcRow.classList.add('show');
    // Load duration from ffprobe for the seek slider
    fetch('/info/' + currentPath).then(r => r.json()).then(data => {
      if (data.duration) {
        const sl = document.getElementById('tc-slider');
        sl.max   = Math.floor(data.duration);
        document.getElementById('tc-dur').textContent = formatTime(data.duration);
      }
    });
  } else {
    tcRow.classList.remove('show');
  }
}

// ── Playback controls ───────────────────────────────────────────────────────
function togglePlay() {
  if (!currentPath) return;
  video.paused ? video.play() : video.pause();
  armChromeAutoHide();
}

function skip(secs) {
  // For transcoded streams, skipping a little uses the existing stream
  // For big jumps use seekTranscode() via the slider
  if (!isTranscoded || Math.abs(secs) <= 30) {
    video.currentTime = Math.max(0, video.currentTime + secs);
  }
}

function setVolume(v) {
  video.volume = v;
  video.muted  = (v == 0);
  updateMuteIcon();
  armChromeAutoHide();
}

function toggleMute() {
  video.muted = !video.muted;
  document.getElementById('vol-slider').value = video.muted ? 0 : video.volume;
  updateMuteIcon();
  armChromeAutoHide();
}

function updateMuteIcon() {
  const showMute = video.muted || video.volume === 0;
  const showLow = !showMute && video.volume < 0.5;
  document.getElementById('icon-vol-high').style.display = (!showMute && !showLow) ? '' : 'none';
  document.getElementById('icon-vol-low').style.display  = showLow ? '' : 'none';
  document.getElementById('icon-mute').style.display     = showMute ? '' : 'none';
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    playerWrap.requestFullscreen().catch(() => video.requestFullscreen());
  } else {
    document.exitFullscreen();
  }
  armChromeAutoHide();
}

// Transcode seek — restarts FFmpeg from chosen timestamp
function seekTranscode() {
  if (!currentPath || !isTranscoded) return;
  const t = document.getElementById('tc-slider').value;
  loadVideo('/stream/' + currentPath + '?t=' + t, 'video/mp4', true);
  playerStatus.textContent = 'Jumped';
}

// ── Close ───────────────────────────────────────────────────────────────────
function closePlayer() {
  if (document.fullscreenElement === playerWrap) {
    document.exitFullscreen().catch(() => {});
  }
  video.pause();
  video.src = '';
  imgViewer.src = '';
  imgViewer.style.display = 'none';
  playerWrap.classList.remove('active', 'chrome-hidden', 'player-fullscreen');
  document.getElementById('ctrl-bar').style.display = '';
  document.getElementById('tc-row').classList.remove('show');
  progressFill.style.width = '0%';
  progressBuf.style.width  = '0%';
  progressThumb.style.left = '0%';
  document.getElementById('time-cur').textContent  = '0:00';
  document.getElementById('time-dur').textContent  = '--:--';
  playerStatus.textContent = 'Ready';
  clearHideChromeTimer();
  currentPath = null; isTranscoded = false;
}

// ── Progress bar update (runs every animationFrame) ─────────────────────────
video.addEventListener('timeupdate', () => {
  if (!video.duration) return;
  const pct = (video.currentTime / video.duration) * 100;
  progressFill.style.width = pct + '%';
  progressThumb.style.left = pct + '%';
  document.getElementById('time-cur').textContent  = formatTime(video.currentTime);
});

video.addEventListener('durationchange', () => {
  document.getElementById('time-dur').textContent = formatTime(video.duration);
});

// Buffered indicator
video.addEventListener('progress', () => {
  if (video.buffered.length && video.duration) {
    const end = video.buffered.end(video.buffered.length - 1);
    progressBuf.style.width = (end / video.duration * 100) + '%';
  }
});

// Play/pause icon sync
video.addEventListener('play',  () => {
  document.getElementById('icon-play').style.display  = 'none';
  document.getElementById('icon-pause').style.display = '';
  playerStatus.textContent = isTranscoded ? 'Streaming' : 'Playing';
  armChromeAutoHide();
});
video.addEventListener('pause', () => {
  document.getElementById('icon-play').style.display  = '';
  document.getElementById('icon-pause').style.display = 'none';
  playerStatus.textContent = 'Paused';
  showChrome();
});
video.addEventListener('waiting', () => {
  playerStatus.textContent = isTranscoded ? 'Buffering transcode' : 'Buffering';
});
video.addEventListener('ended', () => {
  playerStatus.textContent = 'Finished';
  showChrome();
});
video.addEventListener('loadedmetadata', () => {
  document.getElementById('time-dur').textContent = formatTime(video.duration);
});

// Click video to play/pause
video.addEventListener('click', togglePlay);

// ── Progress bar seek / scrub ───────────────────────────────────────────────
function updateProgressFromClientX(clientX) {
  if (!video.duration || isTranscoded) return;
  const rect = progressTrack.getBoundingClientRect();
  const pct = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
  progressFill.style.width = (pct * 100) + '%';
  progressThumb.style.left = (pct * 100) + '%';
  document.getElementById('time-cur').textContent = formatTime(video.duration * pct);
  video.currentTime = video.duration * pct;
}

progressTrack.addEventListener('pointerdown', (event) => {
  if (!video.duration || isTranscoded) return;
  isScrubbing = true;
  wasPlayingBeforeScrub = !video.paused;
  progressTrack.classList.add('dragging');
  showChrome();
  updateProgressFromClientX(event.clientX);
});

window.addEventListener('pointermove', (event) => {
  if (!isScrubbing) return;
  updateProgressFromClientX(event.clientX);
});

window.addEventListener('pointerup', () => {
  if (!isScrubbing) return;
  isScrubbing = false;
  progressTrack.classList.remove('dragging');
  if (wasPlayingBeforeScrub) {
    video.play().catch(() => {});
  }
  armChromeAutoHide();
});

// ── Player chrome behavior ──────────────────────────────────────────────────
function clearHideChromeTimer() {
  if (hideChromeTimer) {
    window.clearTimeout(hideChromeTimer);
    hideChromeTimer = null;
  }
}

function showChrome() {
  if (!playerWrap.classList.contains('active')) return;
  playerWrap.classList.remove('chrome-hidden');
  clearHideChromeTimer();
}

function armChromeAutoHide() {
  if (!playerWrap.classList.contains('active') || video.paused || isScrubbing || imgViewer.style.display === 'block') {
    return;
  }
  showChrome();
  hideChromeTimer = window.setTimeout(() => {
    if (!video.paused && !isScrubbing) {
      playerWrap.classList.add('chrome-hidden');
    }
  }, 2200);
}

['mousemove', 'pointermove', 'touchstart'].forEach((eventName) => {
  playerStage.addEventListener(eventName, armChromeAutoHide, { passive: true });
});

document.addEventListener('fullscreenchange', () => {
  playerWrap.classList.toggle('player-fullscreen', document.fullscreenElement === playerWrap);
});

document.addEventListener('keydown', (event) => {
  if (!playerWrap.classList.contains('active') || imgViewer.style.display === 'block') return;
  if (event.key === ' ' || event.key === 'Spacebar') {
    event.preventDefault();
    togglePlay();
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault();
    skip(-10);
  } else if (event.key === 'ArrowRight') {
    event.preventDefault();
    skip(10);
  } else if (event.key === 'f' || event.key === 'F' || event.key === 'Enter') {
    event.preventDefault();
    toggleFullscreen();
  } else if (event.key === 'm' || event.key === 'M') {
    event.preventDefault();
    toggleMute();
  } else if (event.key === 'Escape') {
    closePlayer();
  }
});

// ── Helpers ─────────────────────────────────────────────────────────────────
function formatTime(secs) {
  secs = Math.floor(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0
    ? h + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0')
    : m + ':' + String(s).padStart(2,'0');
}

function confirmDelete(name) {
  return window.confirm('Delete "' + name + '"? This cannot be undone.');
}

async function handleDeleteSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const path = form.dataset.entryPath;

  if (button) {
    button.disabled = true;
  }

  try {
    const response = await fetch(form.action, {
      method: 'POST',
      headers: { 'X-Requested-With': 'fetch' },
    });

    if (!response.ok) {
      throw new Error('Delete failed');
    }

    document.querySelectorAll('[data-entry-path="' + CSS.escape(path) + '"]').forEach((node) => {
      if (node.matches('tr, article.card')) {
        node.remove();
      }
    });

    const hasEntries = document.querySelector('[data-entry-path]');
    if (!hasEntries) {
      window.location.reload();
    }
  } catch (error) {
    window.location.reload();
  } finally {
    if (button) {
      button.disabled = false;
    }
  }
}

document.querySelectorAll('.delete-form').forEach((form) => {
  form.addEventListener('submit', handleDeleteSubmit);
});

function setViewMode(mode) {
  const normalized = mode === 'grid' ? 'grid' : 'list';
  localStorage.setItem(VIEW_MODE_KEY, normalized);
  applyViewMode(normalized);
}

function applySavedViewMode() {
  applyViewMode(localStorage.getItem(VIEW_MODE_KEY) || 'grid');
}

function applyViewMode(mode) {
  const showGrid = mode === 'grid';
  document.getElementById('view-list').classList.toggle('active', !showGrid);
  document.getElementById('view-grid').classList.toggle('active', showGrid);
  document.querySelectorAll('.view-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.view === mode);
  });
}
</script>
</body>
</html>"""


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
