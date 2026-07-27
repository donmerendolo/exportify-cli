import json

import pytest

from exportify_cli import cli, config, export

# --- fields ---


def test_parse_fields_default_excludes_uris_and_ids():
    fields = export.parse_fields([], include_uris=False, external_ids=False)
    assert "Track URI" not in fields
    assert "Track ISRC" not in fields
    assert "Track Name" in fields


def test_parse_fields_aliases_and_dedup():
    assert export.parse_fields(["name", "artist", "name"], False, False) == [
        "Track Name",
        "Artist Name(s)",
    ]


def test_parse_fields_flags_add_headers():
    fields = export.parse_fields(["name"], include_uris=True, external_ids=True)
    assert "Track URI" in fields and "Album UPC" in fields


def test_parse_fields_unknown_ignored():
    assert export.parse_fields(["name", "bogus"], False, False) == ["Track Name"]


# --- playlist item / track field (Feb 2026 API migration) ---


def test_track_of_prefers_new_item_key():
    assert export._track_of({"item": {"name": "New"}, "track": {"name": "Old"}}) == {
        "name": "New"
    }


def test_track_of_falls_back_to_legacy_track_key():
    assert export._track_of({"track": {"name": "Old"}}) == {"name": "Old"}


def test_track_of_none_when_neither_key_present():
    assert export._track_of({"added_at": "2024-01-01"}) is None


def test_track_count_prefers_new_items_key():
    assert export._track_count({"items": {"total": 5}, "tracks": {"total": 1}}) == 5


def test_track_count_falls_back_to_legacy_tracks_key():
    assert export._track_count({"tracks": {"total": 3}}) == 3


# --- sorting ---


def test_sort_value_numeric_not_lexicographic():
    assert sorted(["100", "9", "10"], key=export.sort_value) == ["9", "10", "100"]


def test_sort_value_none_sorts_first():
    assert sorted(["b", None, "a", 5], key=export.sort_value) == [None, 5, "a", "b"]


# --- input cleaning ---


def test_clean_playlist_input():
    inputs = [
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x",
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        "My Playlist",
    ]
    assert cli.clean_playlist_input(inputs) == [
        "37i9dQZF1DXcBWIGoYBM5M",
        "37i9dQZF1DXcBWIGoYBM5M",
        "My Playlist",
    ]


def test_clean_user_input():
    assert cli.clean_user_input(
        ["https://open.spotify.com/user/wizzler?si=x", "spotify:user:wizzler"]
    ) == ["wizzler", "wizzler"]


# --- formats ---


def _cfg(format_value) -> dict:
    return {"exportify-cli": {"format": format_value}}


def test_resolve_formats_cli_wins():
    assert cli.resolve_formats(("json",), _cfg(["csv"])) == ["json"]


def test_resolve_formats_from_config():
    assert set(cli.resolve_formats((), _cfg(["CSV", "json"]))) == {"csv", "json"}
    # string values are coerced for convenience
    assert set(cli.resolve_formats((), _cfg("csv, json"))) == {"csv", "json"}


def test_resolve_formats_invalid_errors():
    with pytest.raises(SystemExit):
        cli.resolve_formats((), _cfg(["xml"]))
    with pytest.raises(SystemExit):
        cli.resolve_formats((), _cfg([]))


# --- sort key resolution ---


def test_resolve_sort_key_case_insensitive():
    assert cli.resolve_sort_key("popularity") == "Popularity"
    assert cli.resolve_sort_key("artist name(s)") == "Artist Name(s)"


def test_resolve_sort_key_invalid_exits():
    with pytest.raises(SystemExit):
        cli.resolve_sort_key("nope")


# --- config ---

VALID_TOML = '[spotify]\nclient_id = "abc"\nredirect_uri = "http://localhost:8888"\n'


def test_load_config_does_not_rewrite_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID_TOML)
    config.load_config(path, explicit=True)
    assert path.read_text() == VALID_TOML  # bare reads never touch the file


def test_load_config_explicit_missing_errors(tmp_path):
    with pytest.raises(SystemExit):
        config.load_config(tmp_path / "nope.toml", explicit=True)


def test_load_config_bad_toml_errors(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("not = valid = toml")
    with pytest.raises(SystemExit):
        config.load_config(path, explicit=True)


def test_opt_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(VALID_TOML)
    cfg = config.load_config(path, explicit=True)
    assert config.opt(cfg, "format") == ["csv"]
    assert config.opt(cfg, "no_bar") is False
    assert config.opt(cfg, "users") == []


def test_config_template_is_valid_toml_with_default_fields():
    import tomllib

    fields_toml = "[" + ", ".join(f'"{f}"' for f in config.DEFAULT_FIELDS) + "]"
    text = config.CONFIG_TEMPLATE.format(
        client_id="abc", redirect_uri="http://x", fields=fields_toml
    )
    c = tomllib.loads(text)
    assert config.validate_config(c)
    section = c["exportify-cli"]
    # template default == code default
    assert export.parse_fields(section["fields"], False, False) == export.parse_fields(
        [], False, False
    )
    assert "uris" not in section and "external_ids" not in section  # no legacy keys


def test_migrate_legacy_config(tmp_path):
    cfg = tmp_path / "config.cfg"
    cfg.write_text(
        "[spotify]\nclient_id = abc\nredirect_uri = http://localhost\n"
        "[exportify-cli]\nformat = csv json\nplaylists = one, two\nno_bar = true\n"
        "uris = false\nexternal_ids = false\n"
    )
    toml_path = config.migrate_legacy_config(cfg)
    c = config.read_toml(toml_path)
    assert config.validate_config(c)
    assert c["exportify-cli"]["format"] == ["csv", "json"]
    assert c["exportify-cli"]["playlists"] == ["one", "two"]
    assert c["exportify-cli"]["no_bar"] is True
    assert "uris" not in c["exportify-cli"]  # legacy keys dropped
    assert cfg.exists()  # original kept


# --- csv list joining ---


def test_write_file_csv_joins_lists(tmp_path):
    out = tmp_path / "pl"
    export.write_file(
        out,
        ["Track Name", "Artist Name(s)"],
        [{"Track Name": "Song", "Artist Name(s)": ["A", "B"]}],
        ["csv", "json"],
    )
    assert '"A, B"' in (tmp_path / "pl.csv").read_text()
    assert json.loads((tmp_path / "pl.json").read_text())[0]["Artist Name(s)"] == [
        "A",
        "B",
    ]


# --- stdout output ---


def test_write_stdout_csv(capsys):
    export.write_stdout(
        ["Track Name", "Artist Name(s)"],
        [{"Track Name": "Song", "Artist Name(s)": ["A", "B"]}],
        "csv",
    )
    out = capsys.readouterr().out
    assert out.startswith("Track Name,") and '"A, B"' in out


def test_write_stdout_json(capsys):
    export.write_stdout(["Track Name"], [{"Track Name": "Song"}], "json")
    assert json.loads(capsys.readouterr().out) == [{"Track Name": "Song"}]
