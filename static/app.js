let currentPath = null;
let isTranscoded = false;
const video = document.getElementById("main-video");
const playerWrap = document.getElementById("player-wrap");
const playerStage = document.getElementById("player-stage");
const imgViewer = document.getElementById("img-viewer");
const progressTrack = document.getElementById("prog-track");
const progressFill = document.getElementById("prog-fill");
const progressBuf = document.getElementById("prog-buf");
const progressThumb = document.getElementById("prog-thumb");
const playerStatus = document.getElementById("player-status");
const nowPlaying = document.getElementById("now-playing");
const playerEyebrow = document.getElementById("player-eyebrow");
let hideChromeTimer = null;
let isScrubbing = false;
let wasPlayingBeforeScrub = false;
const VIEW_MODE_KEY = "streambox:view-mode";
const PAGE_MODE = window.STREAMBOX_BOOTSTRAP?.pageMode;
const DETAIL_MEDIA = window.STREAMBOX_BOOTSTRAP?.media;

if (PAGE_MODE === "browse") {
  applySavedViewMode();
}

async function openPlayer(path, mime, transcode, shouldScroll = true) {
  currentPath = path;
  isTranscoded = transcode;

  const ctrlBar = document.getElementById("ctrl-bar");
  const filename = path.split("/").pop();

  nowPlaying.textContent = filename;
  playerEyebrow.textContent = mime.startsWith("image/") ? "Preview" : (transcode ? "Streaming with Transcode" : "Now Playing");
  playerStatus.textContent = transcode ? "Transcoding" : "Ready";
  playerWrap.classList.remove("chrome-hidden");
  clearHideChromeTimer();

  if (mime.startsWith("image/")) {
    video.pause();
    video.src = "";
    video.style.display = "none";
    imgViewer.style.display = "block";
    imgViewer.src = "/stream/" + path;
    document.getElementById("player-center").style.display = "none";
    ctrlBar.style.display = "none";
  } else {
    video.style.display = "block";
    imgViewer.style.display = "none";
    document.getElementById("player-center").style.display = "";
    ctrlBar.style.display = "";
    loadVideo("/stream/" + path, mime, transcode);
    armChromeAutoHide();
  }

  playerWrap.classList.add("active");
  if (shouldScroll) {
    playerWrap.scrollIntoView({ behavior: "smooth" });
  }
}

function loadVideo(url, mime, transcode) {
  playerStatus.textContent = transcode ? "Transcoding" : "Loading";
  progressFill.style.width = "0%";
  progressBuf.style.width = "0%";
  progressThumb.style.left = "0%";
  document.getElementById("time-cur").textContent = "0:00";
  video.src = url;
  video.load();
  video.play().catch(() => {});

  const tcRow = document.getElementById("tc-row");
  if (transcode) {
    tcRow.classList.add("show");
    fetch("/info/" + currentPath).then((r) => r.json()).then((data) => {
      if (data.duration) {
        const sl = document.getElementById("tc-slider");
        sl.max = Math.floor(data.duration);
        document.getElementById("tc-dur").textContent = formatTime(data.duration);
      }
    });
  } else {
    tcRow.classList.remove("show");
  }
}

function togglePlay() {
  if (!currentPath) return;
  video.paused ? video.play() : video.pause();
  armChromeAutoHide();
}

function skip(secs) {
  if (!isTranscoded || Math.abs(secs) <= 30) {
    video.currentTime = Math.max(0, video.currentTime + secs);
  }
}

function setVolume(v) {
  video.volume = v;
  video.muted = v == 0;
  updateMuteIcon();
  armChromeAutoHide();
}

function toggleMute() {
  video.muted = !video.muted;
  document.getElementById("vol-slider").value = video.muted ? 0 : video.volume;
  updateMuteIcon();
  armChromeAutoHide();
}

function updateMuteIcon() {
  const showMute = video.muted || video.volume === 0;
  const showLow = !showMute && video.volume < 0.5;
  document.getElementById("icon-vol-high").style.display = (!showMute && !showLow) ? "" : "none";
  document.getElementById("icon-vol-low").style.display = showLow ? "" : "none";
  document.getElementById("icon-mute").style.display = showMute ? "" : "none";
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    playerWrap.requestFullscreen().catch(() => video.requestFullscreen());
  } else {
    document.exitFullscreen();
  }
  armChromeAutoHide();
}

