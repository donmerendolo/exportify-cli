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
        print("""config.cfg not found. Let's create it.

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

    def _init_spotify_client(self) -> spotipy.Spotify:
        """Initialize Spotify client with OAuth."""
        return spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=self.config.get("spotify", "client_id"),
                client_secret=self.config.get("spotify", "client_secret"),
                redirect_uri=self.config.get("spotify", "redirect_uri"),
                scope="playlist-read-private playlist-read-collaborative user-library-read",
            )
        )

    def _rate_limited_request(self, func, *args, **kwargs):
        """Execute a rate-limited Spotify API request with automatic retry."""
        while True:
            try:
                return func(*args, **kwargs)
            except spotipy.SpotifyException as e:
                if e.http_status == 429:  # Rate limit exceeded
                    self._handle_rate_limit()
                else:
                    raise

    def _handle_rate_limit(self, wait_time: int = 60):
        """Handle rate limiting with a countdown timer."""
        for remaining in range(wait_time, -1, -1):
            print(f"Rate limited. Retrying in {remaining} seconds...", end="\r")
            time.sleep(1)
        print()

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
            self._rate_limited_request(self.spotify.current_user_saved_tracks)
            if playlist["id"] == "liked_songs"
            else self._rate_limited_request(
                self.spotify.playlist_tracks, playlist["id"]
            )
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

                results = self._rate_limited_request(self.spotify.next, results)

        return tracks

    def _write_tracks_to_csv(
        self, tracks: List[Dict], file_path: Path, playlist_name: str
    ):
        """Write tracks to CSV file with progress bar."""
        headers = [
            "Spotify ID",
            "Artist IDs",
            "Track Name",
            "Album Name",
            "Artist Name(s)",
            "Release Date",
            "Duration (ms)",
            "Popularity",
            "Added By",
            "Added At",
            "Genres",
            "Danceability",
            "Energy",
            "Key",
            "Loudness",
            "Mode",
            "Speechiness",
            "Acousticness",
            "Instrumentalness",
            "Liveness",
            "Valence",
            "Tempo",
            "Time Signature",
        ]

        with open(file_path, mode="w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(headers)

            rows = []

            for item in tracks:
                try:
                    track = item.get("track", {})
                    if not track:
                        continue

                    artists = [a for a in track.get("artists", []) if a is not None]

                    row = [
                        self._safe_get(track, "id"),
                        self._safe_join(artists, "id"),
                        self._safe_get(track, "name"),
                        self._safe_get(track, "album", "name"),
                        self._safe_join(artists, "name"),
                        self._safe_get(track, "album", "release_date"),
                        self._safe_get(track, "duration_ms"),
                        self._safe_get(track, "popularity"),
                        self._safe_get(item, "added_by", "id"),
                        self._safe_get(item, "added_at"),
                    ]

                    rows.append(row)

                except Exception as e:
                    print(f"\nError processing track: {str(e)}")
                    continue
            writer.writerows(rows)

        print(f"Exported playlist '{playlist_name}' to \"{file_path}\"\n")

    def get_all_playlists(self) -> List[Dict]:
        """Get all user playlists including Liked Songs."""
        playlists = self._rate_limited_request(self.spotify.current_user_playlists)[
            "items"
        ]

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
