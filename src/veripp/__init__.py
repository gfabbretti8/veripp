"""veripp: AI-operated formal verification for C++."""

try:  # the version lives in pyproject.toml; do not duplicate it here
    from importlib.metadata import version as _version

    __version__ = _version("veripp")
except Exception:  # running from a source tree with nothing installed
    __version__ = "0+unknown"
