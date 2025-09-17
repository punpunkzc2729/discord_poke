"""Centralised playback state management."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from fastapi import WebSocket

from .schemas import Device, PlaybackState, Track

logger = logging.getLogger(__name__)


class PlaybackManager:
    """Keeps shared playback state in sync across websocket clients."""

    def __init__(self) -> None:
        self._state = PlaybackState()
        self._devices: Dict[str, Device] = {}
        self._connections: Dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, device_id: str, websocket: WebSocket) -> None:
        """Register a websocket connection for ``device_id``."""

        await websocket.accept()
        async with self._lock:
            self._connections[device_id] = websocket
        logger.info("Device %s connected", device_id)
        await self.send_state(device_id)

    async def disconnect(self, device_id: str) -> None:
        """Remove a websocket connection and device state."""

        async with self._lock:
            self._connections.pop(device_id, None)
            removed = self._devices.pop(device_id, None)
            if removed and self._state.current_device == device_id:
                self._state.current_device = self._select_next_active_device(excluding=device_id)
            self._refresh_device_flags()
        logger.info("Device %s disconnected", device_id)
        await self.broadcast_state()

    async def register_device(self, device_id: str, name: str, *, is_player: bool) -> None:
        """Update metadata for a device once it has identified itself."""

        async with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                device = Device(device_id=device_id, name=name or "Unnamed device")
            else:
                if name:
                    device.name = name
            device.is_player = bool(is_player)
            self._devices[device_id] = device

            if self._state.current_device is None and device.is_player:
                self._state.current_device = device_id

            self._refresh_device_flags()
        await self.broadcast_state()

    async def update_device(self, device_id: str, *, name: Optional[str] = None, is_player: Optional[bool] = None) -> None:
        async with self._lock:
            device = self._devices.get(device_id)
            if not device:
                logger.warning("Attempted to update unknown device %s", device_id)
                return
            if name is not None:
                device.name = name
            if is_player is not None:
                device.is_player = bool(is_player)
                if not device.is_player and self._state.current_device == device_id:
                    self._state.current_device = self._select_next_active_device(excluding=device_id)
                elif device.is_player and self._state.current_device is None:
                    self._state.current_device = device_id
            self._refresh_device_flags()
        await self.broadcast_state()

    async def set_active_device(self, device_id: str) -> None:
        async with self._lock:
            device = self._devices.get(device_id)
            if not device or not device.is_player:
                logger.warning("Cannot activate device %s; not registered or not a player", device_id)
                return
            self._state.current_device = device_id
            self._refresh_device_flags()
        logger.info("Active device set to %s", device_id)
        await self.broadcast_state()

    async def enqueue(self, track: Track) -> None:
        async with self._lock:
            if self._state.current_track is None:
                self._state.current_track = track
                self._state.is_playing = True
            else:
                self._state.queue.append(track)
        logger.info("Track queued: %s", track.title)
        await self.broadcast_state()

    async def play_now(self, track: Track) -> None:
        async with self._lock:
            self._state.current_track = track
            self._state.is_playing = True
        logger.info("Playing track now: %s", track.title)
        await self.broadcast_state()

    async def pause(self) -> None:
        async with self._lock:
            if self._state.current_track is None:
                return
            self._state.is_playing = False
        await self.broadcast_state()

    async def resume(self) -> None:
        async with self._lock:
            if self._state.current_track is None:
                return
            self._state.is_playing = True
        await self.broadcast_state()

    async def stop(self) -> None:
        async with self._lock:
            self._state.current_track = None
            self._state.is_playing = False
            self._state.position_seconds = None
            self._state.queue.clear()
        logger.info("Playback stopped and queue cleared")
        await self.broadcast_state()

    async def next_track(self) -> None:
        async with self._lock:
            if self._state.queue:
                self._state.current_track = self._state.queue.pop(0)
                self._state.is_playing = True
                self._state.position_seconds = 0
            else:
                self._state.current_track = None
                self._state.is_playing = False
                self._state.position_seconds = None
        logger.info("Skipping to next track")
        await self.broadcast_state()

    async def update_position(self, *, position: Optional[float]) -> None:
        async with self._lock:
            self._state.position_seconds = position
        await self.broadcast_state()

    async def get_state(self) -> PlaybackState:
        async with self._lock:
            return self._state.model_copy(deep=True)

    async def broadcast_state(self) -> None:
        payload = (await self.get_state()).model_dump()
        await self._send_to_all({"type": "state", "payload": payload})

    async def send_state(self, device_id: str) -> None:
        payload = (await self.get_state()).model_dump()
        websocket = self._connections.get(device_id)
        if websocket:
            try:
                await websocket.send_json({"type": "state", "payload": payload})
            except Exception:  # pragma: no cover - defensive cleanup
                logger.exception("Failed to send state to %s", device_id)
                await self.disconnect(device_id)

    async def _send_to_all(self, message: dict) -> None:
        for device_id, websocket in list(self._connections.items()):
            try:
                await websocket.send_json(message)
            except Exception:  # pragma: no cover - defensive cleanup
                logger.warning("Websocket for %s dropped during broadcast", device_id)
                await self.disconnect(device_id)

    def _refresh_device_flags(self) -> None:
        current = self._state.current_device
        devices = []
        for device in self._devices.values():
            device.is_active = device.device_id == current
            devices.append(device)
        self._state.devices = devices

    def _select_next_active_device(self, *, excluding: Optional[str] = None) -> Optional[str]:
        for device in self._devices.values():
            if device.device_id == excluding:
                continue
            if device.is_player:
                return device.device_id
        return None


playback_manager = PlaybackManager()
