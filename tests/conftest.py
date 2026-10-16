"""Shared fixtures.

The record fixtures themselves live in `tests/records.py` so the route tests can
import the same strings; `conftest.py` is loaded by pytest and is not meant to be
imported as a module.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from .records import REAL_MULTIMEDIA, REAL_OBJECT, UNTITLED_OBJECT


@pytest.fixture
def object_record() -> ET.Element:
    return ET.fromstring(REAL_OBJECT)


@pytest.fixture
def multimedia_record() -> ET.Element:
    return ET.fromstring(REAL_MULTIMEDIA)


@pytest.fixture
def untitled_record() -> ET.Element:
    return ET.fromstring(UNTITLED_OBJECT)


@pytest.fixture
def config():
    from myfind.config import load

    return load("myfind.toml")
