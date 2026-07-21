"""Unit tests for profession ⊃ target-class containment (ms-115 e-3785).

The data model is "a profession owns its set of target-classes": development
owns Milestone / Operation, sales owns Opportunity / Account. Before ms-115 the
projection honored this but mutation did not, so a wrong-profession target could
be created and become an invisible ghost. These tests pin that
``occupation.assert_target_class_owned`` blocks the cross-profession cases and
allows the owned ones, and that the block message is guidance-rich.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))

import occupation  # noqa: E402


def test_dev_owns_milestone_and_operation():
    dev = {"profession": "dev"}
    occupation.assert_target_class_owned(dev, "milestone")   # no raise
    occupation.assert_target_class_owned(dev, "operation")   # no raise


def test_sales_owns_opportunity_and_account():
    sales = {"profession": "sales"}
    occupation.assert_target_class_owned(sales, "opportunity")  # no raise
    occupation.assert_target_class_owned(sales, "account")      # no raise


def test_sales_cannot_create_dev_classes():
    sales = {"profession": "sales"}
    for kind in ("milestone", "operation"):
        with pytest.raises(occupation.TargetClassProfessionError):
            occupation.assert_target_class_owned(sales, kind)


def test_dev_cannot_create_sales_classes():
    dev = {"profession": "dev"}
    for kind in ("opportunity", "account"):
        with pytest.raises(occupation.TargetClassProfessionError):
            occupation.assert_target_class_owned(dev, kind)


def test_missing_profession_defaults_to_dev():
    # legacy projects (no profession field) keep the development projection, so
    # milestone is allowed and the sales classes are blocked.
    legacy = {}
    occupation.assert_target_class_owned(legacy, "milestone")
    with pytest.raises(occupation.TargetClassProfessionError):
        occupation.assert_target_class_owned(legacy, "account")


def test_block_message_names_owner_owned_classes_and_command():
    sales = {"profession": "sales"}
    with pytest.raises(occupation.TargetClassProfessionError) as ei:
        occupation.assert_target_class_owned(sales, "milestone")
    msg = str(ei.value)
    assert "dev" in msg                       # names the owning profession
    assert "opportunity" in msg and "account" in msg  # what sales CAN make
    assert "beacon opportunity add" in msg    # the exact command to use


def test_target_class_owner_lookup():
    assert occupation.target_class_owner("milestone") == "dev"
    assert occupation.target_class_owner("account") == "sales"
    assert occupation.target_class_owner("nonexistent") == ""
