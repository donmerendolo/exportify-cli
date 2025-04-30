import configparser
import csv
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


# Environment variable names for credentials
ENV_CLIENT_ID = "SPOTIPY_CLIENT_ID"
ENV_CLIENT_SECRET = "SPOTIPY_CLIENT_SECRET"
ENV_REDIRECT_URI = "SPOTIPY_REDIRECT_URI"

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


def load_config(config_path: Path) -> configparser.ConfigParser:
    """Load configuration from environment or file, or prompt user to create one."""
    config = configparser.ConfigParser()

    # Priority: environment variables
    client_id = os.getenv(ENV_CLIENT_ID)
    client_secret = os.getenv(ENV_CLIENT_SECRET)
    redirect_uri = os.getenv(ENV_REDIRECT_URI)

    if client_id and client_secret and redirect_uri:
        config["spotify"] = {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        }
        return config

    # Fallback to file
    if config_path.exists():
        config.read(config_path)
        return config

    # Prompt for credentials
    logger.info(f"Config not found at {config_path}, creating new config file.")
    spotify_cfg = {
        "client_id": click.prompt("Spotify Client ID", type=str),
        "client_secret": click.prompt("Spotify Client Secret", hide_input=True),
        "redirect_uri": click.prompt("Redirect URI", type=str),
    }
    config["spotify"] = spotify_cfg
    with config_path.open("w") as cfg:
        config.write(cfg)
    logger.info(f"Wrote new config to {config_path}")
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


def sanitize_filename(name: str, ext: str = ".csv") -> str:
    """Convert a playlist name into a safe filename."""
    safe = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in name)
    return f"{safe.strip().replace(' ', '_').lower()}{ext}"


def write_csv(file_path: Path, data: list[dict]) -> None:
    """Write list of dicts to CSV, flattening lists to comma-separated values."""
    if not data:
        logger.warning("No data to write; skipping CSV.")
        return

    headers = list(data[0].keys())
    with file_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=headers)
        writer.writeheader()
        for row in data:
            writer.writerow(row)
    logger.info(f"Exported to {file_path}")


