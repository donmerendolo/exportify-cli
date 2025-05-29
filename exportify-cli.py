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
from click_option_group import OptionGroup, optgroup
from spotipy.oauth2 import SpotifyOAuth
from tabulate import tabulate
from tqdm.auto import tqdm

# Default options for the CLI (used in [exportify-cli] section)
CLI_DEFAULTS = {
    "format": "csv",
    "output": "./playlists",
    "uris": "false",
    "external_ids": "false",
    "no_bar": "false",
    "sort_key": "spotify_default",
    "reverse": "false",
}

# Default bar format for progress bars
DEFAULT_BAR_FORMAT = (
    "{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}"
    "/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]"
)

# Max length for playlist name in progress bar
DESC_LENGTH = 21

# ex: "37i9dQZF1DXcBWIGoYBM5M"
PLAYLIST_ID_LENGTH = 22

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
    config = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
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
        "client_id": click.prompt("Spotify Client ID", type=str).strip(),
        "client_secret": click.prompt(
            "Spotify Client Secret",
            hide_input=True,
            type=str,
        ).strip(),
        "redirect_uri": click.prompt(
            "Redirect URI",
            type=str,
            default="http://127.0.1:3000/callback",
        ).strip(),
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
        cache_path=Path(__file__).parent / Path(".cache"),
    )
    return spotipy.Spotify(auth_manager=auth, retries=10)


def sanitize_filename(name: str) -> str:
    """Convert a playlist name into a safe filename."""
    safe = "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in name)
    return f"{safe.strip().replace(' ', '_').lower()}"


def write_file(file_path: Path, data: list[dict], file_formats) -> None:
    """Write list of dicts to file."""
    if not data:
        logger.warning("No data to write; skipping file.")
        return

    if "csv" in file_formats:
        headers = list(data[0].keys())
        with file_path.with_suffix(".csv").open(
            "w", newline="", encoding="utf-8"
        ) as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            for row in data:
                writer.writerow(row)
        logger.info(f"Exported to {file_path}.csv")

    if "json" in file_formats:
        with file_path.with_suffix(".json").open("w", encoding="utf-8") as jsonfile:
            json.dump(data, jsonfile, ensure_ascii=False, indent=4)
        logger.info(f"Exported to {file_path}.json")


