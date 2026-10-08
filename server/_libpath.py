"""Put ``lib/`` on ``sys.path`` — the single owner of that knowledge for ``server/``.

Most modules under ``server/`` import core modules that live in ``lib/``
(``core`` / ``org`` / ``idempotency`` / …). Historically the only thing that
made those imports resolve was one line near the top of ``server/app.py``, so
every other server module silently depended on "app.py was imported first".

That assumption does not hold for every entry point:

* ``store_router`` → ``firestore_client`` is imported directly by tests and by
  tooling that never touches ``app.py``.
* the systemd unit (``deploy/systemd/beacon-api.service``) runs
  ``uvicorn app:app`` from ``/opt/beacon/server`` with no ``PYTHONPATH``, so
  only ``server/`` is on the path at interpreter start. (The Dockerfile does
  set ``PYTHONPATH=/app/lib:/app/server``, which is why the container never
  showed the problem.)

So a module that needs ``lib/`` must say so itself::

    from __future__ import annotations
    import _libpath  # noqa: F401 — puts lib/ on sys.path
    import idempotency as _idem

Importing this module is the whole effect; it is idempotent and safe to import
from as many places as need it. Keep the ``import _libpath`` line **above** the
first ``lib/`` import in the file — below it, the import has already failed.
``tests/test_server_import_bootstrap.py`` fails if that ordering is broken.
"""

import os
import sys

LIB_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))


def ensure() -> str:
    """Prepend ``lib/`` to ``sys.path`` if it is not already there.

    Returns the directory, so callers can assert on it in tests.
    """
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    return LIB_DIR


ensure()
