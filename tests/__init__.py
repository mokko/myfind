"""Test package.

Present so the test modules can import shared fixtures (`tests.records`) by
relative import, instead of reaching into `conftest` -- which pytest loads by
itself and is not meant to be imported as a module.
"""
