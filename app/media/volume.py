"""Spotify's own volume in the Windows mixer (Core Audio ISimpleAudioVolume of Spotify's session).

Only Spotify gets louder/quieter - the system volume and other apps are untouched, and unlike the
Web API volume endpoint this needs no Premium and no network. The audio session appears only after
Spotify has played something since it started; until then volume is reported as unavailable.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

RESCAN_INTERVAL_S = 3.0


class SpotifyAudioVolume:
    def __init__(self, process_name: str = "spotify.exe") -> None:
        self.process_name = process_name
        self._volume = None              # cached ISimpleAudioVolume
        self._last_scan = 0.0

    def _find(self):
        now = time.monotonic()
        if self._volume is not None:
            return self._volume
        if now - self._last_scan < RESCAN_INTERVAL_S:
            return None
        self._last_scan = now
        try:
            from pycaw.pycaw import AudioUtilities

            for session in AudioUtilities.GetAllSessions():
                proc = session.Process
                if proc is not None and proc.name().lower() == self.process_name:
                    self._volume = session.SimpleAudioVolume
                    return self._volume
        except Exception as exc:
            log.debug("Audio session scan failed: %s", exc)
        return None

    def get(self) -> float | None:
        for _ in range(2):              # second attempt after a stale session (Spotify restarted)
            volume = self._find()
            if volume is None:
                return None
            try:
                return 0.0 if volume.GetMute() else float(volume.GetMasterVolume())
            except Exception:
                self._volume, self._last_scan = None, 0.0
        return None

    def set(self, level: float) -> bool:
        level = min(1.0, max(0.0, float(level)))
        for _ in range(2):
            volume = self._find()
            if volume is None:
                return False
            try:
                if volume.GetMute():
                    volume.SetMute(0, None)
                volume.SetMasterVolume(level, None)
                return True
            except Exception:
                self._volume, self._last_scan = None, 0.0
        return False
