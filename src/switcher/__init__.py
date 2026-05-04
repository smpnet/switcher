"""switcher — swap AI-agent configuration profiles via atomic directory links."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("switcher")
except PackageNotFoundError:  # editable install before metadata is registered
    __version__ = "0.0.0+local"
