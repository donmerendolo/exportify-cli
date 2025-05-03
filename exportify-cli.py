import configparser
import csv
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from tabulate import tabulate
from tqdm.auto import tqdm

# Default options for the CLI (used in [exportify-cli] section)
CLI_DEFAULTS = {
    "uris": "false",
    "external_ids": "false",
    "with_bar": "true",
}

# Default bar format for progress bars
DEFAULT_BAR_FORMAT = (
    "{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}"
    "/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]"
)

# Max length for playlist name in progress bar
DESC_LENGTH = 21

# Configure logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def validate_config(config: configparser.ConfigParser) -> bool:
    """Validate that the Spotify section exists and has all required keys, and that the redirect URI is valid."""
    if not config.has_section("spotify"):
        logger.error("Configuration missing [spotify] section.")
        return False
    spotify_cfg = config["spotify"]
    required = ("client_id", "client_secret", "redirect_uri")
    missing = [k for k in required if not spotify_cfg.get(k, "").strip()]
    if missing:
        logger.error(
            f"Missing or empty keys in [spotify] section: {', '.join(missing)}"
        )
        return False
    redirect = spotify_cfg["redirect_uri"].strip()
    if not redirect.startswith(("http://", "https://")):
        logger.error(f"Invalid redirect URI: {redirect}.")
        return False
    return True


def ensure_exportify_cli(config: configparser.ConfigParser, config_path: Path) -> None:
    """Ensure [exportify-cli] section exists with all required options, write back if changed."""
    changed = False
    if not config.has_section("exportify-cli"):
        config.add_section("exportify-cli")
        changed = True
    for key, default in CLI_DEFAULTS.items():
        if not config.has_option("exportify-cli", key):
            config.set("exportify-cli", key, default)
            changed = True
    if changed:
        logger.info(f"Adding missing [exportify-cli] defaults to {config_path}")
        with config_path.open("w") as f:
            config.write(f)


def load_config(config_path: Path) -> configparser.ConfigParser:
    """Load configuration, validate, prompt if needed, and ensure CLI defaults."""
    config = configparser.ConfigParser()
    # Read existing
    if config_path.exists():
        config.read(config_path)
    # Valid Spotify?
    if validate_config(config):
        ensure_exportify_cli(config, config_path)
        logger.info(f"Config loaded from {config_path}")
        return config
    # Prompt user to create config
    logger.info(f"Config not found or invalid at {config_path}, creating new.")
    click.echo(
        """
File "config.cfg" not found or invalid. Let's create it.

1. Go to Spotify Developer Dashboard (https://developer.spotify.com/dashboard).
2. Create a new app.
3. Set a name and description for your app.
4. Add a redirect URI (e.g. http://127.0.0.1:3000/callback).

Now copy the Client ID, Client Secret and Redirect URI and paste below:
"""
    )
    spotify_cfg = {
        "client_id": click.prompt("Spotify Client ID", type=str),
        "client_secret": click.prompt(
            "Spotify Client Secret", hide_input=True, type=str
        ),
        "redirect_uri": click.prompt(
            "Redirect URI",
            type=str,
            default="http://127.0.0.1:3000/callback",
        ),
    }
    config["spotify"] = spotify_cfg
    if not validate_config(config):
        logger.error("Invalid Spotify configuration.")
        sys.exit(1)
    # Write initial config
    with config_path.open("w") as f:
        config.write(f)
    logger.info(f"Wrote new config to {config_path}")
    # Add CLI defaults
    ensure_exportify_cli(config, config_path)
    return config


def init_spotify_client(cfg: configparser.ConfigParser) -> spotipy.Spotify:
    """Initialize Spotify client with OAuth manager."""
    creds = cfg["spotify"]
    auth = SpotifyOAuth(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        redirect_uri=creds["redirect_uri"],
        scope="playlist-read-private playlist-read-collaborative user-library-read",
        open_browser=True,
        cache_path=".cache",
    )
    return spotipy.Spotify(auth_manager=auth, retries=10)


def sanitize_filename(name: str, ext: str) -> str:
    """Convert a playlist name into a safe filename."""
    safe = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in name)
    return f"{safe.strip().replace(' ', '_').lower()}.{ext}"


def write_file(file_path: Path, data: list[dict], file_format: str = "csv") -> None:
    """Write list of dicts to file (CSV or JSON)."""
    if not data:
        logger.warning("No data to write; skipping file.")
        return
    if file_format == "csv":
        headers = list(data[0].keys())
        with file_path.open("w", newline="", encoding="utf-8") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            for row in data:
                writer.writerow(row)
    elif file_format == "json":
        with file_path.open("w", encoding="utf-8") as jsonfile:
            json.dump(data, jsonfile, ensure_ascii=False, indent=4)
    logger.info(f"Exported to {file_path}")


