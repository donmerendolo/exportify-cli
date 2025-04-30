import os
import argparse
import configparser
from typing import Dict, List
from pathlib import Path
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import csv
from tqdm import tqdm
from tabulate import tabulate


class SpotifyExporter:
    def __init__(
        self,
        config_path: str = "config.cfg",
        include_uris: bool = False,
        external_ids: bool = False,
    ):
        self.config = self._load_config(config_path)
        self.include_uris = include_uris
        self.external_ids = external_ids
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

    def _init_spotify_client(self) -> spotipy.Spotify:
        """Initialize Spotify client with OAuth."""
        original = spotipy.Spotify(
            auth_manager=SpotifyOAuth(
                client_id=self.config.get("spotify", "client_id"),
                client_secret=self.config.get("spotify", "client_secret"),
                redirect_uri=self.config.get("spotify", "redirect_uri"),
                scope="playlist-read-private playlist-read-collaborative user-library-read",
            ),
            retries=20
        )
        return original

    def export_playlist(self, playlist: Dict, output_dir: str):
        """Export a single playlist."""
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

        playlist_items = self._fetch_playlist_items(playlist)
        playlist_items_albums = self._fetch_album_details(playlist_items)
        
        album_map = {album.get("id", ""): album for album in playlist_items_albums if album}

        tracks = []
        for playlist_item in playlist_items:
            track_data = playlist_item.get("track", {})
            if not track_data:
                continue
            album_id = track_data.get("album", {}).get("id", "")
            album_data = album_map.get(album_id, {})
            tracks.append(
                self._spotify_data_to_nice_dict(playlist_item, track_data, album_data)
            )
        self._write_tracks_to_csv(tracks, file_path, playlist["name"])

    def _spotify_data_to_nice_dict(
        self, playlist_item: Dict, track_data: Dict, album_data: Dict
    ) -> Dict:
        """Convert Spotify data to a more readable dictionary."""
        artist_data = track_data.get("artists", [])
        artist_uris = [artist.get("uri", "") for artist in artist_data]
        artist_names = [artist.get("name", "") for artist in artist_data]

        track = {
            "Track URI": track_data.get("uri", ""),
            "Artist URI(s)": artist_uris,  # ",".join(artist_uris),
            "Album URI": album_data.get("uri", ""),
            "Track Name": track_data.get("name", ""),
            "Album Name": track_data.get("album", {}).get("name", ""),
            "Artist Name(s)": artist_names,  # ",".join(artist_names),
            "Release Date": track_data.get("album", {}).get("release_date", ""),
            "Duration (ms)": track_data.get("duration_ms"),
            "Popularity": track_data.get("popularity", ""),
            "Added By": playlist_item.get("added_by", {}).get("id", ""),
            "Added At": playlist_item.get("added_at", ""),
            "Record Label": album_data.get("label", ""),
            "Track ISRC": track_data.get("external_ids", {}).get("isrc", ""),
            "Album UPC": album_data.get("external_ids", {}).get("upc", ""),
        }

        if not self.include_uris:
            track.pop("Artist URI(s)")
            track.pop("Album URI")

        if not self.external_ids:
            track.pop("Track ISRC")
            track.pop("Album UPC")

        return track

    def _fetch_playlist_items(self, playlist: Dict) -> List[Dict]:
        """Fetch all tracks from a playlist with progress bar."""
        if len(playlist["name"]) > 22:
            formatted_playlist_name = playlist["name"][:19] + "...: "
        else:
            formatted_playlist_name = (playlist["name"] + ": ").ljust(24)

        # Initial request
        results = (
            self.spotify.current_user_saved_tracks()
            if playlist["id"] == "liked_songs"
            else self.spotify.playlist_tracks(playlist["id"])
        )

        total_playlist_items = results["total"]

        with tqdm(
            total=total_playlist_items,
            desc=formatted_playlist_name,
            unit="track",
            bar_format="{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]",
        ) as pbar:
            playlist_items = []
            while True:
                playlist_items.extend(results["items"])
                pbar.update(len(results["items"]))

                if not results["next"]:
                    break

                results = self.spotify.next(results)
        return playlist_items

    def _fetch_album_details(self, playlist_items: List[Dict]) -> List[Dict]:
        """Fetch album details for all tracks in the playlist."""
        album_ids = [
            album_id for album_id in {
                item["track"]["album"]["id"]
                for item in playlist_items
                if item.get("track")
            }
            if album_id
        ]

        with tqdm(
            total=len(album_ids),
            desc="Fetching album details: ",
            unit="album",
            bar_format="{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]",
        ) as pbar:
            playlist_items_albums = []
            # Spotify Batch API limit is 20 items per request
            sliced_album_ids = [
                album_ids[i : i + 20] for i in range(0, len(album_ids), 20)
            ]
            for albums_batch in sliced_album_ids:
                album_results = self.spotify.albums(albums_batch)
                playlist_items_albums.extend(album_results["albums"])
                pbar.update(len(album_results["albums"]))
        return playlist_items_albums

    def _write_tracks_to_csv(
        self, tracks: List[Dict], file_path: Path, playlist_name: str
    ):
        """Write tracks to CSV file."""
        headers = tracks[0].keys() if tracks else []
        with open(file_path, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)

            for track in tracks:
                try:
                    row = track.values()
                    writer.writerow(row)
                except Exception as e:
                    print(f"\nError processing track: {e}")
                    continue
        print(f"Exported playlist '{playlist_name}' to \"{file_path}\"")

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
        "--include-uris",
        action="store_true",
        help="Include albums and artist(s) URIs in the exported fields (track URIs are always included)",
    )
    parser.add_argument(
        "--external-ids",
        action="store_true",
        help="Include track ISRC and album UPC in the exported fields",
    )

    args = parser.parse_args()

    exporter = SpotifyExporter(
        include_uris=args.include_uris, external_ids=args.external_ids
    )

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
