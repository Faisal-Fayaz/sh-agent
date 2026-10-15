"""The package version.

Read from installed metadata rather than written down here, so it cannot drift
from pyproject.toml the way two hardcoded strings already had.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sh-agent")
except PackageNotFoundError:
    # Running from a source tree that was never installed. Not worth failing over.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