class SpotifyExporter:
    """Class to handle exporting Spotify playlists to CSV files."""

    def __init__(
        self,
        spotify_client: spotipy.Spotify,
        include_uris: bool = False,
        include_ids: bool = False,
    ) -> None:
        """Initialize the exporter with a Spotify client."""
        self.spotify = spotify_client
        self.include_uris = include_uris
        self.include_ids = include_ids

    def _fetch_all_items(
        self,
        fetch_func,
        key: str = None,
        *args: Any,
        desc: str | None = None,
        bar_format: str = DEFAULT_BAR_FORMAT,
        show_bar: bool = True,
        **kwargs: Any,
    ) -> list[dict]:
        """Fetch all paginated or batched items from a Spotify endpoint.

        - If the first positional arg is a list, treats it as an ID list for a batch
          endpoint (e.g. `self.spotify.albums`), calls in chunks of 20, and returns
          results[key] aggregated.
        - Otherwise treats as a paginated endpoint, calling `fetch_func(*args, **kwargs)`
          then .next() until exhausted, aggregating results["items"] or results[key].
        """
        items: list[dict] = []

        # --- Batch mode (e.g. spotify.albums) ---
        if args and isinstance(args[0], list):
            id_list: list[str] = args[0]
            total = len(id_list)
            desc_text = desc or fetch_func.__name__
            # build 20-item batches
            batches = [id_list[i : i + 20] for i in range(0, total, 20)]

            pbar = (
                tqdm(total=total, desc=desc_text, unit="album", bar_format=bar_format)
                if show_bar
                else None
            )

            for batch in batches:
                results = fetch_func(batch, **kwargs)
                page_items = results.get(key, [])
                items.extend(page_items)
                if pbar:
                    pbar.update(len(page_items))

            if pbar:
                pbar.close()
            return items

        # --- Paginated mode (e.g. playlist_tracks, saved_tracks) ---
        # Initial fetch
        results = fetch_func(*args, **kwargs)
        page_items = results.get(key, [])
        items.extend(page_items)

        total = results.get("total")
        # fallback to a name field or the function name if no desc given
        desc_text = desc or (results.get("name") or fetch_func.__name__)

        pbar = (
            tqdm(total=total, desc=desc_text, unit="track", bar_format=bar_format)
            if show_bar
            else None
        )
        if pbar:
            pbar.update(len(page_items))

        # iterate through all pages
        while len(items) < total:
            results = self.spotify.next(results)
            page_items = results.get(key, [])
            items.extend(page_items)
            if pbar:
                pbar.update(len(page_items))

        if pbar:
            pbar.close()
        return items

    def get_playlists(self) -> list[dict]:
        """Retrieve all user playlists plus liked songs."""
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
        """Export a single playlist to CSV file."""
        name, pid = playlist["name"], playlist["id"]
        output_dir.mkdir(parents=True, exist_ok=True)
        filepath = output_dir / sanitize_filename(name)

        # Format description for progress bar
        desc = (
            name[: DESC_LENGTH - 2] + "...: "
            if len(name) > DESC_LENGTH - 2
            else f"{name}: ".ljust(DESC_LENGTH + 3)
        )

        # Fetch tracks
        if pid == "liked_songs":
            items = self._fetch_all_items(
                self.spotify.current_user_saved_tracks,
                "items",
                desc=desc,
            )
        else:
            items = self._fetch_all_items(
                self.spotify.playlist_tracks,
                "items",
                pid,
                desc=desc,
            )

        # Batch fetch album details
        album_ids = list({i["track"]["album"]["id"] for i in items if i.get("track")})
        album_items = self._fetch_all_items(
            self.spotify.albums,
            "albums",
            album_ids,
            desc="Fetching album details: ",
        )
        albums = {a["id"]: a for a in album_items}

        # Build export data
        export_data = []
        for item in items:
            track = item.get("track") or {}
            album = albums.get(track.get("album", {}).get("id"), {})
            artists = [a["name"] for a in track.get("artists", [])]
            artist_uris = [a["uri"] for a in track.get("artists", [])]

            record = {
                "Track URI": track.get("uri"),
                "Artist URI(s)": artist_uris,
                "Album URI": album.get("uri"),
                "Track Name": track.get("name"),
                "Album Name": album.get("name"),
                "Artist Name(s)": artists,
                "Release Date": album.get("release_date"),
                "Duration_ms": track.get("duration_ms"),
                "Popularity": track.get("popularity"),
                "Added By": item.get("added_by", {}).get("id"),
                "Added At": item.get("added_at"),
                "Record Label": album.get("label"),
                "Track ISRC": track.get("external_ids", {}).get("isrc"),
                "Album UPC": album.get("external_ids", {}).get("upc"),
            }

            if not self.include_uris:
                record.pop("Artist URI(s)", None)
                record.pop("Album URI", None)
            if not self.include_ids:
                record.pop("Track ISRC", None)
                record.pop("Album UPC", None)

            export_data.append(record)

        write_csv(filepath, export_data)
        click.echo(
            f"Exported {len(export_data)} tracks from '{name}' to {filepath}",
        )


@click.command()
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
    help="Directory to save CSV files",
)
@click.option("--all", "-a", "export_all", is_flag=True, help="Export all playlists")
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
@click.option("--include-uris", is_flag=True, help="Include album and artist URIs")
@click.option("--include-ids", is_flag=True, help="Include track ISRC and album UPC")
@click.help_option("--help", "-h")
def main(
    config: str,
    output: str,
    playlist: list[str],
    *,
    export_all: bool = False,
    list_only: bool = False,
    include_uris: bool = False,
    include_ids: bool = False,
) -> None:
    """CLI for exporting Spotify playlists to CSV."""
    cfg_path = Path(config)
    cfg = load_config(cfg_path)
    client = init_spotify_client(cfg)

    exporter = SpotifyExporter(
        spotify_client=client,
        include_uris=include_uris,
        include_ids=include_ids,
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
                # 34 is the width of ID and Tracks columns + padding
                maxcolwidths=[terminal_width - 34, None, None],
            ),
        )
        sys.exit(0)

    # Determine targets
    targets = []
    if export_all:
        targets = playlists
    else:
        # Exact matches first
        for p in playlists:
            if p["name"] in playlist or p["id"] in playlist:
                targets.append(p)
        # For unmatched inputs, try unique prefix match
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
                    f"Ambiguous prefix '{term}': matches "
                    f"{', '.join(p['name'] for p in matches)}. Skipping.",
                )

    # Deduplicate
    targets = list({p["id"]: p for p in targets}.values())

    if not targets:
        click.echo("No matching playlists found.")
        sys.exit(1)

    out_dir = Path(output)
    for pl in targets:
        exporter.export_playlist(pl, out_dir)


if __name__ == "__main__":
    main()
