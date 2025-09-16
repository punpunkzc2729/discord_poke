"""FastAPI application exposing REST and websocket interfaces."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from .playback import playback_manager
from .schemas import PlaybackState, SearchResponse, Track
from .youtube import search_youtube

load_dotenv()

if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube Remote Player", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Serve the main control interface."""

    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    """Simple health endpoint used for readiness checks."""

    return {"status": "ok"}


@app.get("/api/search", response_model=SearchResponse)
async def api_search(
    q: str = Query(..., min_length=2, description="Search term"),
    limit: int = Query(8, ge=1, le=25, description="Number of results to return"),
) -> SearchResponse:
    """Search YouTube for tracks matching ``q``."""

    results = await search_youtube(q, limit)
    return SearchResponse(items=results)


@app.get("/api/state", response_model=PlaybackState)
async def api_state() -> PlaybackState:
    """Return the current playback state."""

    return await playback_manager.get_state()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    device_id = websocket.query_params.get("device_id")
    if not device_id:
        await websocket.close(code=4400)
        return

    await playback_manager.connect(device_id, websocket)
    try:
        while True:
            message = await websocket.receive_json()
            await _handle_ws_message(device_id, message)
    except WebSocketDisconnect:
        logger.info("Websocket disconnect for device %s", device_id)
        await playback_manager.disconnect(device_id)
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("Unexpected websocket error for %s", device_id)
        await playback_manager.disconnect(device_id)


async def _handle_ws_message(device_id: str, message: dict[str, Any]) -> None:
    """Handle a message received over the websocket connection."""

    msg_type = message.get("type")
    if not msg_type:
        logger.warning("Received websocket payload without type: %s", message)
        return

    if msg_type == "register":
        name = message.get("name") or "Unnamed device"
        is_player = bool(message.get("is_player", False))
        await playback_manager.register_device(device_id, name, is_player=is_player)
        return

    if msg_type == "update_device":
        await playback_manager.update_device(
            device_id,
            name=message.get("name"),
            is_player=message.get("is_player"),
        )
        return

    if msg_type == "set_active_device":
        target_id = message.get("device_id")
        if not target_id:
            return
        await playback_manager.set_active_device(target_id)
        return

    if msg_type in {"enqueue", "play_now"}:
        track_data = message.get("track")
        if not isinstance(track_data, dict):
            logger.warning("Invalid track payload from %s: %s", device_id, message)
            return
        track = Track.model_validate(track_data)
        if msg_type == "enqueue":
            await playback_manager.enqueue(track)
        else:
            await playback_manager.play_now(track)
        return

    if msg_type == "pause":
        await playback_manager.pause()
        return

    if msg_type == "resume":
        await playback_manager.resume()
        return

    if msg_type == "stop":
        await playback_manager.stop()
        return

    if msg_type == "next":
        await playback_manager.next_track()
        return

    if msg_type == "progress":
        position = message.get("position")
        if isinstance(position, (int, float)):
            await playback_manager.update_position(position=float(position))
        return

    if msg_type == "track_ended":
        state = await playback_manager.get_state()
        if state.current_device == device_id:
            await playback_manager.next_track()
        return

    logger.warning("Unhandled websocket message from %s: %s", device_id, message)
