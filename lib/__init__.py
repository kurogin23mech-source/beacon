"""Beacon lib — flat module collection.

This is intentionally NOT a Python package in the traditional sense; the
modules under here use flat imports (`from store import ...` etc.) and
are loaded by adding this directory to `sys.path` rather than via
`from beacon_cli._bundled_lib import store`.

The `__init__.py` exists so setuptools includes this directory in the
wheel as `beacon_cli/_bundled_lib/`. Do NOT add re-exports here — that
would conflict with the flat-import convention used by `commands.py`.
"""
