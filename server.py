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

    main {
      padding: var(--space-8) var(--space-7) 56px;
      max-width: 1080px;
      margin: 0 auto;
    }

    /* ── Player wrapper ── */
    .player-wrap {
      display: none;
      margin-bottom: var(--space-7);
      background: linear-gradient(180deg, rgba(255,255,255,0.02), rgba(255,255,255,0));
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    .player-wrap.active { display: block; }

    /* The actual <video> element — no native controls */
    #main-video {
      width: 100%;
      display: block;
      max-height: 60vh;
      background: #000; cursor: pointer;
    }

    /* Image viewer */
    #img-viewer {
      display: none;
      width: 100%;
      max-height: 70vh;
      object-fit: contain; background: #000;
    }

    /* ── Custom controls bar ── */
    .ctrl-bar {
      background: linear-gradient(180deg, rgba(11, 13, 12, 0.95), rgba(17, 20, 18, 0.98));
      border-top: 1px solid var(--border-soft);
      padding: var(--space-5);
      display: none;
      flex-direction: column;
      gap: var(--space-4);
    }
    .ctrl-bar.show { display: flex; }

    /* Progress / seek row */
    .progress-row { display: flex; align-items: center; gap: var(--space-3); }
    .time-label {
      font-size: 0.72rem;
      color: var(--muted);
      white-space: nowrap;
      min-width: 42px;
      font-variant-numeric: tabular-nums;
    }
    .time-label.right { text-align: right; }

    /* Custom progress bar */
    .prog-track {
      flex: 1;
      height: 6px;
      background: #2a312d;
      border-radius: 999px;
      position: relative;
      cursor: pointer;
      transition: height 0.15s, transform 0.15s;
    }
    .prog-track:hover { height: 8px; }
    .prog-fill {
      height: 100%;
      background: linear-gradient(90deg, var(--accent-strong), var(--accent));
      border-radius: 999px;
      width: 0%; pointer-events: none; transition: width 0.1s linear;
    }
    .prog-buf {
      position: absolute; top: 0; left: 0; height: 100%;
      background: #465049; border-radius: 999px; pointer-events: none;
    }
    .prog-thumb {
      position: absolute; top: 50%; right: -5px;
      width: 12px; height: 12px; background: var(--accent);
      border-radius: 50%; transform: translateY(-50%);
      pointer-events: none; opacity: 0; transition: opacity 0.15s;
      box-shadow: 0 0 0 3px rgba(216, 255, 90, 0.16);
    }
    .prog-track:hover .prog-thumb { opacity: 1; }

    /* Buttons row */
    .btn-row { display: flex; align-items: center; gap: var(--space-2); flex-wrap: wrap; }

    .ctrl-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--text-soft);
      cursor: pointer;
      font-size: 1rem;
      min-width: 38px;
      min-height: 38px;
      padding: 0 10px;
      border-radius: 10px;
      display: flex; align-items: center; justify-content: center;
      transition: color 0.1s, background 0.1s, border-color 0.1s, transform 0.1s;
    }
    .ctrl-btn:hover {
      color: var(--text);
      background: rgba(255, 255, 255, 0.05);
      border-color: var(--border);
    }
    .ctrl-btn:active { transform: translateY(1px); }
    .ctrl-btn svg { width: 16px; height: 16px; fill: currentColor; }

    /* Volume group */
    .vol-group {
      display: flex;
      align-items: center;
      gap: var(--space-2);
      padding: 4px 6px 4px 2px;
      border: 1px solid var(--border-soft);
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.02);
    }
    .vol-slider {
      width: 88px; height: 3px; accent-color: var(--accent);
      cursor: pointer; opacity: 0.8;
    }
    .vol-slider:hover { opacity: 1; }

    /* Title + badge */
    .now-playing {
      font-size: 0.74rem; color: var(--muted);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      max-width: 360px; margin-left: auto;
    }
    .now-playing b { color: var(--text); font-weight: 500; }

    /* Transcode seek row (shown only for .mkv etc.) */
    .tc-row {
      display: none;
      align-items: center;
      gap: var(--space-3);
      padding-top: var(--space-4);
      border-top: 1px solid var(--border-soft);
      flex-wrap: wrap;
    }
    .tc-row.show { display: flex; }
    .tc-label {
      font-size: 0.68rem;
      color: var(--muted);
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .tc-slider { flex: 1; accent-color: var(--accent2); cursor: pointer; }
    .tc-btn {
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      font-family: 'IBM Plex Mono', monospace;
      font-size: 0.68rem;
      padding: 8px 12px;
      border-radius: 10px;
      cursor: pointer;
      transition: border-color 0.1s, color 0.1s, background 0.1s;
    }
    .tc-btn:hover { border-color: var(--accent2); color: var(--accent2); background: var(--surface-soft); }

    /* Close button */
    .player-close-row {
      display: flex; justify-content: flex-end;
      padding: 6px 14px 0;
    }
    .close-btn {
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      cursor: pointer;
      font-size: 0.7rem;
      font-family: 'IBM Plex Mono', monospace;
      padding: 8px 10px;
      border-radius: 10px;
      transition: color 0.1s, background 0.1s, border-color 0.1s;
    }
    .close-btn:hover {
      color: var(--text);
      background: rgba(255, 255, 255, 0.04);
      border-color: var(--border);
    }

    /* ── File table ── */
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
      white-space: nowrap;
    }
    .actions a,
    .actions button {
      color: var(--text-soft);
      font-size: 0.71rem;
      margin-right: 14px;
      margin-left: 0;
      transition: color 0.1s;
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

      .actions a,
      .actions button {
        display: inline-block;
        margin-right: 16px;
        margin-bottom: 4px;
      }
    }
  </style>
