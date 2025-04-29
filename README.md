# exportify-cli
![](image.png?raw=true "exportify-cli") \
Export Spotify playlists to CSV directly from the terminal, inspired by [pavelkomarov/exportify](https://github.com/pavelkomarov/exportify).

This tool can export all saved playlists, including liked songs.

## Installation:
**If you use Windows, you can download the [binary](https://github.com/donmerendolo/exportify-cli/releases/latest/download/exportify-cli.exe) and skip steps 1 and 2. It's recommended to place it in a dedicated folder for better organization.**
1. **Clone this repository:**
```bash
git clone https://github.com/donmerendolo/exportify-cli.git
```

2. **Install the required packages:**
```bash
cd exportify-cli
pip install -r requirements.txt
```
(recommended to use a virtual environment)
  
3. **Set up Client ID, Client Secret and Redirect URI:**

The first time you run exportify-cli, it will guide you through the setup:
```
File "config.cfg" not found. Let's create it.

1. Go to Spotify Developer Dashboard (https://developer.spotify.com/dashboard).
2. Create a new app.
3. Set a name and description for your app.
4. Add a redirect URI (e.g. http://localhost:8080).

Now after creating the app, press the Settings button on the upper right corner.
Copy the Client ID, Client Secret and Redirect URI and paste them below.
```

After running `python exportify-cli.py` (or [`exportify-cli.exe`](https://github.com/donmerendolo/exportify-cli/releases/latest/download/exportify-cli.exe) if you use the Windows binary) the first time, it should keep you authenticated so you don't have to log in each time. If you wish to log out, simply remove the `.cache` file.

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

### Examples:
```
# List all saved playlists
python exportify-cli.py --list

# Export all saved playlists, including liked songs
exportify-cli.exe --all

# Export playlist whose name is "COCHE"
python exportify-cli.py -p COCHE

# Export playlist whose ID is "2VqAIceMCzBRhzq6zVmDZw"
exportify-cli.exe -p 2VqAIceMCzBRhzq6zVmDZw
```
