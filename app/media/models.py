"""Source-independent playback state shared between media sources and the UI."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum


class PlaybackStatus(Enum):
    NONE = "none"        # no player / nothing loaded
    STOPPED = "stopped"
    PAUSED = "paused"
    PLAYING = "playing"


class RepeatMode(Enum):
    OFF = "off"
    CONTEXT = "context"      # playlist / album
    TRACK = "track"

    def next(self) -> "RepeatMode":
        """Spotify's own cycle: off -> playlist -> track -> off."""
        return {RepeatMode.OFF: RepeatMode.CONTEXT, RepeatMode.CONTEXT: RepeatMode.TRACK,
                RepeatMode.TRACK: RepeatMode.OFF}[self]


class Command(Enum):
    PLAY = "play"
    PAUSE = "pause"
    TOGGLE = "toggle"
    NEXT = "next"
    PREVIOUS = "previous"


@dataclass(frozen=True)
class Capabilities:
    play: bool = False
    pause: bool = False
    next: bool = False
    previous: bool = False
    seek: bool = False
    repeat: bool = False

    def allows(self, command: Command) -> bool:
        if command is Command.TOGGLE:
            return self.play or self.pause
        return getattr(self, command.value)


@dataclass(frozen=True)
class MediaState:
    source: str = "none"                 # "smtc", "webapi" or "none"
    player_available: bool = False       # the player (Spotify) is running / reachable
    title: str = ""
    artist: str = ""
    album: str = ""
    status: PlaybackStatus = PlaybackStatus.NONE
    position_ms: int | None = None
    duration_ms: int | None = None
    position_timestamp: float = field(default_factory=time.monotonic)  # monotonic time of position_ms
    cover_path: str | None = None
    caps: Capabilities = Capabilities()
    volume: float | None = None          # 0..1, only reported by the Web API (device volume)
    repeat: RepeatMode | None = None     # None = the player does not report it

    @property
    def has_track(self) -> bool:
        return bool(self.title)

    @property
    def is_playing(self) -> bool:
        return self.status is PlaybackStatus.PLAYING

    @property
    def track_key(self) -> tuple[str, str, str]:
        return (self.title, self.artist, self.album)

    def position_at(self, now: float | None = None) -> int | None:
        """Position extrapolated to `now` (monotonic seconds) while playing."""
        if self.position_ms is None:
            return None
        pos = self.position_ms
        if self.is_playing:
            now = time.monotonic() if now is None else now
            pos += int(max(0.0, now - self.position_timestamp) * 1000)
        if self.duration_ms:
            pos = min(pos, self.duration_ms)
        return max(0, pos)

    def same_as(self, other: "MediaState | None", tolerance_ms: int = 1500) -> bool:
        """Equal apart from position drift that interpolation already accounts for."""
        if other is None:
            return False
        if replace(self, position_ms=None, position_timestamp=0.0) != replace(
            other, position_ms=None, position_timestamp=0.0
        ):
            return False
        a, b = self.position_at(), other.position_at()
        if a is None or b is None:
            return a is b
        return abs(a - b) <= tolerance_ms


EMPTY_STATE = MediaState()
