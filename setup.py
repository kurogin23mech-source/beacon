"""Imperative build shim on top of the pyproject metadata (ms-170 e-6476).

All static metadata lives in ``pyproject.toml``. This file exists only to do
the two dynamic things that a static manifest cannot:

1. **Bundle the per-platform Go viewer as a wheel script.** When the release
   build stages a ``beacon-view`` binary for the wheel's target platform and
   points ``BEACON_BUNDLE_VIEWER`` at it, we add it to ``scripts`` so pip
   installs it onto the user's PATH (bin/Scripts) with the executable bit set.
   ``beacon view`` then finds it via ``shutil.which("beacon-view")`` — no extra
   build or fetch needed (e-6476). Without the env var (normal ``pip install``
   / local dev), nothing is bundled and the wheel stays pure Python.

2. **Tag the wheel to its platform.** A wheel carrying a native binary is not
   ``py3-none-any``. We mark it non-pure so pip on each OS/arch picks the wheel
   with the matching binary. ``BEACON_WHEEL_PLAT`` lets the release build set an
   explicit tag for cross-compiled targets (e.g. building linux-arm64 on an
   x86_64 runner); otherwise the build platform's own tag is used.

The chosen delivery mechanism (PATH script, not package-data) is why this is a
``scripts`` entry: pip preserves the +x bit for scripts, sidestepping the wheel
format's unreliable file-mode handling. Confirmed with the maintainer
(2026-09-15); the accepted trade-off is possible version skew when multiple
beacon installs put a ``beacon-view`` on PATH.
"""
from __future__ import annotations

import os

from setuptools import setup

# setuptools requires script paths relative to this setup.py directory (never
# absolute), so the release build stages the binary inside the repo tree (e.g.
# viewer/dist/beacon-view) and we relativize it here.
_here = os.path.dirname(os.path.abspath(__file__))
_staged = os.environ.get("BEACON_BUNDLE_VIEWER", "").strip()
_scripts = []
if _staged:
    _abs = os.path.abspath(_staged)
    if os.path.isfile(_abs):
        _scripts = [os.path.relpath(_abs, _here).replace(os.sep, "/")]

_cmdclass: dict = {}
if _scripts:
    try:
        # setuptools >= 70.1 vendors bdist_wheel (the build-isolation env pulls
        # a recent setuptools per pyproject's build-system.requires).
        from setuptools.command.bdist_wheel import bdist_wheel as _base_bdist_wheel
    except ImportError:  # older toolchains: fall back to the wheel package
        from wheel.bdist_wheel import bdist_wheel as _base_bdist_wheel

    try:
        from setuptools.command.build_scripts import build_scripts as _base_build_scripts
    except ImportError:
        # build_scripts is a distutils command (setuptools vendors distutils).
        from distutils.command.build_scripts import build_scripts as _base_build_scripts

    _plat = os.environ.get("BEACON_WHEEL_PLAT", "").strip()

    class _PlatformBdistWheel(_base_bdist_wheel):
        def finalize_options(self):
            super().finalize_options()
            # Carrying a native binary -> not a pure-Python wheel (gets a
            # platform tag so pip picks the wheel matching the user's OS/arch).
            self.root_is_pure = False
            if _plat:
                self.plat_name = _plat
                self.plat_name_supplied = True

        def get_tag(self):
            # Only the bundled binary is platform-specific; the Python code is
            # pure and runs on any Python 3. Keep the platform tag but force the
            # interpreter/ABI tags to py3/none so pip installs this wheel on
            # every CPython 3.x (not just the version that built it).
            _impl, _abi, plat = super().get_tag()
            return "py3", "none", plat

    class _BinaryFriendlyBuildScripts(_base_build_scripts):
        """Copy scripts verbatim, +x, no shebang/encoding processing.

        setuptools' default ``copy_scripts`` runs ``tokenize.detect_encoding``
        on every script to preserve/adjust the shebang. A prebuilt binary
        (Mach-O/ELF/PE) is not text, so that raises SyntaxError. We bundle the
        Go ``beacon-view`` binary here, so copy it byte-for-byte and make it
        executable — exactly what a script on PATH needs.
        """

        def copy_scripts(self):
            import shutil

            self.mkpath(self.build_dir)
            outfiles = []
            for script in self.scripts:
                outfile = os.path.join(
                    self.build_dir, os.path.basename(script))
                outfiles.append(outfile)
                if not self.dry_run:
                    shutil.copyfile(script, outfile)
                    os.chmod(outfile, 0o755)
            # Signature parity with setuptools>=60 (outfiles, updated_files).
            return outfiles, outfiles

    _cmdclass["bdist_wheel"] = _PlatformBdistWheel
    _cmdclass["build_scripts"] = _BinaryFriendlyBuildScripts

setup(scripts=_scripts, cmdclass=_cmdclass)
