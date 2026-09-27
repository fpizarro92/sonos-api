try:
    from ._version import __version__
except ImportError:
    from _version import __version__

__all__ = ["__version__"]
