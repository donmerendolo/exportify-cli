"""Fetching from Spotify and writing playlist exports."""

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable

import click
import spotipy
from pathvalidate import sanitize_filename
from tqdm.auto import tqdm

logger = logging.getLogger(__name__)

DEFAULT_BAR_FORMAT = (
    "{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}"
    "/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]"
)

# Max length for playlist name in progress bar
DESC_LENGTH = 21

# All exportable fields, in output order
ALL_HEADERS = [
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
]

FIELD_ALIASES = {
    "position": "Position",
    "default": "Position",
    # In previous versions of exportify-cli, spotify_default was
    # separate code-wise from Position, but it's the same thing
    "spotify_default": "Position",
    "track_uri": "Track URI",
    "uri": "Track URI",
    "artist_uris": "Artist URI(s)",
    "artist_uri": "Artist URI(s)",
    "album_uri": "Album URI",
    "name": "Track Name",
    "album": "Album Name",
    "artist": "Artist Name(s)",
    "artists": "Artist Name(s)",
    "artists name": "Artist Name(s)",
    "date": "Release Date",
    "duration": "Duration_ms",
    "popularity": "Popularity",
    "added_by": "Added By",
    "added_at": "Added At",
    "label": "Record Label",
    "isrc": "Track ISRC",
    "upc": "Album UPC",
}

URI_HEADERS = ["Track URI", "Artist URI(s)", "Album URI"]
EXTERNAL_ID_HEADERS = ["Track ISRC", "Album UPC"]


def parse_fields(
    fields_value: list[str], include_uris: bool, external_ids: bool
) -> list[str]:
    """Resolve export headers from a list of field names/aliases.

    --uris / --external-ids add their headers on top; when no fields are
    given, the default is every header minus the URI/external-id ones.
    """
    headers: list[str] = []
    for field in (f.strip() for f in fields_value if f.strip()):
        if field in ALL_HEADERS:
            headers.append(field)
        elif field.lower() in FIELD_ALIASES:
            headers.append(FIELD_ALIASES[field.lower()])
        else:
            logger.warning(f"Unknown field: {field}")

    if not headers:
        excluded = URI_HEADERS + EXTERNAL_ID_HEADERS
        headers = [h for h in ALL_HEADERS if h not in excluded]

    if include_uris:
        headers += [h for h in URI_HEADERS if h not in headers]
    if external_ids:
        headers += [h for h in EXTERNAL_ID_HEADERS if h not in headers]

    return list(dict.fromkeys(headers))


def sort_value(value: Any) -> tuple:
    """Sort key that handles None and compares numbers numerically."""
    if value is None:
        return (0, 0.0, "")
    try:
        return (1, float(value), "")
    except (TypeError, ValueError):
        return (2, 0.0, str(value).lower())


def _write_csv(stream, headers: list[str], data: list[dict]) -> None:
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    for row in data:
        writer.writerow(
            {k: ", ".join(v) if isinstance(v, list) else v for k, v in row.items()}
        )


def write_stdout(headers: list[str], data: list[dict], file_format: str) -> None:
    """Write a single export to stdout."""
    if file_format == "csv":
        _write_csv(sys.stdout, headers, data)
    else:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=4)
        sys.stdout.write("\n")


def write_file(
    file_path: Path, headers: list[str], data: list[dict], file_formats: list[str]
) -> None:
    """Write list of dicts to CSV and/or JSON files."""
    if "csv" in file_formats:
        csv_path = file_path.with_name(file_path.name + ".csv")
        with csv_path.open("w", newline="", encoding="utf-8") as csvfile:
            _write_csv(csvfile, headers, data)
        logger.info(f"Exported to {csv_path}")

    if "json" in file_formats:
        json_path = file_path.with_name(file_path.name + ".json")
        with json_path.open("w", encoding="utf-8") as jsonfile:
            json.dump(data, jsonfile, ensure_ascii=False, indent=4)
        logger.info(f"Exported to {json_path}")


