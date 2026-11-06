"""Host version from the installed distribution metadata."""

from importlib.metadata import version

__version__ = version("qwenpaw-data-host-core")
