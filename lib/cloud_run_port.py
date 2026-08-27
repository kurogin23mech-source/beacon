#!/usr/bin/env python3
"""cloud_run_port.py — the Cloud Run (gcloud) port (ms-142 e-5527, spine §5).

The third outward tool the thin-action verbs touch, after git and gh: `gcloud`,
used by ``cmd_deploy_rollback`` to shift Cloud Run traffic to a prior revision.
Same per-tool ("道具別") granularity the git/gh ports use — a distinct external
tool gets its own port so the outward effect is swappable and the L3 handler
keeps only policy (require --reason / --execute, print the plan, void the record).

Audit-first design is preserved by construction: ``update_traffic_argv`` builds
the exact argv, the handler prints it for the human, then ``run`` executes *that
same argv* — "what you see is what runs". The concrete adapter shells out to
`gcloud`; swappable via ``set_adapter``.
"""
from __future__ import annotations

import subprocess
from typing import Protocol


class CloudRunAdapter(Protocol):
    """Cloud Run control interface Beacon declares."""

    def update_traffic_argv(self, service: str, revision: str, region: str) -> list: ...

    def run(self, argv: list) -> subprocess.CompletedProcess: ...


class GcloudCloudRunAdapter:
    """Default adapter: talks to Cloud Run via the `gcloud` CLI."""

    def update_traffic_argv(self, service: str, revision: str, region: str) -> list:
        """The gcloud argv that routes 100% of ``service`` traffic to ``revision``."""
        return [
            "gcloud", "run", "services", "update-traffic", service,
            f"--to-revisions={revision}=100",
            f"--region={region}",
        ]

    def run(self, argv: list) -> subprocess.CompletedProcess:
        """Execute a prepared gcloud argv (the one the handler already displayed)."""
        return subprocess.run(argv)


_adapter: CloudRunAdapter = GcloudCloudRunAdapter()


def set_adapter(adapter: CloudRunAdapter) -> None:
    """L4 wiring / tests: swap the concrete adapter behind the port."""
    global _adapter
    _adapter = adapter


def get_adapter() -> CloudRunAdapter:
    return _adapter


# --- port surface: the thin IF Beacon declares ----------------------------

def update_traffic_argv(service: str, revision: str, region: str) -> list:
    """Build the argv that shifts 100% traffic to ``revision`` (no side effect)."""
    return _adapter.update_traffic_argv(service, revision, region)


def run(argv: list) -> subprocess.CompletedProcess:
    """Execute a prepared argv. Returns CompletedProcess (caller checks returncode)."""
    return _adapter.run(argv)
