"""Command line interface."""
# In case a human reads this: AI (Fable) rewrote the program, then I read
# and understood everything and made my own handwritten changes.

import logging
import re
import shutil
import sys
from pathlib import Path

import click
import spotipy
from click_option_group import OptionGroup, optgroup
from pathvalidate import sanitize_filename
from spotipy.oauth2 import SpotifyOauthError, SpotifyPKCE
from tabulate import tabulate

from . import __version__
from .config import (
    default_config_path,
    load_config,
    opt,
    token_cache_path,
)
from .export import ALL_HEADERS, FIELD_ALIASES, SpotifyExporter, parse_fields

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

# ex: "37i9dQZF1DXcBWIGoYBM5M"
PLAYLIST_ID_LENGTH = 22

VALID_FORMATS = {"csv", "json"}


def clean_playlist_input(playlists: list[str]) -> list[str]:
    """Convert playlist URLs or URIs to bare IDs."""
    cleaned = []
    for p in playlists:
        p = re.sub(r"^.*playlists?/([a-zA-Z0-9]{22}).*$", r"\1", p)
        cleaned.append(p.replace("spotify:playlist:", ""))
    return cleaned


def clean_user_input(users: list[str]) -> list[str]:
    """Convert user URLs or URIs to bare IDs."""
    cleaned = []
    for u in users:
        u = re.sub(r"^.*users?/([a-zA-Z0-9]+).*$", r"\1", u)
        cleaned.append(u.replace("spotify:user:", ""))
    return cleaned


def resolve_formats(format_param: tuple[str, ...], cfg: dict) -> list[str]:
    """Resolve output formats, --format > config > default."""
    if format_param:
        return list(dict.fromkeys(format_param))
    formats = [f.lower() for f in opt(cfg, "format")]
    invalid = [f for f in formats if f not in VALID_FORMATS]
    if invalid or not formats:
        click.echo(
            f"Error: invalid format(s) in config: {', '.join(invalid) or '(empty)'}. "
            f"Valid formats: {', '.join(sorted(VALID_FORMATS))}.",
            err=True,
        )
        sys.exit(1)
    return list(dict.fromkeys(formats))


def resolve_sort_key(sort_key: str) -> str:
    """Match sort parameter with key or alias."""

    def normalize(s: str) -> str:
        return re.sub(r"[\s_()]", "", s.lower())

    for key in ALL_HEADERS:
        if normalize(key) == normalize(sort_key):
            return key
    for key in FIELD_ALIASES:
        if normalize(key) == normalize(sort_key):
            return FIELD_ALIASES[key]
    click.echo(
        f"Error: sort key '{sort_key}' not found. Available keys: {ALL_HEADERS}.",
        err=True,
    )
    sys.exit(1)


def init_spotify_client(cfg: dict, cache_path: Path) -> spotipy.Spotify:
    """Initialize Spotify client with PKCE manager."""
    creds = cfg["spotify"]
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    auth = SpotifyPKCE(
        client_id=creds["client_id"],
        redirect_uri=creds["redirect_uri"],
        cache_path=str(cache_path),
        scope=["user-library-read", "playlist-read-private"],
    )
    try:
        auth.get_access_token(check_cache=True)
    except SpotifyOauthError as err:
        logger.error(f"Spotify authentication failed: {err}")
        sys.exit(1)
    return spotipy.Spotify(auth_manager=auth, retries=10)


def list_playlists(playlists: list[dict]) -> None:
    data = [[p["name"], p["id"], p["tracks"]["total"]] for p in playlists]
    # shutil (unlike os.get_terminal_size) has a fallback when not a tty
    terminal_width = shutil.get_terminal_size().columns
    click.echo(
        tabulate(
            data,
            headers=["Name", "ID", "Tracks"],
            tablefmt="simple",
            # 34 is the width of ID and Tracks columns + padding
            maxcolwidths=[terminal_width - 34, None, None],
        )
    )


