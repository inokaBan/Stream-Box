# StreamBox

A personal media streaming server built with Python and Flask. Stream videos, music, and images directly in your browser from any device on your network — no downloads required.

Built to run on anything, including Android via Termux.

---

## Features

- **HTTP Range Request streaming** — native formats play instantly with full seeking support
- **On-the-fly transcoding** — `.mkv`, `.avi`, `.mov` and other non-browser formats are converted in real time via FFmpeg
- **Custom video player** — play/pause, seek bar, skip buttons, volume control, fullscreen, and a buffering indicator. No browser default controls
- **Image preview** — images open inline without downloading
- **Smart routing** — the server automatically detects whether a file needs transcoding or can stream natively
- **Directory browsing** — navigate folders, see file sizes, download files, and delete files
- **Termux-friendly** — runs on Android, your phone becomes the server

---

## Quick Start

### On Termux (Android)

```bash
pkg install python ffmpeg
pip install flask
mkdir ~/streambox && cd ~/streambox
mkdir media
# paste server.py here
python server.py
```

### On Linux / Mac / Windows

```bash
pip install flask
# ffmpeg must be installed and available in PATH
mkdir streambox && cd streambox
mkdir media
# paste server.py here
python server.py
```

Then open `http://localhost:5000` in your browser.

---

## Accessing Remotely

Your phone or machine acts as the server. Any device on the same Wi-Fi network can connect.

**Find your local IP:**
```bash
# Termux / Linux
ip addr show wlan0 | grep 'inet '

# macOS
ipconfig getifaddr en0

# Windows
ipconfig
```

Then visit `http://<your-ip>:5000` from any browser on the same network.

> For access over the internet (outside your home network), you would need to set up port forwarding on your router. Be aware this exposes the server publicly — authentication is not yet implemented.

---

## Adding Files

Drop files into the `media/` folder. Subfolders are supported and browsable.

```
streambox/
  server.py
  media/
    movies/
      film.mp4
      old-film.mkv
    music/
      song.mp3
    photos/
      photo.jpg
```

---

## Supported Formats

### Native (no transcoding, instant playback)

| Type  | Formats                              |
|-------|--------------------------------------|
| Video | `.mp4`, `.webm`, `.ogv`              |
| Audio | `.mp3`, `.ogg`, `.wav`, `.flac`, `.m4a`, `.aac`, `.opus` |
| Image | `.jpg`, `.png`, `.gif`, `.webp`, `.svg` |

### Transcoded (FFmpeg required)

| Formats |
|---------|
| `.mkv`, `.avi`, `.mov`, `.wmv`, `.flv`, `.m4v`, `.3gp`, `.ts`, `.vob`, `.divx`, `.rm`, `.rmvb`, `.asf`, `.f4v` |

These are converted to H.264/MP4 on the fly when you open them. The original file is never modified.

---

## How It Works

### Native streaming — HTTP Range Requests

Browsers need to request specific byte ranges of a file to seek through video (`Range: bytes=1048576-2097151`). Python's built-in `http.server` ignores these requests, so files have to be fully downloaded before playing.

StreamBox reads the `Range` header, seeks directly to that byte position in the file, and returns only the requested chunk with a `206 Partial Content` response. This is what makes seeking instant.

### Transcoded streaming — FFmpeg pipe

For formats the browser can't play, StreamBox spawns FFmpeg as a subprocess and pipes its stdout directly into the HTTP response. FFmpeg re-encodes the video to H.264/AAC inside a fragmented MP4 container (`frag_keyframe+empty_moov`), which doesn't require knowing the file size upfront, making it streamable chunk by chunk.

Seeking in transcoded streams works by restarting FFmpeg with a `-ss <timestamp>` argument, which tells it to start encoding from that point in the file.

---

## Configuration

Edit the top of `server.py`:

```python
MEDIA_ROOT        = Path("./media")   # Folder to serve
CHUNK_SIZE        = 1024 * 1024       # Read chunk size in bytes (1MB default)
TRANSCODE_BITRATE = "1500k"           # Video bitrate for transcoded output
AUDIO_BITRATE     = "128k"            # Audio bitrate for transcoded output
```

**On low-powered devices (phones), lower the bitrate if transcoded video stutters:**
```python
TRANSCODE_BITRATE = "800k"
```

---

## Project Structure

```
streambox/
  server.py     # Everything — Flask server, streaming logic, HTML/CSS/JS UI
  media/        # Your files go here
```

The entire project is a single Python file by design, making it easy to copy, edit, and run anywhere.

---

## Dependencies

| Dependency | Purpose | Install |
|---|---|---|
| Python 3.10+ | Runtime | `pkg install python` |
| Flask | Web server | `pip install flask` |
| FFmpeg | Transcoding | `pkg install ffmpeg` |

FFmpeg is optional — native formats (.mp4, .mp3, images) work without it. The header will show `✗ ffmpeg missing` if it's not installed, and transcoded formats will return a 503 error instead of playing.

---

## License

Do whatever you want with it. This is a personal learning project.