class SpotifyExporter:
    """Exports Spotify playlists to files."""

    def __init__(
        self,
        spotify_client: spotipy.Spotify,
        file_formats: list[str],
        fields: list[str],
        with_bar: bool,
        sort_key: str,
        reverse_order: bool,
    ) -> None:
        self.spotify = spotify_client
        self.file_formats = file_formats
        self.fields = fields
        self.with_bar = with_bar
        self.sort_key = sort_key
        self.reverse_order = reverse_order
        self.exported_playlists = 0
        self.exported_tracks = 0
        self.album_cache: dict[str, dict] = {}

    def _progress_bar(
        self, total: int | None, desc: str, unit: str, show_bar: bool
    ) -> tqdm | None:
        if not (show_bar and self.with_bar):
            return None
        return tqdm(total=total, desc=desc, unit=unit, bar_format=DEFAULT_BAR_FORMAT)

    def fetch_paginated(
        self,
        fetch_func: Callable,
        key: str,
        *args: Any,
        desc: str | None = None,
        show_bar: bool = True,
        **kwargs: Any,
    ) -> list[dict]:
        """Fetch all pages from a paginated endpoint (playlist_tracks, etc.)."""
        results = fetch_func(*args, **kwargs)
        items: list[dict] = list(results.get(key, []))

        pbar = self._progress_bar(
            results.get("total"),
            desc or results.get("name") or fetch_func.__name__,  # type: ignore
            "track",
            show_bar,
        )
        if pbar:
            pbar.update(len(items))

        while results.get("next"):
            results = self.spotify.next(results)
            page_items = results.get(key, [])
            items.extend(page_items)
            if pbar:
                pbar.update(len(page_items))

        if pbar:
            pbar.close()
        return items

    def fetch_albums(self, album_ids: list[str], already_cached: int = 0) -> list[dict]:
        """Fetch album details in batches of 20; IDs that aren't albums are
        retried as shows (podcasts saved in playlists)."""
        items: list[dict] = []
        total = len(album_ids) + already_cached
        pbar = self._progress_bar(total, "Fetching album details: ", "album", True)
        if pbar and already_cached:
            pbar.update(already_cached)

        for start in range(0, len(album_ids), 20):
            batch = album_ids[start : start + 20]
            page_items = [a for a in self.spotify.albums(batch).get("albums", []) if a]

            fetched_ids = {a["id"] for a in page_items if a.get("id")}
            for show_id in set(batch) - fetched_ids:
                try:
                    show = self.spotify.show(show_id)
                    show["label"] = show.get("publisher")
                    page_items.append(show)
                except spotipy.SpotifyException as e:
                    logger.warning(f"Failed to fetch show for ID {show_id}: {e}")

            items.extend(page_items)
            if pbar:
                pbar.update(len(page_items))

        if pbar:
            pbar.close()
        return items

    def _fix_episode(self, item: dict) -> None:
        """Fill in release date and artist names for podcast episodes."""
        track = item.get("track")
        if not track or track.get("type") != "episode":
            return
        episode = self.spotify.episode(track.get("id"))
        track["release_date"] = episode.get("release_date")
        for artist in track.get("artists", []):
            artist["name"] = artist.get("type")

    def get_playlists(self) -> list[dict]:
        """Retrieve all user playlists plus liked songs."""
        items = self.fetch_paginated(
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

    def _fetch_tracks(self, playlist: dict) -> list[dict]:
        name, pid = playlist["name"], playlist["id"]
        desc = (
            name[: DESC_LENGTH - 2] + "...: "
            if len(name) > DESC_LENGTH - 2
            else f"{name}: ".ljust(DESC_LENGTH + 3)
        )
        if pid == "liked_songs":
            items = self.fetch_paginated(
                self.spotify.current_user_saved_tracks, "items", desc=desc
            )
        else:
            items = self.fetch_paginated(
                self.spotify.playlist_tracks, "items", pid, desc=desc
            )
        for item in items:
            self._fix_episode(item)
        return items

    def _album_lookup(self, items: list[dict]) -> dict[str, dict]:
        """Batch-fetch album details for all tracks, using the cache."""
        album_ids = {
            i["track"]["album"]["id"]
            for i in items
            if i.get("track")
            and i["track"].get("album")
            and i["track"]["album"].get("id")
        }
        ids_to_fetch = [aid for aid in album_ids if aid not in self.album_cache]
        if ids_to_fetch:
            for alb in self.fetch_albums(
                ids_to_fetch, already_cached=len(album_ids) - len(ids_to_fetch)
            ):
                if alb.get("id"):
                    self.album_cache[alb["id"]] = {
                        "id": alb.get("id"),
                        "uri": alb.get("uri"),
                        "name": alb.get("name"),
                        "release_date": alb.get("release_date"),
                        "label": alb.get("label"),
                        "external_ids": alb.get("external_ids", {}),
                    }
        return {
            aid: self.album_cache[aid] for aid in album_ids if aid in self.album_cache
        }

    def _build_record(self, position: int, item: dict, albums: dict) -> dict:
        # Playlists can contain items with 'track': None (seen in the wild;
        # no idea how it got generated). We keep them as mostly-empty rows
        # instead of dropping them: they carry no track info, but one could
        # hypothetically do detective work out of 'added_at'/'added_by' to
        # find what they originally were.
        track = item.get("track") or {}
        album = albums.get(track.get("album", {}).get("id"), {})
        artists = [a.get("name") for a in track.get("artists", []) if a.get("name")]
        artist_uris = [a.get("uri") for a in track.get("artists", []) if a.get("uri")]
        record = dict(
            zip(
                ALL_HEADERS,
                [
                    position,
                    track.get("uri"),
                    artist_uris,
                    album.get("uri"),
                    track.get("name"),
                    album.get("name"),
                    artists,
                    album.get("release_date") or track.get("release_date"),
                    track.get("duration_ms"),
                    track.get("popularity"),
                    item.get("added_by", {}).get("id"),
                    item.get("added_at"),
                    album.get("label"),
                    track.get("external_ids", {}).get("isrc"),
                    album.get("external_ids", {}).get("upc"),
                ],
            )
        )
        return {k: v for k, v in record.items() if k in self.fields}

    def export_playlist(self, playlist: dict, output_dir: Path | None) -> None:
        """Export a single playlist to the configured formats.

        A None output_dir writes to stdout (status messages go to stderr).
        """
        name, pid = playlist["name"], playlist["id"]
        to_stdout = output_dir is None
        if not to_stdout:
            output_dir.mkdir(parents=True, exist_ok=True)
            filepath = output_dir / sanitize_filename(f"{name} [{pid}]")

        items = self._fetch_tracks(playlist)
        albums = self._album_lookup(items)
        export_data = [
            self._build_record(i, item, albums) for i, item in enumerate(items, start=1)
        ]

        export_data.sort(key=lambda x: sort_value(x.get(self.sort_key)))
        if self.reverse_order:
            export_data.reverse()

        self.exported_playlists += 1
        self.exported_tracks += len(export_data)
        if to_stdout:
            write_stdout(self.fields, export_data, self.file_formats[0])
            click.echo(f"Exported {len(export_data)} tracks from '{name}'.", err=True)
            return
        write_file(filepath, self.fields, export_data, self.file_formats)
        for ext in self.file_formats:
            click.echo(
                f"Exported {len(export_data)} tracks from '{name}' to {filepath}.{ext}",
            )
