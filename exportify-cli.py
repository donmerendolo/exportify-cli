import os
import argparse
import configparser
import time
from typing import Dict, List
from pathlib import Path
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import csv
from tqdm import tqdm
from tabulate import tabulate
from functools import wraps


def rate_limited(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        while True:
            try:
                return func(*args, **kwargs)
            except spotipy.SpotifyException as e:
                if e.http_status == 429:
                    wait = int(e.headers.get("Retry-After", 60))
                    for remaining in range(wait, -1, -1):
                        print(
                            f"Rate limited. Retrying in {remaining} seconds...",
                            end="\r",
                        )
                        time.sleep(1)
                else:
                    raise

    return wrapper


class RateLimitedSpotify:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if callable(attr):
            return rate_limited(attr)
        return attr


class SpotifyExporter:
    def __init__(self, config_path: str = "config.cfg"):
        self.config = self._load_config(config_path)
        self.spotify = self._init_spotify_client()

    def _load_config(self, config_path: str) -> configparser.ConfigParser:
        """Load or create Spotify API configuration."""
        config = configparser.ConfigParser()

        if not os.path.exists(config_path):
            return self._create_config(config, config_path)

        config.read(config_path)
        return config

    def _create_config(
        self, config: configparser.ConfigParser, config_path: str
    ) -> configparser.ConfigParser:
        """Create a new configuration file with user input."""
        print("""File "config.cfg" not found. Let's create it.

1. Go to Spotify Developer Dashboard (https://developer.spotify.com/dashboard).
2. Create a new app.
3. Set a name and description for your app.
4. Add a redirect URI (e.g. http://localhost:8080).

Now after creating the app, press the Settings button on the upper right corner.
Copy the Client ID, Client Secret and Redirect URI and paste them below.
""")
        config["spotify"] = {
            "client_id": input("Client ID: "),
            "client_secret": input("Client Secret: "),
            "redirect_uri": input("Redirect URI: "),
        }

        with open(config_path, "w") as configfile:
            config.write(configfile)

        return config

    def _init_spotify_client(self) -> RateLimitedSpotify:
        """Initialize Spotify client with OAuth."""
        original = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=self.config.get("spotify", "client_id"),
                client_secret=self.config.get("spotify", "client_secret"),
                redirect_uri=self.config.get("spotify", "redirect_uri"),
                scope="playlist-read-private playlist-read-collaborative user-library-read",
            )
        )
        return RateLimitedSpotify(original)

    def _safe_get(self, d: Dict, *keys) -> str:
        """Safely get nested dictionary values."""
        for key in keys:
            if not isinstance(d, dict):
                return ""
            d = d.get(key, "")
        return d if d is not None else ""

    def _safe_join(self, items: List, key: str) -> str:
        """Safely join list items with a specific key."""
        if not items:
            return ""
        return ",".join(str(self._safe_get(item, key)) for item in items if item)

    def export_playlist(self, playlist: Dict, output_dir: str):
        """Export a single playlist to CSV."""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Create sanitized filename
        playlist_filename = (
            "".join(
                c if (c.isalnum() or c in (" ", "_", "-")) else "_"
                for c in playlist["name"]
            )
            .replace(" ", "_")
            .lower()
            + ".csv"
        )

        file_path = output_path / playlist_filename

        tracks = self._fetch_playlist_tracks(playlist)
        self._write_tracks_to_csv(tracks, file_path, playlist["name"])

    def _fetch_playlist_tracks(self, playlist: Dict) -> List[Dict]:
        """Fetch all tracks from a playlist with progress bar."""
        tracks = []

        # Initial request
        results = (
            self.spotify.current_user_saved_tracks()
            if playlist["id"] == "liked_songs"
            else self.spotify.playlist_tracks(playlist["id"])
        )

        total_tracks = results["total"]

        if len(playlist["name"]) > 22:
            formatted_playlist_name = playlist["name"][:19] + "...: "
        else:
            formatted_playlist_name = (playlist["name"] + ": ").ljust(24)

        with tqdm(
            total=total_tracks,
            desc=formatted_playlist_name,
            unit="track",
            bar_format="{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]",
        ) as pbar:
            while True:
                tracks.extend(results["items"])
                pbar.update(len(results["items"]))

                if not results["next"]:
                    break

                results = self.spotify.next(results)

        return tracks

    def _write_tracks_to_csv(
        self, track_info: List[Dict], file_path: Path, playlist_name: str
    ):
        """Write tracks to CSV file with clean, DRY extractor definitions."""
        # Define each column with a header and a small extractor function
        fields = {
            "Track URI": lambda track: self._safe_get(track, "uri"),
            "Artist URI(s)": lambda artists: self._safe_join(artists, "uri"),
            "Album URI(s)": lambda album: self._safe_join(album, "uri"),
            "Track Name": lambda track: self._safe_get(track, "name"),
            "Album Name": lambda track: self._safe_get(track, "album", "name"),
            "Artist Name(s)": lambda artists: self._safe_join(artists, "name"),
            "Release Date": lambda track: self._safe_get(
                track, "album", "release_date"
            ),
            "Duration (ms)": lambda track: self._safe_get(track, "duration_ms"),
            "Popularity": lambda track: self._safe_get(track, "popularity"),
            "Added By": lambda item: self._safe_get(item, "added_by", "id"),
            "Added At": lambda item: self._safe_get(item, "added_at"),
            "Genres": lambda: "",
            "Record Label": lambda: "",
            "Danceability": lambda: "",
            "Energy": lambda: "",
            "Key": lambda: "",
            "Loudness": lambda: "",
            "Mode": lambda: "",
            "Speechiness": lambda: "",
            "Acousticness": lambda: "",
            "Instrumentalness": lambda: "",
            "Liveness": lambda: "",
            "Valence": lambda: "",
            "Tempo": lambda: "",
            "Time Signature": lambda: "",
        }

        headers = fields.keys()
        with open(file_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)

            for item in track_info:
                try:
                    if not item.get("track"):
                        continue
                    row = fields.values()(item)
                    writer.writerow(row)
                except Exception as e:
                    print(f"\nError processing track: {e}")
                    continue

        print(f"Exported playlist '{playlist_name}' to \"{file_path}\"\n")

    def get_all_playlists(self) -> List[Dict]:
        """Get all user playlists including Liked Songs."""
        playlists = self.spotify.current_user_playlists()["items"]

        # Add Liked Songs as a special playlist
        liked_songs = {
            "name": "Liked Songs",
            "id": "liked_songs",
            "tracks": {"total": self.spotify.current_user_saved_tracks()["total"]},
        }
        return [liked_songs] + playlists

    def list_playlists(self):
        """Display all playlists in a formatted table."""
        playlists = self.get_all_playlists()
        table_data = [
            [p["name"], p["id"], p["tracks"]["total"]]
            for p in playlists
            if p is not None
        ]
        print(tabulate(table_data, headers=["Name", "ID", "Tracks"], tablefmt="simple"))