function seekTranscode() {
  if (!currentPath || !isTranscoded) return;
  const t = document.getElementById("tc-slider").value;
  loadVideo("/stream/" + currentPath + "?t=" + t, "video/mp4", true);
  playerStatus.textContent = "Jumped";
}

function closePlayer() {
  if (document.fullscreenElement === playerWrap) {
    document.exitFullscreen().catch(() => {});
  }
  video.pause();
  video.src = "";
  imgViewer.src = "";
  imgViewer.style.display = "none";
  playerWrap.classList.remove("active", "chrome-hidden", "player-fullscreen");
  document.getElementById("ctrl-bar").style.display = "";
  document.getElementById("tc-row").classList.remove("show");
  progressFill.style.width = "0%";
  progressBuf.style.width = "0%";
  progressThumb.style.left = "0%";
  document.getElementById("time-cur").textContent = "0:00";
  document.getElementById("time-dur").textContent = "--:--";
  playerStatus.textContent = "Ready";
  clearHideChromeTimer();
  currentPath = null;
  isTranscoded = false;
}

video.addEventListener("timeupdate", () => {
  if (!video.duration) return;
  const pct = (video.currentTime / video.duration) * 100;
  progressFill.style.width = pct + "%";
  progressThumb.style.left = pct + "%";
  document.getElementById("time-cur").textContent = formatTime(video.currentTime);
});

video.addEventListener("durationchange", () => {
  document.getElementById("time-dur").textContent = formatTime(video.duration);
});

video.addEventListener("progress", () => {
  if (video.buffered.length && video.duration) {
    const end = video.buffered.end(video.buffered.length - 1);
    progressBuf.style.width = (end / video.duration * 100) + "%";
  }
});

video.addEventListener("play", () => {
  document.getElementById("icon-play").style.display = "none";
  document.getElementById("icon-pause").style.display = "";
  playerStatus.textContent = isTranscoded ? "Streaming" : "Playing";
  armChromeAutoHide();
});

video.addEventListener("pause", () => {
  document.getElementById("icon-play").style.display = "";
  document.getElementById("icon-pause").style.display = "none";
  playerStatus.textContent = "Paused";
  showChrome();
});

video.addEventListener("waiting", () => {
  playerStatus.textContent = isTranscoded ? "Buffering transcode" : "Buffering";
});

video.addEventListener("ended", () => {
  playerStatus.textContent = "Finished";
  showChrome();
});

video.addEventListener("loadedmetadata", () => {
  document.getElementById("time-dur").textContent = formatTime(video.duration);
});

video.addEventListener("click", togglePlay);

function updateProgressFromClientX(clientX) {
  if (!video.duration || isTranscoded) return;
  const rect = progressTrack.getBoundingClientRect();
  const pct = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
  progressFill.style.width = (pct * 100) + "%";
  progressThumb.style.left = (pct * 100) + "%";
  document.getElementById("time-cur").textContent = formatTime(video.duration * pct);
  video.currentTime = video.duration * pct;
}

progressTrack.addEventListener("pointerdown", (event) => {
  if (!video.duration || isTranscoded) return;
  isScrubbing = true;
  wasPlayingBeforeScrub = !video.paused;
  progressTrack.classList.add("dragging");
  showChrome();
  updateProgressFromClientX(event.clientX);
});

window.addEventListener("pointermove", (event) => {
  if (!isScrubbing) return;
  updateProgressFromClientX(event.clientX);
});

window.addEventListener("pointerup", () => {
  if (!isScrubbing) return;
  isScrubbing = false;
  progressTrack.classList.remove("dragging");
  if (wasPlayingBeforeScrub) {
    video.play().catch(() => {});
  }
  armChromeAutoHide();
});

function clearHideChromeTimer() {
  if (hideChromeTimer) {
    window.clearTimeout(hideChromeTimer);
    hideChromeTimer = null;
  }
}

function showChrome() {
  if (!playerWrap.classList.contains("active")) return;
  playerWrap.classList.remove("chrome-hidden");
  clearHideChromeTimer();
}

function armChromeAutoHide() {
  if (!playerWrap.classList.contains("active") || video.paused || isScrubbing || imgViewer.style.display === "block") {
    return;
  }
  showChrome();
  hideChromeTimer = window.setTimeout(() => {
    if (!video.paused && !isScrubbing) {
      playerWrap.classList.add("chrome-hidden");
    }
  }, 2200);
}

