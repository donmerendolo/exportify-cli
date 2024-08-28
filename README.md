# exportify-cli
![](exportify.png?raw=true "exportify-cli") \
Export Spotify playlists to CSV. Inspired by [pavelkomarov/exportify](https://github.com/pavelkomarov/exportify).

This tool can export all saved playlists of a user, including liked songs.

## Installation:
1. **Clone this repository:**
```bash
$ git clone https://github.com/donmerendolo/exportify-cli.git
```

2. **Install the required packages:**
```bash
$ cd exportify-cli
$ pip install -r requirements.txt
```
(recommended to use a virtual environment)
  
3. **Create an app on Spotify Developer Dashboard:**
   - Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
   - Set a name and description for your app.
   - In the "Redirect URIs" field, use: `http://localhost:3000/callback` (for example).

4. **Configure your credentials:**
   - Replace `CLIENT_ID`, `CLIENT_SECRET`, and `REDIRECT_URI` in the `config.cfg.RENAME` file.
   - These values can be found in the "Settings" section of your app on the Spotify Developer Dashboard.

6. **Rename config.cfg.RENAME to config.cfg**

After running `python exportify.py` the first time, it should keep you authenticated so you don't have to log in each time. If you wish to log out, simply remove the `.cache` file.

---

Tested on Windows with Python 3.11.9.

## Usage:
```
usage: exportify.py [-h] [-a] [-p PLAYLISTS [PLAYLISTS ...]] [-o OUTPUT] [-l]

Export Spotify playlists to CSV.

options:
  -h, --help            show this help message and exit
  -a, --all             Export all playlists
  -p PLAYLISTS [PLAYLISTS ...], --playlists PLAYLISTS [PLAYLISTS ...]
                        Specify playlist names or IDs to export
  -o OUTPUT, --output OUTPUT
                        Specify the output directory (default: ./playlists/)
  -l, --list            List all playlists
```
