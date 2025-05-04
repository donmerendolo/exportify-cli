import configparser
import csv
import json
import logging
import os
import re
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
            f"Missing or empty keys in [spotify] section: {', '.join(missing)}",
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
    click.echo("""File "config.cfg" not found or invalid. Let's create it.

1. Go to Spotify Developer Dashboard (https://developer.spotify.com/dashboard).
2. Create a new app.
3. Set a name and description for your app.
4. Add a redirect URI (e.g. http://127.0.0.1:3000/callback).

Now after creating the app, press the Settings button on the upper right corner.
Copy the Client ID, Client Secret and Redirect URI and paste them below.""")

    spotify_cfg = {
        "client_id": click.prompt("Spotify Client ID", type=str),
        "client_secret": click.prompt(
            "Spotify Client Secret",
            hide_input=True,
            type=str,
        ),
        "redirect_uri": click.prompt(
            "Redirect URI",
            type=str,
            default="http://127.0.1:3000/callback",
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


def clean_playlist_input(playlist: list[str]) -> None:
    """Detect playlist URLs or URIs and convert them to IDs."""
    for i, p in enumerate(playlist):
        playlist[i] = re.sub(r"^.*playlists?/([a-zA-Z0-9]{22}).*$", r"\1", p)
        playlist[i] = playlist[i].replace("spotify:playlist:", "")


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
    """Write list of dicts to file."""
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
        """Initialize the exporter with a Spotify client."""
        self.spotify = spotify_client
        self.file_format = file_format
        self.include_uris = include_uris
        self.external_ids = external_ids
        self.with_bar_flag = with_bar_flag
        self.exported_playlists = 0
        self.exported_tracks = 0

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
                if show_bar and self.with_bar_flag
                else None
            )

            for batch in batches:
                results = fetch_func(batch, *args[1:], **kwargs)
                page_items = results.get(key, [])

                fetched_ids = [i.get("id") for i in page_items if i and i.get("id")]
                page_items = [i for i in page_items if i]

                unfetched_ids = list(set(batch) - set(fetched_ids))
                shows = []
                for id in unfetched_ids:
                    try:
                        if unfetched_ids:
                            shows.append(self.spotify.show(id))
                    except spotipy.SpotifyException as e:
                        logger.warning(f"Failed to fetch show for ID {id}: {e}")

                for i in shows:
                    i["label"] = i.get("publisher")
                page_items.extend(shows)

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
            if show_bar and self.with_bar_flag
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

        def _episode_to_track(item: dict) -> dict:
            """Convert episode to track if applicable."""
            if item.get("track"):
                track = item.get("track")
            else:
                return

            if track.get("type") == "episode":
                episode_id = track.get("id")
                episode = self.spotify.episode(episode_id)
                track["release_date"] = episode.get("release_date")

                for artist in track.get("artists", []):
                    artist["name"] = artist.get("type")

        for i in items:
            _episode_to_track(i)

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
        filepath = output_dir / sanitize_filename(name, self.file_format)

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

        # Found a track like this in a playlist (replacing [user_id]
        # with the actual user id). I could remove it from the list
        # since it has no info about the track, but one could hypothetically
        # do detective work out of 'added_at' to find the track (or thing)
        # it originally was (I guess), so I'll leave it there.
        #
        # No idea on how it was generated.
        #
        # {'added_at': '2022-06-30T21:08:13Z',
        #  'added_by': {'external_urls': {'spotify': 'https://open.spotify.com/user/[user_id]'},
        #               'href': 'https://api.spotify.com/v1/users/[user_id]',
        #               'id': '[user_id]',
        #               'type': 'user',
        #               'uri': 'spotify:user:[user_id]'},
        #  'is_local': False,
        #  'primary_color': None,
        #  'track': None,
        #  'video_thumbnail': {'url': None}}

        # Batch fetch album details
        album_ids = list(
            {
                i.get("track").get("album").get("id")
                for i in items
                if i.get("track")
                and i.get("track").get("album")
                and i.get("track").get("album").get("id")
            },
        )
        album_items = self._fetch_all_items(
            self.spotify.albums,
            "albums",
            album_ids,
            desc="Fetching album details: ",
        )
        albums = {a.get("id"): a for a in album_items if a}

        # Build export data
        export_data = []
        for i in items:
            track = i.get("track") or {}
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
                "Release Date": album.get("release_date")
                or (track.get("release_date")),
                "Duration_ms": track.get("duration_ms"),
                "Popularity": track.get("popularity"),
                "Added By": i.get("added_by", {}).get("id"),
                "Added At": i.get("added_at"),
                "Record Label": album.get("label"),
                "Track ISRC": track.get("external_ids", {}).get("isrc"),
                "Album UPC": album.get("external_ids", {}).get("upc"),
            }

            if not self.include_uris:
                record.pop("Artist URI(s)", None)
                record.pop("Album URI", None)
            if not self.external_ids:
                record.pop("Track ISRC", None)
                record.pop("Album UPC", None)

            export_data.append(record)

        write_file(filepath, export_data, self.file_format)
        self.exported_playlists += 1
        self.exported_tracks += len(export_data)
        click.echo(
            f"Exported {len(export_data)} tracks from '{name}' to {filepath}",
        )


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
    "playlist",
    multiple=True,
    help="Names, URLs or IDs of playlists to export",
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
    help="Show or hide progress bar (overrides config)",
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

    playlist = list(playlist)
    clean_playlist_input(playlist)

    fetched_playlists = exporter.get_playlists()

    if list_only:
        playlist_data = [
            [p["name"], p["id"], p["tracks"]["total"]] for p in fetched_playlists
        ]
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
        targets = fetched_playlists
    else:
        # Exact matches first
        for p in fetched_playlists:
            if p["name"] in playlist or p["id"] in playlist:
                targets.append(p)
        # For unmatched inputs, try unique prefix match
        for term in playlist:
            if any(p for p in targets if p["name"] == term or p["id"] == term):
                continue

            matches = [
                p
                for p in fetched_playlists
                if p["name"].lower().startswith(term.lower())
            ]
            if len(matches) == 1:
                targets.append(matches[0])
            elif len(matches) > 1:
                click.echo(
                    f"Ambiguous prefix '{term}': matches "
                    f"{', '.join(p['name'] for p in matches)}. Skipping.",
                )

            # User may be trying to export a playlist they have not saved
            elif term.isalnum() and len(term) == 22:
                try:
                    pl = client.playlist(term)
                    if pl:
                        targets.append(pl)
                except spotipy.SpotifyException as e:
                    logger.warning(f"Failed to fetch playlist {term}: {e}")

    # Deduplicate
    targets = list({p["id"]: p for p in targets}.values())

    if not targets:
        click.echo("No matching playlists found.")
        sys.exit(1)

    out_dir = Path(output)
    for pl in targets:
        exporter.export_playlist(pl, out_dir)

    if exporter.exported_playlists > 1:
        click.echo(
            f"Successfully exported {exporter.exported_tracks} tracks "
            f"from {exporter.exported_playlists} playlists.",
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
