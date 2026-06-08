"""Single source of truth for the Beacon package version.

Kept in sync with `lib/commands.py:__version__`. When bumping the version,
update BOTH places (or run `scripts/release.py` which handles it). A future
task will collapse them into one — see ms-44 e-695.
"""

__version__ = "0.25.0"