def main():
    parser = argparse.ArgumentParser(description="Export Spotify playlists to CSV.")
    parser.add_argument("-a", "--all", action="store_true", help="Export all playlists")
    parser.add_argument(
        "-p", "--playlists", nargs="+", help="Specify playlist names or IDs to export"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="./playlists/",
        help="Specify the output directory (default: ./playlists/)",
    )
    parser.add_argument("-l", "--list", action="store_true", help="List all playlists")
    parser.add_argument(
        "-i",
        "--id",
        action="store_true",
        help="Include albums and artist(s) IDs in the exported fields",
    )

    args = parser.parse_args()

    exporter = SpotifyExporter()

    # Initialize Spotify connection
    exporter.spotify.current_user()

    if args.list:
        exporter.list_playlists()
        return

    if args.all:
        for playlist in exporter.get_all_playlists():
            exporter.export_playlist(playlist, args.output)
        return

    if args.playlists:
        playlists = exporter.get_all_playlists()
        for name_or_id in args.playlists:
            playlist = next(
                (
                    p
                    for p in playlists
                    if p and (p["name"] == name_or_id or p["id"] == name_or_id)
                ),
                None,
            )

            if playlist:
                exporter.export_playlist(playlist, args.output)
            else:
                print(f"Playlist '{name_or_id}' not found.")
        return

    print("Please specify either --all, --playlists, or --list.")


if __name__ == "__main__":
    main()
