"""ReticulumPi - An extensible Reticulum network node for Raspberry Pi."""

try:
    from ._version import __version__
except ImportError:  # Source trees that have not run the build backend yet.
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("reticulumpi")
    except PackageNotFoundError:  # pragma: no cover - only bare, uninstalled source archives
        __version__ = "0+unknown"
