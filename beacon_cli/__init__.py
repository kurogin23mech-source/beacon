"""Beacon CLI — cross-platform Python entry-point package.

This is a thin wrapper around the existing `lib/*.py` modules + `bin/beacon`
bash script. The goal is to make `pipx install beacon` work on every OS,
including Windows where the bash entry-point cannot run.

Status: ms-44 e-695 scaffold. Most commands still delegate to the bash
script via subprocess when bash is available. Full Python rewrite is
tracked under e-695.
"""

from ._version import __version__

__all__ = ["__version__"]
