const connectionStatusEl = document.getElementById("connection-status");
const nowPlayingTitleEl = document.getElementById("now-playing-title");
const nowPlayingChannelEl = document.getElementById("now-playing-channel");
const nowPlayingArtEl = document.getElementById("now-playing-art");
const progressBarEl = document.getElementById("progress-bar");
const playPauseBtn = document.getElementById("play-pause-btn");
const nextBtn = document.getElementById("next-btn");
const stopBtn = document.getElementById("stop-btn");
const queueListEl = document.getElementById("queue-list");
const queueEmptyEl = document.getElementById("queue-empty");
const searchForm = document.getElementById("search-form");
const searchInput = document.getElementById("search-input");
const searchResultsEl = document.getElementById("search-results");
const deviceNameInput = document.getElementById("device-name");
const devicePlayerToggle = document.getElementById("device-player");
const devicesListEl = document.getElementById("devices-list");

const STORAGE_KEYS = {
  id: "yt-connect-device-id",
  name: "yt-connect-device-name",
  player: "yt-connect-is-player",
};

const defaultTitle = document.title;
const deviceId = getOrCreateDeviceId();
let deviceName = getStoredOrDefault(STORAGE_KEYS.name, () => `Device ${Math.floor(Math.random() * 999) + 1}`);
let isPlayer = getStoredOrDefault(STORAGE_KEYS.player, () => "true") === "true";

let ws;
let reconnectHandle;
let currentState = null;
let player;
let playerReady = false;
let ignoreNextPlayerEvent = false;
let currentVideoId = null;
let progressTimer;
let lastProgressSent = 0;

function getOrCreateDeviceId() {
  const saved = localStorage.getItem(STORAGE_KEYS.id);
  if (saved) {
    return saved;
  }
  const id = crypto.randomUUID();
  localStorage.setItem(STORAGE_KEYS.id, id);
  return id;
}

function getStoredOrDefault(key, factory) {
  const saved = localStorage.getItem(key);
  if (saved !== null) {
    return saved;
  }
  const value = factory();
  localStorage.setItem(key, value);
  return value;
}

function setConnectionStatus(text, kind = "info") {
  connectionStatusEl.textContent = text;
  connectionStatusEl.classList.toggle("muted", kind !== "ok");
}