["mousemove", "pointermove", "touchstart"].forEach((eventName) => {
  playerStage.addEventListener(eventName, armChromeAutoHide, { passive: true });
});

document.addEventListener("fullscreenchange", () => {
  playerWrap.classList.toggle("player-fullscreen", document.fullscreenElement === playerWrap);
});

document.addEventListener("keydown", (event) => {
  if (!playerWrap.classList.contains("active") || imgViewer.style.display === "block") return;
  if (event.key === " " || event.key === "Spacebar") {
    event.preventDefault();
    togglePlay();
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    skip(-10);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    skip(10);
  } else if (event.key === "f" || event.key === "F" || event.key === "Enter") {
    event.preventDefault();
    toggleFullscreen();
  } else if (event.key === "m" || event.key === "M") {
    event.preventDefault();
    toggleMute();
  } else if (event.key === "Escape") {
    if (PAGE_MODE === "browse") {
      closePlayer();
    }
  }
});

function formatTime(secs) {
  secs = Math.floor(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h > 0
    ? h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0")
    : m + ":" + String(s).padStart(2, "0");
}

function confirmDelete(name) {
  return window.confirm('Delete "' + name + '"? This cannot be undone.');
}

async function handleDeleteSubmit(event) {
  event.preventDefault();
  event.stopPropagation();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const path = form.dataset.entryPath;
  const name = form.dataset.entryName || path;

  if (!confirmDelete(name)) {
    const menu = form.closest(".menu-popover");
    if (menu) {
      menu.open = false;
    }
    return;
  }

  if (button) {
    button.disabled = true;
  }

  try {
    const response = await fetch(form.action, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
    });

    if (!response.ok) {
      throw new Error("Delete failed");
    }

    document.querySelectorAll('[data-entry-path="' + CSS.escape(path) + '"]').forEach((node) => {
      if (node.matches("tr, article.card")) {
        node.remove();
      }
    });

    const hasEntries = document.querySelector("[data-entry-path]");
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

document.querySelectorAll(".delete-form").forEach((form) => {
  form.addEventListener("submit", handleDeleteSubmit);
});

document.querySelectorAll(".menu-popover").forEach((menu) => {
  menu.addEventListener("toggle", () => {
    if (!menu.open) {
      return;
    }
    document.querySelectorAll(".menu-popover").forEach((other) => {
      if (other !== menu) {
        other.open = false;
      }
    });
  });
});

document.addEventListener("click", (event) => {
  if (event.target.closest(".menu-popover")) {
    return;
  }
  document.querySelectorAll(".menu-popover").forEach((menu) => {
    menu.open = false;
  });
});

document.querySelectorAll(".menu-list a, .menu-list button").forEach((item) => {
  item.addEventListener("click", () => {
    const menu = item.closest(".menu-popover");
    if (menu) {
      menu.open = false;
    }
  });
});

function setViewMode(mode) {
  if (PAGE_MODE !== "browse") {
    return;
  }
  const normalized = mode === "grid" ? "grid" : "list";
  localStorage.setItem(VIEW_MODE_KEY, normalized);
  applyViewMode(normalized);
}

function applySavedViewMode() {
  if (PAGE_MODE !== "browse") {
    return;
  }
  applyViewMode(localStorage.getItem(VIEW_MODE_KEY) || "grid");
}

function applyViewMode(mode) {
  const viewList = document.getElementById("view-list");
  const viewGrid = document.getElementById("view-grid");
  if (!viewList || !viewGrid) {
    return;
  }
  const showGrid = mode === "grid";
  viewList.classList.toggle("active", !showGrid);
  viewGrid.classList.toggle("active", showGrid);
  document.querySelectorAll(".view-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === mode);
  });
}

async function loadDetailDuration(path) {
  const badge = document.getElementById("media-duration");
  if (!badge) {
    return;
  }
  try {
    const response = await fetch("/info/" + path);
    const data = await response.json();
    if (data.duration) {
      badge.textContent = "duration " + formatTime(data.duration);
      badge.classList.add("show");
    }
  } catch (error) {
    // Ignore metadata errors and leave the badge hidden.
  }
}

if (PAGE_MODE === "detail" && DETAIL_MEDIA) {
  openPlayer(DETAIL_MEDIA.path, DETAIL_MEDIA.mime, DETAIL_MEDIA.transcode, false);
  if (DETAIL_MEDIA.is_video) {
    loadDetailDuration(DETAIL_MEDIA.path);
  }
}