def match_targets(
    client: spotipy.Spotify, fetched_playlists: list[dict], terms: list[str]
) -> tuple[list[dict], list[str]]:
    """Match requested names/IDs against the user's playlists.

    Exact name/ID match first, then unique prefix match, then a direct
    fetch for anything that looks like a playlist ID. Returns the matched
    playlists and the terms that couldn't be resolved.
    """
    targets: list[dict] = []
    unmatched: list[str] = []
    for term in terms:
        exact = [p for p in fetched_playlists if term in (p["name"], p["id"])]
        if exact:
            targets.extend(exact)
            continue

        matches = [
            p for p in fetched_playlists if p["name"].lower().startswith(term.lower())
        ]

        if len(matches) == 1:
            targets.append(matches[0])

        elif len(matches) > 1:
            # Considered adding an extra case sensitive match here but decided against it
            click.echo(
                f"Ambiguous prefix '{term}': matches "
                f"{', '.join(p['name'] for p in matches)}. Skipping.",
            )
            unmatched.append(term)

        elif term.isalnum() and len(term) == PLAYLIST_ID_LENGTH:
            # May be a playlist the user hasn't saved
            try:
                pl = client.playlist(term)
                if pl:
                    targets.append(pl)
                    continue
            except spotipy.SpotifyException as e:
                logger.warning(f"Failed to fetch playlist {term}: {e}")
            unmatched.append(term)

        else:
            unmatched.append(term)

    # Deduplicate, preserving order
    targets = list({p["id"]: p for p in targets}.values())
    return targets, unmatched


def fetch_user_targets(
    client: spotipy.Spotify, exporter: SpotifyExporter, user_ids: list[str]
) -> tuple[list[dict], list[str]]:
    """Fetch public playlists for each user ID; returns (targets, failed_ids)."""
    users_targets: list[dict] = []
    failed: list[str] = []
    for uid in user_ids:
        try:
            items = exporter.fetch_paginated(
                client.user_playlists,
                "items",
                uid,
                desc=f"Playlists of {uid}",
                show_bar=False,
            )
            user_data = client.user(uid)
            users_targets.append(
                {
                    "name": user_data.get("display_name") or uid,
                    "uid": uid,
                    "targets": [t for t in items if t.get("uri")],
                }
            )
        except spotipy.SpotifyException as e:
            logger.warning(f"Failed to fetch playlists for user {uid}: {e}")
            failed.append(uid)
    return users_targets, failed