function connectWebSocket() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    return;
  }

  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${window.location.host}/ws?device_id=${encodeURIComponent(deviceId)}`;
  ws = new WebSocket(url);

  ws.addEventListener("open", () => {
    setConnectionStatus("Connected", "ok");
    sendMessage("register", { name: deviceName, is_player: isPlayer });
  });

  ws.addEventListener("message", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.type === "state") {
        handleStateUpdate(payload.payload);
      }
    } catch (error) {
      console.error("Failed to parse websocket message", error);
    }
  });

  ws.addEventListener("close", () => {
    setConnectionStatus("Disconnected – reconnecting…");
    scheduleReconnect();
  });

  ws.addEventListener("error", () => {
    ws.close();
  });
}

function scheduleReconnect() {
  if (reconnectHandle) {
    return;
  }
  reconnectHandle = window.setTimeout(() => {
    reconnectHandle = null;
    connectWebSocket();
  }, 2000);
}

function sendMessage(type, payload = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    return;
  }
  ws.send(JSON.stringify({ type, ...payload }));
}

function handleStateUpdate(state) {
  currentState = state;
  updateNowPlaying(state);
  updateQueue(state.queue || []);
  updateDevices(state.devices || []);
  updateControls(state);
  syncPlayer(state);
}

function updateNowPlaying(state) {
  const track = state.current_track;

  if (!track) {
    document.title = defaultTitle;
    nowPlayingTitleEl.textContent = "No track playing";
    nowPlayingChannelEl.textContent = "Choose a track to get started.";
    nowPlayingArtEl.classList.add("hidden");
    nowPlayingArtEl.src = "";
    updateProgressBar(0);
    return;
  }

  document.title = `${track.title} · YouTube Connect Player`;
  nowPlayingTitleEl.textContent = track.title;
  nowPlayingChannelEl.textContent = track.channel || "Unknown artist";

  if (track.thumbnail) {
    nowPlayingArtEl.src = track.thumbnail;
    nowPlayingArtEl.classList.remove("hidden");
  } else {
    nowPlayingArtEl.classList.add("hidden");
  }

  if (typeof state.position_seconds === "number" && track.duration) {
    const progress = Math.min(100, (state.position_seconds / track.duration) * 100);
    updateProgressBar(progress);
  } else {
    updateProgressBar(0);
  }
}

function updateProgressBar(percent) {
  progressBarEl.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

function updateQueue(queue) {
  queueListEl.innerHTML = "";
  if (!queue.length) {
    queueEmptyEl.classList.remove("hidden");
    return;
  }
  queueEmptyEl.classList.add("hidden");

  queue.forEach((track) => {
    const item = document.createElement("li");
    item.className = "queue-item";
    const meta = document.createElement("div");
    meta.className = "meta";
    const title = document.createElement("span");
    title.textContent = track.title;
    meta.appendChild(title);
    if (track.channel || track.duration) {
      const subtitle = document.createElement("small");
      subtitle.textContent = [track.channel, formatDuration(track.duration)].filter(Boolean).join(" • ");
      meta.appendChild(subtitle);
    }
    item.appendChild(meta);
    queueListEl.appendChild(item);
  });
}

function updateDevices(devices) {
  devicesListEl.innerHTML = "";

  if (!Array.isArray(devices) || !devices.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "No other devices are connected yet.";
    devicesListEl.appendChild(empty);
    return;
  }

  devices.forEach((device) => {
    const item = document.createElement("li");
    item.className = "device-item";

    const details = document.createElement("div");
    details.className = "details";
    const name = document.createElement("span");
    name.textContent = device.device_id === deviceId ? `${device.name} (You)` : device.name;
    details.appendChild(name);
    const descriptor = document.createElement("small");
    const parts = [];
    parts.push(device.is_player ? "Playback device" : "Controller only");
    if (device.is_active) {
      parts.push("Active");
    }
    descriptor.textContent = parts.join(" • ");
    details.appendChild(descriptor);

    const actions = document.createElement("div");
    actions.className = "actions";
    const button = document.createElement("button");
    if (device.is_active) {
      button.textContent = "Active";
      button.disabled = true;
    } else if (!device.is_player) {
      button.textContent = "Controller";
      button.disabled = true;
    } else {
      button.textContent = "Set active";
      button.addEventListener("click", () => {
        sendMessage("set_active_device", { device_id: device.device_id });
      });
    }

    actions.appendChild(button);
    item.appendChild(details);
    item.appendChild(actions);
    devicesListEl.appendChild(item);

    if (device.device_id === deviceId) {
      if (!deviceNameInput.matches(":focus")) {
        deviceNameInput.value = device.name;
      }
      if (!devicePlayerToggle.matches(":focus")) {
        devicePlayerToggle.checked = Boolean(device.is_player);
        isPlayer = Boolean(device.is_player);
        localStorage.setItem(STORAGE_KEYS.player, String(isPlayer));
      }
    }
  });
}

function updateControls(state) {
  const hasTrack = Boolean(state.current_track);
  const isPlaying = Boolean(state.is_playing);

  playPauseBtn.textContent = isPlaying ? "Pause" : "Play";
  playPauseBtn.dataset.state = isPlaying ? "pause" : "play";
  playPauseBtn.disabled = !hasTrack;
  nextBtn.disabled = !hasTrack;
  stopBtn.disabled = !hasTrack;
}

function syncPlayer(state) {
  if (!playerReady) {
    return;
  }

  const track = state.current_track;
  const isActive = state.current_device === deviceId && isPlayer;

  if (!track || !isActive) {
    if (player && typeof player.stopVideo === "function") {
      if (player.getPlayerState() !== YT.PlayerState.UNSTARTED) {
        ignoreNextPlayerEvent = true;
        player.stopVideo();
      }
    }
    currentVideoId = null;
    stopProgressLoop();
    return;
  }

  if (currentVideoId !== track.video_id) {
    ignoreNextPlayerEvent = true;
    player.loadVideoById(track.video_id, Math.max(0, state.position_seconds || 0));
    currentVideoId = track.video_id;
  }

  const shouldPlay = Boolean(state.is_playing);
  const playerState = player.getPlayerState();

  if (shouldPlay && playerState !== YT.PlayerState.PLAYING) {
    ignoreNextPlayerEvent = true;
    player.playVideo();
    startProgressLoop();
  } else if (!shouldPlay && playerState === YT.PlayerState.PLAYING) {
    ignoreNextPlayerEvent = true;
    player.pauseVideo();
    stopProgressLoop();
  }

  if (typeof state.position_seconds === "number" && track.duration) {
    const currentTime = player.getCurrentTime();
    if (Math.abs(currentTime - state.position_seconds) > 2) {
      player.seekTo(state.position_seconds, true);
    }
  }
}

function startProgressLoop() {
  if (progressTimer) {
    return;
  }
  progressTimer = window.setInterval(() => {
    if (!playerReady || !currentState) {
      return;
    }
    const isActive = currentState.current_device === deviceId && isPlayer;
    if (!isActive || !currentState.current_track) {
      return;
    }
    const duration = player.getDuration();
    const position = player.getCurrentTime();
    if (Number.isFinite(duration) && duration > 0) {
      const percent = Math.min(100, (position / duration) * 100);
      updateProgressBar(percent);
    }
    const now = Date.now();
    if (now - lastProgressSent > 2000 && Number.isFinite(position)) {
      lastProgressSent = now;
      sendMessage("progress", { position });
    }
  }, 1000);
}

function stopProgressLoop() {
  if (progressTimer) {
    window.clearInterval(progressTimer);
    progressTimer = null;
  }
}

function formatDuration(duration) {
  if (!duration || !Number.isFinite(duration)) {
    return "";
  }
  const totalSeconds = Math.floor(duration);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = `${totalSeconds % 60}`.padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function createTrackPayload(track) {
  return {
    video_id: track.video_id,
    title: track.title,
    url: track.url,
    channel: track.channel,
    duration: track.duration,
    thumbnail: track.thumbnail,
  };
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  if (!query) {
    return;
  }

  searchResultsEl.innerHTML = "";
  const loading = document.createElement("li");
  loading.className = "muted";
  loading.textContent = "Searching…";
  searchResultsEl.appendChild(loading);

  try {
    const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
    if (!response.ok) {
      throw new Error(`Search failed: ${response.status}`);
    }
    const data = await response.json();
    renderSearchResults(Array.isArray(data.items) ? data.items : []);
  } catch (error) {
    console.error(error);
    searchResultsEl.innerHTML = "";
    const errorItem = document.createElement("li");
    errorItem.className = "muted";
    errorItem.textContent = "Unable to fetch search results.";
    searchResultsEl.appendChild(errorItem);
  }
});

function renderSearchResults(items) {
  searchResultsEl.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "No results found.";
    searchResultsEl.appendChild(empty);
    return;
  }

  items.forEach((track) => {
    const listItem = document.createElement("li");
    const meta = document.createElement("div");
    meta.className = "meta";
    const title = document.createElement("span");
    title.textContent = track.title;
    meta.appendChild(title);
    const subtitle = document.createElement("small");
    subtitle.textContent = [track.channel, formatDuration(track.duration)].filter(Boolean).join(" • ");
    meta.appendChild(subtitle);

    const actions = document.createElement("div");
    actions.className = "actions";
    const playButton = document.createElement("button");
    playButton.textContent = "Play now";
    playButton.addEventListener("click", () => {
      sendMessage("play_now", { track: createTrackPayload(track) });
    });
    const queueButton = document.createElement("button");
    queueButton.textContent = "Add to queue";
    queueButton.addEventListener("click", () => {
      sendMessage("enqueue", { track: createTrackPayload(track) });
    });

    actions.appendChild(playButton);
    actions.appendChild(queueButton);
    listItem.appendChild(meta);
    listItem.appendChild(actions);
    searchResultsEl.appendChild(listItem);
  });
}

playPauseBtn.addEventListener("click", () => {
  if (!currentState || !currentState.current_track) {
    return;
  }
  if (playPauseBtn.dataset.state === "pause") {
    sendMessage("pause");
  } else {
    sendMessage("resume");
  }
});

nextBtn.addEventListener("click", () => {
  sendMessage("next");
});

stopBtn.addEventListener("click", () => {
  sendMessage("stop");
});

deviceNameInput.value = deviceName;
deviceNameInput.addEventListener("change", () => {
  deviceName = deviceNameInput.value.trim() || "Unnamed device";
  localStorage.setItem(STORAGE_KEYS.name, deviceName);
  sendMessage("update_device", { name: deviceName });
});

devicePlayerToggle.checked = Boolean(isPlayer);
devicePlayerToggle.addEventListener("change", () => {
  isPlayer = devicePlayerToggle.checked;
  localStorage.setItem(STORAGE_KEYS.player, String(isPlayer));
  sendMessage("update_device", { is_player: isPlayer });
});

window.onYouTubeIframeAPIReady = () => {
  player = new YT.Player("player", {
    height: "360",
    width: "640",
    playerVars: { playsinline: 1 },
    events: {
      onReady: () => {
        playerReady = true;
        if (currentState) {
          syncPlayer(currentState);
        }
      },
      onStateChange: (event) => {
        if (!currentState) {
          return;
        }
        const isActive = currentState.current_device === deviceId && isPlayer;
        if (!isActive) {
          return;
        }
        if (ignoreNextPlayerEvent && (event.data === YT.PlayerState.PLAYING || event.data === YT.PlayerState.PAUSED)) {
          ignoreNextPlayerEvent = false;
          return;
        }
        switch (event.data) {
          case YT.PlayerState.PLAYING:
            startProgressLoop();
            sendMessage("resume");
            break;
          case YT.PlayerState.PAUSED:
            stopProgressLoop();
            sendMessage("pause");
            break;
          case YT.PlayerState.ENDED:
            stopProgressLoop();
            sendMessage("track_ended");
            break;
          default:
            break;
        }
      },
    },
  });
};

window.addEventListener("beforeunload", () => {
  if (ws) {
    ws.close();
  }
});

connectWebSocket();
