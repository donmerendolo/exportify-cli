"""Configuration: file locations, loading, validation and first-run creation.

Config lives in TOML (config.toml). Legacy config.cfg files are migrated
automatically. The file is never rewritten by the program after creation;
missing options simply fall back to defaults at read time.
"""

import configparser
import logging
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

import click

logger = logging.getLogger(__name__)

APP_NAME = "exportify-cli"

# Read-time defaults for the [exportify-cli] table (nothing is written back).
CLI_DEFAULTS: dict[str, Any] = {
    "format": ["csv"],
    "output": "./playlists",
    "playlists": [],
    "users": [],
    "no_bar": False,
    "sort_key": "spotify_default",
    "reverse": False,
    "fields": [],
}

DEFAULT_FIELDS = [
    "Position",
    "Track Name",
    "Album Name",
    "Artist Name(s)",
    "Release Date",
    "Duration_ms",
    "Popularity",
    "Added By",
    "Added At",
    "Record Label",
]

CONFIG_TEMPLATE = """\
[spotify]
client_id = "{client_id}"
redirect_uri = "{redirect_uri}"

[exportify-cli]
# Output format(s): "csv", "json".
format = ["csv"]

# Directory where exports are written.
output = "./playlists"

# Playlists exported when no -a/-p/-u is given (names or IDs).
playlists = []

# Users whose public playlists to export (IDs).
users = []

# Hide the progress bar.
no_bar = false

# Key to sort tracks by ("spotify_default" keeps Spotify's order).
sort_key = "spotify_default"

# Reverse the sort order.
reverse = false

# Fields to export. All available fields:
# "Position", "Track URI", "Artist URI(s)", "Album URI", "Track Name",
# "Album Name", "Artist Name(s)", "Release Date", "Duration_ms", "Popularity",
# "Added By", "Added At", "Record Label", "Track ISRC", "Album UPC"
fields = {fields}
"""


def app_base_dir() -> Path:
    """Directory next to the running program (script or frozen binary)."""
    if getattr(sys, "frozen", False):  # PyInstaller --onefile
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def xdg_config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP_NAME


def xdg_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / APP_NAME


def default_config_path() -> Path:
    """First existing of: ./config.toml, legacy locations, XDG path.
    A config.cfg found where no config.toml exists gets migrated.
    If nothing exists, the XDG path (where a new one is created)."""
    dirs = [Path.cwd(), app_base_dir(), xdg_config_dir()]
    for d in dirs:
        if (d / "config.toml").is_file():
            return d / "config.toml"
    for d in dirs:
        if (d / "config.cfg").is_file():
            return migrate_legacy_config(d / "config.cfg")
    return xdg_config_dir() / "config.toml"


def migrate_legacy_config(cfg_path: Path) -> Path:
    """Convert an INI config.cfg to config.toml next to it. Keeps the .cfg."""
    toml_path = cfg_path.with_name("config.toml")
    old = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    old.read(cfg_path)

    def _split(value: str) -> list[str]:
        return [v.strip() for v in value.replace(",", "\n").splitlines() if v.strip()]

    spotify = dict(old["spotify"]) if old.has_section("spotify") else {}
    cli = dict(old["exportify-cli"]) if old.has_section("exportify-cli") else {}

    fields = _split(cli.get("fields", "")) or DEFAULT_FIELDS
    # old format option allowed space *or* comma separation
    formats = cli.get("format", "csv").replace(",", " ").split() or ["csv"]
    lines = [
        "[spotify]",
        f'client_id = "{spotify.get("client_id", "")}"',
        f'redirect_uri = "{spotify.get("redirect_uri", "")}"',
        "",
        "[exportify-cli]",
        f"format = {formats!r}".replace("'", '"'),
        f'output = "{cli.get("output", "./playlists")}"',
        f"playlists = {_split(cli.get('playlists', ''))!r}".replace("'", '"'),
        f"users = {_split(cli.get('users', ''))!r}".replace("'", '"'),
        f"no_bar = {cli.get('no_bar', 'false').lower()}",
        f'sort_key = "{cli.get("sort_key", "spotify_default")}"',
        f"reverse = {cli.get('reverse', 'false').lower()}",
        f"fields = {fields!r}".replace("'", '"'),
        "",
    ]
    toml_path.write_text("\n".join(lines), encoding="utf-8")
    click.echo(f"Migrated legacy config '{cfg_path}' to '{toml_path}'.")
    return toml_path


def validate_config(config: dict) -> bool:
    """Spotify table exists, has the required keys and a sane redirect URI."""
    spotify = config.get("spotify")
    if not isinstance(spotify, dict):
        logger.error("Configuration missing [spotify] section.")
        return False
    missing = [
        k for k in ("client_id", "redirect_uri") if not str(spotify.get(k, "")).strip()
    ]
    if missing:
        logger.error(
            f"Missing or empty keys in [spotify] section: {', '.join(missing)}"
        )
        return False
    if not str(spotify["redirect_uri"]).strip().startswith(("http://", "https://")):
        logger.error(f"Invalid redirect URI: {spotify['redirect_uri']}.")
        return False
    return True


def create_config(config_path: Path) -> None:
    """Interactive first-run wizard; writes a commented template."""
    click.echo(
        f'No valid config found. Creating "{config_path}".\n'
        "Use a Client ID and Redirect URI that belong to the same Spotify app.\n"
    )
    client_id = click.prompt("Spotify Client ID", type=str).strip()
    redirect_uri = click.prompt("Redirect URI", type=str).strip()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fields_toml = "[" + ", ".join(f'"{f}"' for f in DEFAULT_FIELDS) + "]"
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            client_id=client_id, redirect_uri=redirect_uri, fields=fields_toml
        ),
        encoding="utf-8",
    )
    logger.info(f"Wrote new config to {config_path}")


def read_toml(config_path: Path) -> dict:
    try:
        with config_path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        click.echo(f"Error: could not parse '{config_path}': {e}", err=True)
        sys.exit(1)


def load_config(config_path: Path, explicit: bool) -> dict:
    """Load and validate config; run the creation wizard if needed.

    An explicitly passed path (-c) that doesn't exist is an error, not a
    silent wizard in an unexpected location. An explicit .cfg gets migrated.
    """
    if explicit and config_path.suffix == ".cfg" and config_path.is_file():
        config_path = migrate_legacy_config(config_path)

    if config_path.is_file():
        config = read_toml(config_path)
        if validate_config(config):
            return config
        if explicit:
            click.echo(f"Error: config file '{config_path}' is invalid.", err=True)
            sys.exit(1)
    elif explicit:
        click.echo(f"Error: config file '{config_path}' does not exist.", err=True)
        sys.exit(1)

    create_config(config_path)
    config = read_toml(config_path)
    if not validate_config(config):
        logger.error("Invalid Spotify configuration.")
        sys.exit(1)
    return config


def opt(config: dict, key: str) -> Any:
    """Read an [exportify-cli] option, falling back to CLI_DEFAULTS.
    String values for list options are coerced (comma-separated)."""
    value = config.get("exportify-cli", {}).get(key, CLI_DEFAULTS[key])
    if isinstance(CLI_DEFAULTS[key], list) and isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    return value


def token_cache_path(config_path: Path) -> Path:
    """Spotipy token cache. Reuse a legacy `.cache` next to the config if
    present (avoids forcing re-auth), otherwise use the XDG cache dir."""
    legacy = config_path.parent / ".cache"
    if legacy.is_file():
        return legacy
    return xdg_cache_dir() / "token.cache"