class CustomCommand(click.Command):
    def format_usage(self, ctx, formatter) -> None:
        formatter.write_text(
            "Usage: exportify-cli (-a | -p NAME|ID|URL|URI [-p ...] "
            "| -u ID|URL|URI | -l | --logout) [OPTIONS]\n",
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
    help="Export a Spotify playlist given name, ID, URL, or URI; repeatable.",
)
@optgroup.option(
    "-u",
    "--user",
    "user",
    multiple=True,
    metavar="ID|URL|URI",
    help="Export all public playlists of a Spotify user given ID, URL, or URI; repeatable.",
)
@optgroup.option(
    "-l", "--list", "list_only", is_flag=True, help="List available playlists."
)
@optgroup.option(
    "--logout",
    "logout",
    is_flag=True,
    help="Delete cached Spotify auth token and exit.",
)
@optgroup.option(
    "-c",
    "--config",
    "config",
    default=None,
    type=click.Path(),
    help="Path to configuration file (default: ./config.toml or "
    "$XDG_CONFIG_HOME/exportify-cli/config.toml).",
)
@optgroup.option(
    "-o",
    "--output",
    "output_param",
    default=None,
    type=click.Path(),
    help="Directory to save exported files (default is ./playlists); '-' writes to stdout.",
)
@optgroup.option(
    "-f",
    "--format",
    "format_param",
    multiple=True,
    type=click.Choice(sorted(VALID_FORMATS)),
    help="Output file format (defaults to 'csv'); repeatable.",
)
@optgroup.option(
    "--uris",
    "uris_flag",
    default=None,
    is_flag=True,
    help="(Deprecated: add the URI fields to --fields or the config instead.)",
)
@optgroup.option(
    "--external-ids",
    "external_ids_flag",
    default=None,
    is_flag=True,
    help="(Deprecated: add 'Track ISRC'/'Album UPC' to --fields or the config instead.)",
)
@optgroup.option(
    "--no-bar", "no_bar_flag", default=None, is_flag=True, help="Hide progress bar."
)
@optgroup.option(
    "-s",
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
@optgroup.option(
    "--fields",
    "fields_param",
    default=None,
    help="Comma-separated list of fields to include.",
)
@click.help_option("-h", "--help")
@click.version_option(
    __version__,
    "-v",
    "--version",
    prog_name="exportify-cli",
    message="%(prog)s v%(version)s",
)
def main(
    export_all: bool,
    playlist: tuple[str, ...],
    user: tuple[str, ...],
    list_only: bool,
    logout: bool,
    config: str | None,
    output_param: str | None,
    format_param: tuple[str, ...],
    uris_flag: bool | None,
    external_ids_flag: bool | None,
    fields_param: str | None,
    no_bar_flag: bool | None,
    sort_key: str | None,
    reverse_order: bool | None,
) -> None:
    """Export Spotify playlists to CSV or JSON."""

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore

    # --- Config ---
    if config:
        cfg_path = Path(config).expanduser()
        if cfg_path.is_dir():
            cfg_path = cfg_path / "config.toml"
    else:
        cfg_path = default_config_path()
    cfg = load_config(cfg_path, explicit=config is not None)
    cache_path = token_cache_path(cfg_path)

    if logout:
        if cache_path.exists():
            cache_path.unlink()
            click.echo(f"Logged out successfully. Removed token cache '{cache_path}'.")
        else:
            click.echo(f"No token cache file '{cache_path}' was found.")
        sys.exit(0)

    # --- Resolve options: CLI beats config, config beats defaults ---
    file_formats = resolve_formats(format_param, cfg)
    output = output_param if output_param is not None else opt(cfg, "output")
    include_uris = bool(uris_flag)
    external_ids = bool(external_ids_flag)
    if include_uris or external_ids:
        logger.warning(
            "--uris and --external-ids are deprecated; use --fields or the config's 'fields' instead."
        )
    with_bar = not (no_bar_flag if no_bar_flag is not None else opt(cfg, "no_bar"))
    actual_key = resolve_sort_key(
        sort_key if sort_key is not None else opt(cfg, "sort_key")
    )
    reverse = reverse_order if reverse_order is not None else opt(cfg, "reverse")
    fields = parse_fields(
        fields_param.split(",") if fields_param is not None else opt(cfg, "fields"),
        include_uris,
        external_ids,
    )
    if actual_key not in fields:
        logger.warning(
            f"Sort key '{actual_key}' is not in the exported fields; sort is ignored."
        )

    playlists = list(playlist) or opt(cfg, "playlists")
    users = list(user) or opt(cfg, "users")

    if not (export_all or playlists or users or list_only):
        click.echo(main.get_help(ctx=click.get_current_context()))
        sys.exit(1)

    # --- Spotify ---
    client = init_spotify_client(cfg, cache_path)
    exporter = SpotifyExporter(
        spotify_client=client,
        file_formats=file_formats,
        fields=fields,
        with_bar=with_bar,
        sort_key=actual_key,
        reverse_order=reverse,
    )
    fetched_playlists = exporter.get_playlists()

    if list_only:
        list_playlists(fetched_playlists)
        sys.exit(0)

    # --- Determine targets ---
    if export_all:
        targets, unmatched = fetched_playlists, []
    else:
        targets, unmatched = match_targets(
            client, fetched_playlists, clean_playlist_input(playlists)
        )
    users_targets, failed_users = fetch_user_targets(
        client, exporter, clean_user_input(users)
    )

    if not (targets or users_targets):
        click.echo("No matching playlists found.")
        sys.exit(1)

    # --- Export ---
    to_stdout = output == "-"
    if to_stdout and len(file_formats) > 1:
        click.echo("Error: -o - requires a single format (use -f).", err=True)
        sys.exit(1)
    out_dir = None if to_stdout else Path(output)
    for pl in targets:
        exporter.export_playlist(pl, out_dir)
    for ut in users_targets:
        user_out_dir = (
            out_dir / sanitize_filename(f"{ut['name']} [{ut['uid']}]")
            if out_dir is not None
            else None
        )
        for pl in ut["targets"]:
            exporter.export_playlist(pl, user_out_dir)

    if exporter.exported_playlists > 1:
        click.echo(
            f"Successfully exported {exporter.exported_tracks} tracks "
            f"from {exporter.exported_playlists} playlists.",
            err=to_stdout,
        )
    if unmatched or failed_users:
        skipped = [*unmatched, *failed_users]
        click.echo(f"Skipped (not found or failed): {', '.join(skipped)}", err=True)
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