class SpotifyExporter:
    """Class to handle exporting Spotify playlists."""

    def __init__(
        self,
        spotify_client: spotipy.Spotify,
        file_format: str,
        include_uris: bool,
        external_ids: bool,
        with_bar_flag: bool,
    ) -> None:
        self.spotify = spotify_client
        self.file_format = file_format
        self.include_uris = include_uris
        self.external_ids = external_ids
        self.with_bar_flag = with_bar_flag

    def _fetch_all_items(
        self,
        fetch_func,
        key: str | None = None,
        *args: Any,
        desc: str | None = None,
        bar_format: str = DEFAULT_BAR_FORMAT,
        show_bar: bool = True,
        **kwargs: Any,
    ) -> list[dict]:
        # ... existing pagination and batching logic unchanged ...
        pass  # existing implementation here

    def get_playlists(self) -> list[dict]:
        items = self._fetch_all_items(
            self.spotify.current_user_playlists,
            "items",
            desc="Playlists",
            show_bar=False,
        )
        liked_total = self.spotify.current_user_saved_tracks(limit=1)["total"]
        liked = {
            "name": "Liked Songs",
            "id": "liked_songs",
            "tracks": {"total": liked_total},
        }
        return [liked, *items]

    def export_playlist(self, playlist: dict, output_dir: Path) -> None:
        # ... existing export logic unchanged ...
        pass  # existing implementation here


@click.command()
@click.help_option("-h", "--help")
@click.option(
    "--config",
    "-c",
    default="config.cfg",
    type=click.Path(),
    help="Path to configuration file",
)
@click.option(
    "--output",
    "-o",
    default="./playlists",
    type=click.Path(),
    help="Directory to save files",
)
@click.option(
    "--format",
    "-f",
    "file_format",
    type=click.Choice(["csv", "json"]),
    default="csv",
    help="Output file format",
)
@click.option(
    "--all",
    "-a",
    "export_all",
    is_flag=True,
    help="Export all playlists",
)
@click.option(
    "--playlist",
    "-p",
    multiple=True,
    help="Names or IDs of playlists to export",
)
@click.option(
    "--list",
    "-l",
    "list_only",
    is_flag=True,
    help="List available playlists",
)
@click.option(
    "--uris/--no-uris",
    "include_uris",
    default=None,
    help="Include album and artist URIs (overrides config)",
)
@click.option(
    "--external-ids/--no-external-ids",
    "external_ids",
    default=None,
    help="Include track ISRC and album UPC (overrides config)",
)
@click.option(
    "--with-bar/--no-bar",
    "with_bar_flag",
    default=None,
    help="Show progress bar (overrides config)",
)
def main(
    config: str,
    output: str,
    file_format: str,
    export_all: bool,
    playlist: tuple[str, ...],
    list_only: bool,
    include_uris: bool,
    external_ids: bool,
    with_bar_flag: bool,
) -> None:
    """CLI entrypoint for exporting Spotify playlists."""
    cfg_path = Path(config)
    cfg = load_config(cfg_path)

    # Resolve config vs CLI
    uris_flag = (
        include_uris
        if include_uris is not None
        else cfg.getboolean("exportify-cli", "uris")
    )
    ext_flag = (
        external_ids
        if external_ids is not None
        else cfg.getboolean("exportify-cli", "external_ids")
    )
    bar_flag = (
        with_bar_flag
        if with_bar_flag is not None
        else cfg.getboolean("exportify-cli", "with_bar")
    )

    client = init_spotify_client(cfg)
    exporter = SpotifyExporter(
        spotify_client=client,
        file_format=file_format,
        include_uris=uris_flag,
        external_ids=ext_flag,
        with_bar_flag=bar_flag,
    )

    playlists = exporter.get_playlists()
    if list_only:
        playlist_data = [[p["name"], p["id"], p["tracks"]["total"]] for p in playlists]
        terminal_width = os.get_terminal_size().columns
        click.echo(
            tabulate(
                playlist_data,
                headers=["Name", "ID", "Tracks"],
                tablefmt="simple",
                maxcolwidths=[terminal_width - 34, None, None],
            )
        )
        sys.exit(0)

    # Determine targets
    targets = []
    if export_all:
        targets = playlists
    else:
        for p in playlists:
            if p["name"] in playlist or p["id"] in playlist:
                targets.append(p)
        for term in playlist:
            if any(p for p in targets if p["name"] == term or p["id"] == term):
                continue
            matches = [
                p for p in playlists if p["name"].lower().startswith(term.lower())
            ]
            if len(matches) == 1:
                targets.append(matches[0])
            elif len(matches) > 1:
                click.echo(
                    f"Ambiguous prefix '{term}': matches {', '.join(p['name'] for p in matches)}. Skipping."
                )
    targets = list({p["id"]: p for p in targets}.values())
    if not targets:
        click.echo("No matching playlists found.")
        sys.exit(1)

    out_dir = Path(output)
    for pl in targets:
        exporter.export_playlist(pl, out_dir)


if __name__ == "__main__":
    main()
