import json

from app.config import LIMITS, Settings, load_settings, read_env_file, save_settings, settings_path


def test_defaults_when_missing(tmp_path):
    assert load_settings(tmp_path / "nope.json") == Settings()


def test_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    s = Settings(autostart=True, compact_width=200, expanded_width=500, excluded_processes=["VLC.exe"])
    save_settings(s, path)
    loaded = load_settings(path)
    assert loaded.autostart is True
    assert loaded.compact_width == 200
    assert loaded.expanded_width == 500
    assert loaded.excluded_processes == ["vlc.exe"]       # normalised to lower case


def test_default_path_is_under_home(isolated_home):
    save_settings(Settings(height=40))
    assert settings_path().parent == isolated_home
    assert load_settings().height == 40


def test_corrupted_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_settings(path) == Settings()
    assert (tmp_path / "s.json.bad").exists()


def test_invalid_values_are_clamped_or_replaced(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({
        "compact_width": 99999,
        "height": -5,
        "collapse_delay_ms": "slow",
        "show_cover": "yes",
        "hide_in_games": 0,
        "excluded_processes": "a.exe\n\nB.EXE\na.exe",
        "game_processes": [1, None, "Game.exe"],
        "unknown_key": 42,
        "spotify_client_id": "bad id; rm -rf",
    }), encoding="utf-8")
    s = load_settings(path)
    assert s.compact_width == LIMITS["compact_width"][1]
    assert s.height == LIMITS["height"][0]
    assert s.collapse_delay_ms == Settings().collapse_delay_ms
    assert s.show_cover is True and s.hide_in_games is True    # wrong types -> defaults
    assert s.excluded_processes == ["a.exe", "b.exe"]
    assert s.game_processes == ["game.exe"]
    assert s.spotify_client_id == ""
    assert s.expanded_width >= s.compact_width + 60


def test_non_dict_json(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_settings(path) == Settings()


def test_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nSPOTIFY_CLIENT_ID = 'abc123'\nBROKEN LINE\n", encoding="utf-8")
    assert read_env_file(env) == {"SPOTIFY_CLIENT_ID": "abc123"}
    assert read_env_file(tmp_path / "missing") == {}
