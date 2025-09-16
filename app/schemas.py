"""Pydantic schemas shared between the API and websocket payloads."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class Track(BaseModel):
    """Represents a track retrieved from YouTube."""

    video_id: str = Field(..., description="YouTube video identifier")
    title: str = Field(..., description="Title of the track")
    url: str = Field(..., description="Canonical URL to the YouTube video")
    channel: Optional[str] = Field(default=None, description="Channel or artist name")
    duration: Optional[float] = Field(default=None, description="Duration of the track in seconds")
    thumbnail: Optional[str] = Field(default=None, description="URL for the video thumbnail")


class Device(BaseModel):
    """A device that is connected to the playback session."""

    device_id: str
    name: str
    is_active: bool = Field(default=False, description="Whether the device is currently playing audio")
    is_player: bool = Field(default=False, description="Whether the device is capable of direct playback")


class PlaybackState(BaseModel):
    """Describes the state of the shared playback session."""

    current_track: Optional[Track] = None
    queue: List[Track] = Field(default_factory=list)
    is_playing: bool = False
    current_device: Optional[str] = None
    position_seconds: Optional[float] = Field(default=None, description="Approximate playback position in seconds")
    devices: List[Device] = Field(default_factory=list)


class SearchResponse(BaseModel):
    """Response model for YouTube search results."""

    items: List[Track]
