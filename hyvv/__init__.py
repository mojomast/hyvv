"""hyvv worker-host runtime."""

from pathlib import Path

__all__ = ["__version__"]


def _version() -> str:
    """Resolve the package version from installed metadata or the source tree.

    pyproject.toml is the single authoritative source. An installed wheel or
    editable install reports the dist-info metadata the build wrote from
    pyproject.toml; a bare checkout falls back to reading pyproject.toml
    beside this package so both paths agree by construction.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("hyvv")
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    import tomllib

    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]


__version__ = _version()
