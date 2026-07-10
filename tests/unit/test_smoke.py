"""Day 2 smoke tests for the minimal project package."""

import sys

import app


def test_package_is_importable() -> None:
    assert app.__version__ == "0.1.0"


def test_runtime_uses_python_311() -> None:
    assert sys.version_info[:2] == (3, 11)