class SpotifyExporter:
    """Class to handle exporting Spotify playlists."""

    def __init__(
        self,
        spotify_client: spotipy.Spotify,
        file_formats: list[str],
        include_uris: bool,
        external_ids: bool,
        with_bar: bool,
        sort_key: str | None,
        reverse_order: bool,
    ) -> None:
        """Initialize the exporter with a Spotify client."""
        self.spotify = spotify_client
        self.file_formats = file_formats
        self.include_uris = include_uris
        self.external_ids = external_ids
        self.with_bar = with_bar
        self.sort_key = sort_key
        self.reverse_order = reverse_order
        self.exported_playlists = 0
        self.exported_tracks = 0
        self.album_cache: dict[str, dict] = {}

    def _fetch_all_items(
        self,
        fetch_func,
        key: str | None = None,
        *args: Any,
        desc: str | None = None,
        bar_format: str = DEFAULT_BAR_FORMAT,
        show_bar: bool = True,
        initial: int = 0,
        total_override: int | None = None,
        **kwargs: Any,
    ) -> list[dict]:
        """
        Fetch all paginated or batched items from a Spotify endpoint.

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
            total = total_override if total_override is not None else len(id_list)
            desc_text = desc or fetch_func.__name__
            # build 20-item batches
            batches = [
                id_list[i : i + 20] for i in range(0, total, 20) if id_list[i : i + 20]
            ]

            pbar = (
                tqdm(total=total, desc=desc_text, unit="album", bar_format=bar_format)
                if show_bar and self.with_bar
                else None
            )
            # Advance the bar to reflect items already in cache
            if pbar and initial:
                pbar.update(initial)

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
            if show_bar and self.with_bar
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
        album_ids = {
            i.get("track").get("album").get("id")
            for i in items
            if i.get("track")
            and i.get("track").get("album")
            and i.get("track").get("album").get("id")
        }

        # Figure out which albums we haven't fetched yet
        ids_to_fetch = [aid for aid in album_ids if aid not in self.album_cache]
        already_cached = len(album_ids) - len(ids_to_fetch)

        # Fetch only the missing albums
        if ids_to_fetch:
            new_albums = self._fetch_all_items(
                self.spotify.albums,
                "albums",
                ids_to_fetch,
                desc="Fetching album details: ",
                initial=already_cached,
                total_override=len(album_ids),
            )
            for alb in new_albums:
                if alb and alb.get("id"):
                    self.album_cache[alb["id"]] = alb

        # Build a lookup from cache
        albums = {aid: self.album_cache[aid] for aid in album_ids}

        # Build export data
        export_data = []
        for i, item in enumerate(items, start=1):
            track = item.get("track") or {}
            album = albums.get(track.get("album", {}).get("id"), {})
            artists = [a.get("name") for a in track.get("artists", []) if a.get("name")]
            artist_uris = [
                a.get("uri") for a in track.get("artists", []) if a.get("uri")
            ]

            record = {
                "Position": i,
                "Track URI": track.get("uri"),
                "Artist URI(s)": artist_uris,
                "Album URI": album.get("uri"),
                "Track Name": track.get("name"),
                "Album Name": album.get("name"),
                "Artist Name(s)": artists,
                "Release Date": album.get("release_date") or track.get("release_date"),
                "Duration_ms": track.get("duration_ms"),
                "Popularity": track.get("popularity"),
                "Added By": item.get("added_by", {}).get("id"),
                "Added At": item.get("added_at"),
                "Record Label": album.get("label"),
                "Track ISRC": track.get("external_ids", {}).get("isrc"),
                "Album UPC": album.get("external_ids", {}).get("upc"),
            }

            export_data.append(record)

        if self.sort_key != "spotify_default":
            export_data.sort(key=lambda x: str(x[self.sort_key]) or "")

        if self.reverse_order:
            export_data.reverse()

        if not self.include_uris:
            for record in export_data:
                record.pop("Artist URI(s)", None)
                record.pop("Album URI", None)
        if not self.external_ids:
            for record in export_data:
                record.pop("Track ISRC", None)
                record.pop("Album UPC", None)

        write_file(filepath, export_data, self.file_formats)
        self.exported_playlists += 1
        self.exported_tracks += len(export_data)
        for ext in self.file_formats:
            click.echo(
                f"Exported {len(export_data)} tracks from '{name}' to {filepath}.{ext}",
            )


# Custom command class to override usage line
class CustomCommand(click.Command):
    def format_usage(self, ctx, formatter) -> None:
        # Override the usage display
        formatter.write_text(
            "Usage: exportify-cli.py (-a | -p NAME|ID|URL|URI [-p ...] | -l) [OPTIONS]\n",
        )


@click.command(cls=CustomCommand)
@optgroup.group(cls=OptionGroup)
@optgroup.option("-a", "--all", "export_all", is_flag=True, help="Export all playlists")
@optgroup.option(
    "-p",
    "--playlist",
    "playlist",
    multiple=True,
    metavar="NAME|ID|URL|URI",
    help="Spotify playlist name, ID, URL, or URI; repeatable.",
)
@optgroup.option(
    "-l",
    "--list",
    "list_only",
    is_flag=True,
    help="List available playlists.",
)
@optgroup.option(
    "-c",
    "--config",
    "config",
    default=None,
    type=click.Path(),
    help="Path to configuration file (default is ./config.cfg next to this script).",
)
@optgroup.option(
    "-o",
    "--output",
    "output_param",
    default="./playlists",
    type=click.Path(),
    help="Directory to save exported files (default is ./playlists).",
)
@optgroup.option(
    "-f",
    "--format",
    "format_param",
    multiple=True,
    default=None,
    type=click.Choice(["csv", "json"]),
    help="Output file format (defaults to 'csv'); repeatable.",
)
@optgroup.option(
    "--uris",
    "uris_flag",
    default=None,
    is_flag=True,
    help="Include album and artist URIs.",
)
@optgroup.option(
    "--external-ids",
    "external_ids_flag",
    default=None,
    is_flag=True,
    help="Include track ISRC and album UPC.",
)
@optgroup.option(
    "--no-bar",
    "no_bar_flag",
    default=None,
    is_flag=True,
    help="Hide progress bar.",
)
@optgroup.option(
    "--sort-key",
    "sort_key",
    default=None,
    help="Key to sort tracks by (default is 'spotify_default').",
)
@optgroup.option(
    "--reverse",
    "reverse_order",
    default=None,
    is_flag=True,
    help="Reverse the sort order.",
)
@click.help_option("-h", "--help")
@click.version_option(
    "0.3",
    "-v",
    "--version",
    prog_name="exportify-cli",
    message="%(prog)s v%(version)s",
)
def main(
    export_all: bool,
    playlist: tuple[str, ...],
    list_only: bool,
    config: None | str,
    output_param: str,
    format_param: str,
    uris_flag: bool,
    external_ids_flag: bool,
    no_bar_flag: bool,
    sort_key: str | None,
    reverse_order: bool,
) -> None:
    """Export Spotify playlists to CSV or JSON."""
    # If no --all, --playlist or --list options are given, show help
    if not export_all and not playlist and not list_only:
        click.echo(main.get_help(ctx=click.get_current_context()))
        sys.exit(1)

    if config:
        cfg_path = Path(config).expanduser()
        if cfg_path.is_dir():
            cfg_path = cfg_path / Path("config.cfg")
    else:
        cfg_path = Path(__file__).parent / Path("config.cfg")
    cfg = load_config(cfg_path)

    # Resolve config vs CLI
    file_formats = format_param if format_param else None
    if file_formats is None:
        file_formats = []
        if "csv" in cfg.get("exportify-cli", "format"):
            file_formats.append("csv")
        if "json" in cfg.get("exportify-cli", "format"):
            file_formats.append("json")
    file_formats = list(set(file_formats))

    output = output_param if output_param else cfg.get("exportify-cli", "output")
    include_uris = (
        uris_flag if uris_flag is not None else cfg.getboolean("exportify-cli", "uris")
    )
    external_ids = (
        external_ids_flag
        if external_ids_flag is not None
        else cfg.getboolean("exportify-cli", "external_ids")
    )
    with_bar = not (
        no_bar_flag
        if no_bar_flag is not None
        else cfg.getboolean("exportify-cli", "no_bar")
    )
    sort_key = (
        sort_key if sort_key is not None else cfg.get("exportify-cli", "sort_key")
    )
    keys = [
        "Position",
        "Track URI",
        "Artist URI(s)",
        "Album URI",
        "Track Name",
        "Album Name",
        "Artist Name(s)",
        "Release Date",
        "Duration_ms",
        "Popularity",
        "Added By",
        "Added At",
        "Record Label",
        "Track ISRC",
        "Album UPC",
        "spotify_default",
    ]
    # Find the actual key that matches case-insensitively
    actual_key = None
    found = False
    for key in keys:
        if key.lower().replace(" ", "").replace("_", "").replace("(", "").replace(
            ")", ""
        ) == sort_key.lower().replace(" ", "").replace("_", "").replace(
            "(", ""
        ).replace(")", ""):
            actual_key = key
            found = True
            break

    if not found:
        logger.warning(
            f"Sort key '{sort_key}' not found in keys. Available keys: {keys}."
        )
        sys.exit(1)

    reverse_order = (
        reverse_order
        if reverse_order is not None
        else cfg.getboolean("exportify-cli", "reverse", fallback=False)
    )

    client = init_spotify_client(cfg)

    exporter = SpotifyExporter(
        spotify_client=client,
        file_formats=file_formats,
        include_uris=include_uris,
        external_ids=external_ids,
        with_bar=with_bar,
        sort_key=actual_key,
        reverse_order=reverse_order,
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
            elif term.isalnum() and len(term) == PLAYLIST_ID_LENGTH:
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
    click.echo()
    sys.exit(0)


if __name__ == "__main__":
    main()