</head>
<body>

<header>
  <div class="logo">Stream<span>Box</span></div>
  <div class="header-right">
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

    <!-- Video element (no controls attr — we build our own) -->
    <video id="main-video" preload="metadata"></video>
    <!-- Image viewer -->
    <img id="img-viewer" alt="preview"/>

    <!-- Custom controls -->
    <div class="ctrl-bar" id="ctrl-bar">

      <!-- Close + now playing -->
      <div style="display:flex;align-items:center;gap:8px;">
        <button class="close-btn" onclick="closePlayer()">✕ close</button>
        <span class="now-playing" id="now-playing"></span>
      </div>

      <!-- Progress bar -->
      <div class="progress-row">
        <span class="time-label" id="time-cur">0:00</span>
        <div class="prog-track" id="prog-track">
          <div class="prog-buf"   id="prog-buf"></div>
          <div class="prog-fill"  id="prog-fill">
            <div class="prog-thumb"></div>
          </div>
        </div>
        <span class="time-label right" id="time-dur">--:--</span>
      </div>

      <!-- Buttons row -->
      <div class="btn-row">

        <!-- Play/Pause -->
        <button class="ctrl-btn" id="btn-play" onclick="togglePlay()" title="Play/Pause">
          <svg id="icon-play" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
          <svg id="icon-pause" viewBox="0 0 24 24" style="display:none"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
        </button>

        <!-- Skip back 10s -->
        <button class="ctrl-btn" onclick="skip(-10)" title="-10s">
          <svg viewBox="0 0 24 24"><path d="M11.99 5V1l-5 5 5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6h-2c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/><text x="7.5" y="15.5" font-size="5" fill="currentColor" font-family="monospace">10</text></svg>
        </button>

        <!-- Skip forward 10s -->
        <button class="ctrl-btn" onclick="skip(10)" title="+10s">
          <svg viewBox="0 0 24 24"><path d="M12.01 5V1l5 5-5 5V7c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6h2c0 4.42-3.58 8-8 8s-8-3.58-8-8 3.58-8 8-8z"/><text x="7.5" y="15.5" font-size="5" fill="currentColor" font-family="monospace">10</text></svg>
        </button>

        <!-- Volume -->
        <div class="vol-group">
          <button class="ctrl-btn" id="btn-mute" onclick="toggleMute()" title="Mute">
            <svg id="icon-vol" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>
            <svg id="icon-mute" viewBox="0 0 24 24" style="display:none"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>
          </button>
          <input type="range" class="vol-slider" id="vol-slider"
                 min="0" max="1" step="0.05" value="1"
                 oninput="setVolume(this.value)"/>
        </div>

        <!-- Fullscreen -->
        <button class="ctrl-btn" onclick="toggleFullscreen()" title="Fullscreen" style="margin-left:auto">
          <svg viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
        </button>

      </div>

      <!-- Transcode seek row (only for .mkv etc.) -->
      <div class="tc-row" id="tc-row">
        <span class="tc-label">jump to:</span>
        <span class="tc-label" id="tc-cur">0:00</span>
        <input type="range" class="tc-slider" id="tc-slider"
               min="0" max="100" value="0" step="1"
               oninput="document.getElementById('tc-cur').textContent = formatTime(this.value)"/>
        <span class="tc-label" id="tc-dur">--:--</span>
        <button class="tc-btn" onclick="seekTranscode()">⏎ go</button>
      </div>

    </div><!-- /ctrl-bar -->
  </div><!-- /player-wrap -->

  {% if entries %}
  <table>
    <thead><tr><th>name</th><th>size</th><th>actions</th></tr></thead>
    <tbody>
    {% for e in entries %}
      <tr>
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
               {% if e.streamable %}onclick="openPlayer('{{ e.path }}', '{{ e.mime }}', {{ 'true' if e.transcode else 'false' }})"
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
            <form method="post" action="/delete/{{ e.path }}" style="display:inline" onsubmit="return confirmDelete('{{ e.name|replace(\"'\", \"\\\\'\") }}')">
              <button type="submit" class="danger">✕ delete</button>
            </form>
          {% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
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

// ── Open player ────────────────────────────────────────────────────────────
async function openPlayer(path, mime, transcode) {
  currentPath  = path;
  isTranscoded = transcode;

  const wrap   = document.getElementById('player-wrap');
  const ctrlBar = document.getElementById('ctrl-bar');
  const imgEl  = document.getElementById('img-viewer');
  const nowPlaying = document.getElementById('now-playing');

  // Filename for display
  nowPlaying.innerHTML = '<b>' + path.split('/').pop() + '</b>';

  if (mime.startsWith('image/')) {
    video.style.display  = 'none';
    imgEl.style.display  = 'block';
    imgEl.src            = '/stream/' + path;
    ctrlBar.classList.remove('show');
  } else {
    video.style.display  = 'block';
    imgEl.style.display  = 'none';
    ctrlBar.classList.add('show');
    loadVideo('/stream/' + path, mime, transcode);
  }

  wrap.classList.add('active');
  wrap.scrollIntoView({ behavior: 'smooth' });
}

// ── Load video source ───────────────────────────────────────────────────────
function loadVideo(url, mime, transcode) {
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
  video.paused ? video.play() : video.pause();
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
}

function toggleMute() {
  video.muted = !video.muted;
  document.getElementById('vol-slider').value = video.muted ? 0 : video.volume;
  updateMuteIcon();
}

function updateMuteIcon() {
  document.getElementById('icon-vol').style.display  = video.muted ? 'none' : '';
  document.getElementById('icon-mute').style.display = video.muted ? '' : 'none';
}

function toggleFullscreen() {
  const wrap = document.getElementById('player-wrap');
  if (!document.fullscreenElement) {
    wrap.requestFullscreen().catch(() => video.requestFullscreen());
  } else {
    document.exitFullscreen();
  }
}

// Transcode seek — restarts FFmpeg from chosen timestamp
function seekTranscode() {
  if (!currentPath || !isTranscoded) return;
  const t = document.getElementById('tc-slider').value;
  loadVideo('/stream/' + currentPath + '?t=' + t, 'video/mp4', true);
}

// ── Close ───────────────────────────────────────────────────────────────────
function closePlayer() {
  video.pause();
  video.src = '';
  document.getElementById('img-viewer').src = '';
  document.getElementById('player-wrap').classList.remove('active');
  document.getElementById('ctrl-bar').classList.remove('show');
  document.getElementById('tc-row').classList.remove('show');
  document.getElementById('prog-fill').style.width = '0%';
  document.getElementById('prog-buf').style.width  = '0%';
  document.getElementById('time-cur').textContent  = '0:00';
  document.getElementById('time-dur').textContent  = '--:--';
  currentPath = null; isTranscoded = false;
}

// ── Progress bar update (runs every animationFrame) ─────────────────────────
video.addEventListener('timeupdate', () => {
  if (!video.duration) return;
  const pct = (video.currentTime / video.duration) * 100;
  document.getElementById('prog-fill').style.width = pct + '%';
  document.getElementById('time-cur').textContent  = formatTime(video.currentTime);
});

video.addEventListener('durationchange', () => {
  document.getElementById('time-dur').textContent = formatTime(video.duration);
});

// Buffered indicator
video.addEventListener('progress', () => {
  if (video.buffered.length && video.duration) {
    const end = video.buffered.end(video.buffered.length - 1);
    document.getElementById('prog-buf').style.width = (end / video.duration * 100) + '%';
  }
});

// Play/pause icon sync
video.addEventListener('play',  () => {
  document.getElementById('icon-play').style.display  = 'none';
  document.getElementById('icon-pause').style.display = '';
});
video.addEventListener('pause', () => {
  document.getElementById('icon-play').style.display  = '';
  document.getElementById('icon-pause').style.display = 'none';
});

// Click video to play/pause
video.addEventListener('click', togglePlay);

// ── Click on progress bar to seek ───────────────────────────────────────────
document.getElementById('prog-track').addEventListener('click', function(e) {
  if (!video.duration || isTranscoded) return;
  const rect = this.getBoundingClientRect();
  const pct  = (e.clientX - rect.left) / rect.width;
  video.currentTime = pct * video.duration;
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
