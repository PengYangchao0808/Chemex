"""ChemEx-Lit: chemistry literature reaction extraction."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("chemex-lit")
except PackageNotFoundError:  # Source-tree execution before installation.
    __version__ = "1.0.0a1"

__all__ = ["__version__"]
