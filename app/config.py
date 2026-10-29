"""User settings: dataclass, validation and JSON persistence.

Paths:
    %APPDATA%\\DynamicIsland\\settings.json      - settings
    %APPDATA%\\DynamicIsland\\spotify_token.bin  - DPAPI-encrypted Spotify tokens
    %LOCALAPPDATA%\\DynamicIsland\\covers        - album art cache
    %LOCALAPPDATA%\\DynamicIsland\\logs          - logs

Setting DYNAMIC_ISLAND_HOME puts everything under one folder (handy for tests / portable use).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from app import APP_NAME

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- paths
def _home_override() -> Path | None:
    value = os.environ.get("DYNAMIC_ISLAND_HOME")
    return Path(value) if value else None


def config_dir() -> Path:
    base = _home_override()
    if base is None:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def data_dir() -> Path:
    base = _home_override()
    if base is None:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def settings_path() -> Path:
    return config_dir() / "settings.json"


def token_path() -> Path:
    return config_dir() / "spotify_token.bin"


def covers_dir() -> Path:
    path = data_dir() / "covers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- settings
# (min, max) for every integer setting
LIMITS: dict[str, tuple[int, int]] = {
    "compact_width": (120, 400),
    "expanded_width": (320, 700),
    "height": (26, 56),
    "collapse_delay_ms": (0, 3000),
    "spotify_redirect_port": (1024, 65535),
}
MIN_EXPANSION = 60  # expanded width must exceed compact width by at least this much
MAX_LIST_ITEMS = 200
MAX_ITEM_LEN = 260


@dataclass
class Settings:
    autostart: bool = False
    overlay_visible: bool = True          # manual show/hide from tray
    hide_in_games: bool = True
    hide_in_fullscreen: bool = True
    game_mode_manual: bool = False        # forced "game mode": overlay hidden until turned off
    monitor: str = ""                     # QScreen name, "" = primary monitor
    compact_width: int = 190
    expanded_width: int = 440
    height: int = 34
    collapse_delay_ms: int = 500
    show_cover: bool = True
    show_progress: bool = True
    equalizer: bool = True
    spotify_client_id: str = ""
    spotify_redirect_port: int = 8765
    excluded_processes: list[str] = field(default_factory=list)  # never hide for these
    game_processes: list[str] = field(default_factory=list)      # always treat as games

    def validated(self) -> "Settings":
        """Return a copy with every value coerced into a safe range."""
        defaults = Settings()
        clean: dict = {}
        for f in fields(self):
            value = getattr(self, f.name)
            default = getattr(defaults, f.name)
            if isinstance(default, bool):
                clean[f.name] = value if isinstance(value, bool) else default
            elif isinstance(default, int):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    value = default
                lo, hi = LIMITS[f.name]
                clean[f.name] = int(min(max(int(value), lo), hi))
            elif isinstance(default, str):
                clean[f.name] = value.strip()[:MAX_ITEM_LEN] if isinstance(value, str) else default
            elif isinstance(default, list):
                clean[f.name] = _clean_process_list(value)
        result = Settings(**clean)
        if result.expanded_width < result.compact_width + MIN_EXPANSION:
            result.expanded_width = min(result.compact_width + MIN_EXPANSION, LIMITS["expanded_width"][1])
        # client ids are 32 hex chars; anything else is dropped rather than sent to Spotify
        cid = result.spotify_client_id
        if cid and not (len(cid) <= 64 and cid.isalnum()):
            result.spotify_client_id = ""
        return result

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        raw = cls()
        for key, value in data.items():
            if key in known:
                setattr(raw, key, value)
        return raw.validated()


def _clean_process_list(value) -> list[str]:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip().strip('"').lower()
        if name and len(name) <= MAX_ITEM_LEN and name not in result:
            result.append(name)
        if len(result) >= MAX_LIST_ITEMS:
            break
    return result


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Settings file is unreadable (%s); using defaults", exc)
        try:
            path.replace(path.with_suffix(".json.bad"))
        except OSError:
            pass
        return Settings()
    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.validated().to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE lines). Values are never executed."""
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result
