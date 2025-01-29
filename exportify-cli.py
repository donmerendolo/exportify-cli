import os
import argparse
import configparser
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import time
import csv
from tqdm import tqdm
from tabulate import tabulate
from pprint import pprint

# Load configuration from config.cfg
config = configparser.ConfigParser()
config.read('config.cfg')
if not os.path.exists('config.cfg'):
    print("""config.cfg not found. Let's create it.

1. Go to Spotify Developer Dashboard (https://developer.spotify.com/dashboard).
2. Create a new app.
3. Set a name and description for your app.
4. Add a redirect URI (e.g. http://localhost:8080).

Now after creating the app, press the Settings button on the upper right corner.
Copy the Client ID, Client Secret and Redirect URI and paste them below.
""")
    client_id = input("Client ID: ")
    client_secret = input("Client Secret: ")
    redirect_uri = input("Redirect URI: ")

    config['spotify'] = {
        'client_id': client_id,
        'client_secret': client_secret,
        'redirect_uri': redirect_uri
    }

    with open('config.cfg', 'w') as configfile:
        config.write(configfile)

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
            
def safe_get(d, *keys):
    for key in keys:
        if not isinstance(d, dict):
            return ""
        d = d.get(key, "")
    return d if d is not None else ""

def safe_join(items, key):
    if not items:
        return ""
    return ",".join(str(safe_get(item, key)) for item in items if item)

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
        
        bar_format = '{desc}{percentage:3.0f}%|{bar}| {n_fmt:>4}/{total_fmt:>4} [{elapsed:>6}<{remaining:>6}]'
        
        # Initialize tqdm progress bar for fetching the playlist
        pbar = tqdm(desc="Fetching playlist: ",
                    unit="track",
                    bar_format=bar_format)

        # Fetch all tracks using pagination
        if playlist['id'] == 'liked_songs':
            results = rate_limited_request(sp.current_user_saved_tracks)
        else:
            results = rate_limited_request(sp.playlist_tracks, playlist['id'])
        tracks = results['items']
        total_tracks = results['total']
        
        # Set the total number of tracks after the first fetch
        pbar.total = total_tracks
        pbar.refresh()

        # Update the progress bar with the number of tracks fetched so far
        pbar.update(len(tracks))

        # Pagination loop
        while results['next']:
            results = rate_limited_request(sp.next, results)
            tracks.extend(results['items'])
            pbar.update(len(results['items']))

        # Close the progress bar after the loop
        pbar.close()
        
        # Initialize tqdm progress bar for saving the playlist
        pbar = tqdm(total=total_tracks, desc="Saving to disk:    ", unit="track",
                    bar_format=bar_format)
        
        # Iterate over each track
        for item in tracks:
            try:
                track = item.get('track', {})
                if not track:  # Skip if track is None or empty
                    continue
                
                # Safe get for nested dictionaries
                def safe_get(d, *keys):
                    for key in keys:
                        if not isinstance(d, dict):
                            return ""
                        d = d.get(key, "")
                    return d if d is not None else ""

                # Safe get for lists
                def safe_join(items, key):
                    if not items:
                        return ""
                    return ",".join(str(safe_get(item, key)) for item in items if item)

                artists = track.get('artists', [])
                artists = [a for a in artists if a is not None]  # Filter out None artists
                
                row = [
                    safe_get(track, "id"),
                    safe_join(artists, "id"),
                    safe_get(track, "name"),
                    safe_get(track, "album", "name"),
                    safe_join(artists, "name"),
                    safe_get(track, "album", "release_date"),
                    safe_get(track, "duration_ms"),
                    safe_get(track, "popularity"),
                    safe_get(item, "added_by", "id"),
                    safe_get(item, "added_at")
                ]

                writer.writerow(row)
                pbar.update()
            except Exception as e:
                print(f"\nError processing track: {str(e)}")
                continue
            
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
    
    table_data = [[playlist['name'], playlist['id'], playlist['tracks']['total']] for playlist in playlists if playlist is not None]
    print(tabulate(table_data, headers=["Name", "ID", "Tracks"], tablefmt="simple"))

def main():
    parser = argparse.ArgumentParser(description="Export Spotify playlists to CSV.")
    parser.add_argument('-a', '--all', action='store_true', help="Export all playlists")
    parser.add_argument('-p', '--playlists', nargs='+', help="Specify playlist names or IDs to export")
    parser.add_argument('-o', '--output', default='./playlists/', help="Specify the output directory (default: ./playlists/)")
    parser.add_argument('-l', '--list', action='store_true', help="List all playlists")

    args = parser.parse_args()
    
    output_dir = args.output
    
    # This line opens the Spotify authorization page in the browser
    sp.current_user()
        
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
                if (p is not None) and (p['name'] == playlist_name_or_id or p['id'] == playlist_name_or_id) ), None)
            
            if playlist:
                export_playlist_to_csv(playlist, output_dir)
            else:
                print(f"Playlist '{playlist_name_or_id}' not found.")
    else:
        print("Please specify either --all, --playlists, or --list.")

if __name__ == "__main__":
    main()
