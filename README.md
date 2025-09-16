# YouTube Connect Player

A modern FastAPI application that turns YouTube into a shared, multi-device listening experience. Search for music, manage a shared queue, and control playback remotely—similar to Spotify Connect—directly from a responsive web interface.

## Features

- **YouTube search** powered by [`yt_dlp`](https://github.com/yt-dlp/yt-dlp); no official API keys required.
- **Shared playback state** maintained on the server and synchronised to all clients via WebSockets.
- **Device management** that lets you pick which browser/device acts as the active player while others remain as remote controllers.
- **Real-time controls** for play, pause, skip, stop, and queue management from any connected client.
- **Responsive web UI** featuring an embedded YouTube player for the active device and live updates for every participant.

## Getting started

### Prerequisites

- Python 3.11 or newer
- `pip`

### Installation

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
pip install -r requirements.txt
```

### Running locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open [http://localhost:8000](http://localhost:8000) and connect from multiple devices or browser tabs. Mark one device as a playback device to start playing audio; others can control the queue and playback in real time.

## Project structure

```
app/
├── __init__.py
├── main.py          # FastAPI application & websocket handlers
├── playback.py      # Shared playback state manager
├── schemas.py       # Pydantic models for API and websocket payloads
└── youtube.py       # YouTube search helper
static/
├── css/styles.css   # UI styling
└── js/app.js        # Front-end logic and websocket client
templates/
└── index.html       # Main interface served by FastAPI
```

## Deployment

The repository includes a `Dockerfile`, `Procfile`, and Railway/Nixpacks configuration already configured to run the FastAPI app using Uvicorn. Build and run the container locally with:

```bash
docker build -t yt-connect-player .
docker run -p 8000:8000 yt-connect-player
```

## Notes

- The application uses the YouTube IFrame Player API in the browser; therefore the active playback device must keep the page open to stream audio.
- `yt_dlp` performs scraping for search results. Respect YouTube’s terms of service when deploying publicly.
- Environment variables can be defined in an optional `.env` file if you need to customise behaviour; `python-dotenv` is pre-installed.
