import os
import argparse
import configparser
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import time
import csv
from tqdm import tqdm
from tabulate import tabulate


# Load configuration from config.cfg
config = configparser.ConfigParser()
config.read('config.cfg')

client_id = config.get('spotify', 'client_id')
client_secret = config.get('spotify', 'client_secret')
redirect_uri = config.get('spotify', 'redirect_uri')

# Spotify authentication
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(client_id=client_id,
    client_secret=client_secret,
    redirect_uri=redirect_uri,
    scope="playlist-read-private playlist-read-collaborative user-library-read"))

def rate_limited_request(func, *args, **kwargs):
    """Handles rate limiting by retrying after the specified delay."""
    while True:
        try:
            return func(*args, **kwargs)
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                for remaining in range(60, -1, -1):
                    print(f"Rate limited. Retrying in {remaining} seconds...", end='\r')
                    time.sleep(1)
                print()
            else:
                raise

def export_playlist_to_csv(playlist, output_dir):
    """Exports a playlist's tracks to a CSV file."""
    # Ensure the output directory exists
    os.makedirs(output_dir, exist_ok=True)
    
    print(playlist['name'])
    
    # Construct the file path
    # Sanitize the playlist name to create a safe filename
    sanitized_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in playlist['name'])
    playlist_filename = sanitized_name.replace(' ', '_').lower() + ".csv"
    file_path = os.path.join(output_dir, playlist_filename)
    
    with open(file_path, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        
        # Write headers
        headers = ['Spotify ID', 'Artist IDs', 'Track Name',
                   'Album Name', 'Artist Name(s)', 'Release Date',
                   'Duration (ms)', 'Popularity', 'Added By', 'Added At',
                   'Genres', 'Danceability', 'Energy', 'Key', 'Loudness', 'Mode',
                   'Speechiness', 'Acousticness', 'Instrumentalness', 'Liveness',
                   'Valence', 'Tempo', 'Time Signature']
        writer.writerow(headers)
        
        # Initialize tqdm progress bar for fetching the playlist
        pbar = tqdm(desc="Fetching playlist", unit="track",
                    bar_format='{l_bar}{bar}{n:>5}/{total} [{elapsed:>6}<{remaining:>6}]')

        # Fetch all tracks using pagination
        if playlist['id'] == 'liked_songs':
            results = rate_limited_request(sp.current_user_saved_tracks)
            tracks = results['items']
            total_tracks = results['total']
        else:
            results = rate_limited_request(sp.playlist_tracks, playlist['id'])
            tracks = results['items']
            total_tracks = results['total']
        
        # Set the total number of tracks after the first fetch
        pbar.total = total_tracks
        pbar.refresh()  # Refresh the progress bar to update the total

        # Update the progress bar with the number of tracks fetched so far
        pbar.update(len(tracks))

        # Pagination loop
        while results['next']:
            results = rate_limited_request(sp.next, results)
            tracks.extend(results['items'])
            pbar.update(len(results['items']))  # Update the progress bar

        # Close the progress bar after the loop
        pbar.close()
        
        # Initialize tqdm progress bar for saving the playlist
        pbar = tqdm(total=total_tracks, desc="Saving to disk   ", unit="track",
                    bar_format='{l_bar}{bar}{n:>5}/{total} [{elapsed:>6}<{remaining:>6}]')
        
        # Iterate over each track
        for item in tracks:
            track = item['track']
            row = [
                track["id"] if "id" in track else "",
                ",".join([artist["id"] if "id" in artist else "" for artist in track["artists"]]),
                track["name"] if "name" in track else "",
                track["album"]["name"] if "album" in track and "name" in track["album"] else "",
                ",".join([artist["name"] if "name" in artist else "" for artist in track["artists"]]),
                track["album"]["release_date"] if "album" in track and "release_date" in track["album"] else None,
                track["duration_ms"] if "duration_ms" in track else "",
                track["popularity"] if "popularity" in track else "",
                item["added_by"]["id"] if "added_by" in item and "id" in item["added_by"] else "",
                item["added_at"] if "added_at" in item else ""
            ]
            
            row.extend

            writer.writerow(row)
            pbar.update()
            
        pbar.close()
            
    print(f"Exported playlist '{playlist['name']}' to {file_path}\n")

def export_all_playlists(output_dir):
    """Exports all of the user's playlists as CSV files."""
    playlists = rate_limited_request(sp.current_user_playlists)['items']
    liked_songs = {
        'name': 'Liked Songs',
        'id': 'liked_songs',
        'tracks': {
            'total': sp.current_user_saved_tracks()['total']
        }
    }
    playlists.insert(0, liked_songs)
    
    # Progress bar for the total process
    for playlist in playlists:
        export_playlist_to_csv(playlist, output_dir)

def list_playlists():
    """Lists all playlists in the user's account in a nicely formatted table."""
    playlists = rate_limited_request(sp.current_user_playlists)['items']
    liked_songs = {
        'name': 'Liked Songs',
        'id': 'liked_songs',
        'tracks': {
            'total': sp.current_user_saved_tracks()['total']
        }
    }
    playlists.insert(0, liked_songs)
    
    table_data = [[playlist['name'], playlist['id'], playlist['tracks']['total']] for playlist in playlists]
    print(tabulate(table_data, headers=["Name", "ID", "Tracks"], tablefmt="simple"))

def main():
    parser = argparse.ArgumentParser(description="Export Spotify playlists to CSV.")
    parser.add_argument('-a', '--all', action='store_true', help="Export all playlists")
    parser.add_argument('-p', '--playlists', nargs='+', help="Specify playlist names or IDs to export")
    parser.add_argument('-o', '--output', default='./playlists/', help="Specify the output directory (default: ./playlists/)")
    parser.add_argument('-l', '--list', action='store_true', help="List all playlists")

    args = parser.parse_args()

    output_dir = args.output

    if args.list:
        list_playlists()
    elif args.all:
        export_all_playlists(output_dir)
    elif args.playlists:
        for playlist_name_or_id in args.playlists:
            playlists = [
                {
                    'name': 'Liked Songs',
                    'id': 'liked_songs',
                    'tracks': {
                        'total': sp.current_user_saved_tracks()['total']
                    }
                }
            ]
            playlists.extend(rate_limited_request(sp.current_user_playlists)['items'])
            playlist = next((
                p for p in playlists
                if p['name'] == playlist_name_or_id or p['id'] == playlist_name_or_id), None)
            
            if playlist:
                export_playlist_to_csv(playlist, output_dir)
            else:
                print(f"Playlist '{playlist_name_or_id}' not found.")
    else:
        print("Please specify either --all, --playlists, or --list.")

if __name__ == "__main__":
    main()
