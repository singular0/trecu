"""trecu — read and decode Triumph motorcycle ECU fault codes over a KKL cable."""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    # Sourced from the installed package metadata, which hatch-vcs derives from
    # the git release tag at build time (see pyproject.toml).
    __version__ = _version("trecu")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0+unknown"
