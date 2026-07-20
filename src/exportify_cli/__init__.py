from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("exportify-cli")
except PackageNotFoundError:  # running from source without install
    __version__ = "1.0.0+dev"
