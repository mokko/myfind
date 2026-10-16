"""The index receipt.

The receipt is fail-safe machinery: it decides which batches a resumed run may
skip. A receipt that lies skips chunks into a hole, and nothing on screen says
which -- so every way it can be wrong is pinned here, including the ones that
must fail *closed* (assume nothing was done) rather than open.
"""

from __future__ import annotations

import json

import pytest

from myfind.indexer import Receipt, mapping_hash


def test_an_absent_receipt_reads_as_nothing_done(tmp_path):
    receipt = Receipt.load(tmp_path / "nope.json")
    assert receipt.data == {}
    assert receipt.batches_done("Object", "abc") == 0


def test_a_corrupt_receipt_fails_closed(tmp_path):
    """Unreadable means "nothing has been done".

    The alternative reading -- trusting a garbled file -- assumes work that may
    not have happened, which loses records silently. A truncated write is
    exactly how this file gets corrupted, so it must not raise either.
    """
    path = tmp_path / "r.json"
    path.write_text('{"modules": {"Object": {"batches_do', encoding="utf-8")
    receipt = Receipt.load(path)
    assert receipt.data == {}
    assert receipt.batches_done("Object", "abc") == 0


def test_a_receipt_that_is_a_directory_fails_closed(tmp_path):
    """OSError as well as a parse error -- both mean "cannot trust this"."""
    receipt = Receipt.load(tmp_path)
    assert receipt.data == {}


def test_recording_then_loading_round_trips(tmp_path):
    path = tmp_path / "r.json"
    Receipt.load(path).record("Object", "hash1", batches_done=7, docs_indexed=1750, total=47853)

    reloaded = Receipt.load(path)
    assert reloaded.batches_done("Object", "hash1") == 7
    entry = reloaded.data["modules"]["Object"]
    assert entry["docs_indexed"] == 1750
    assert entry["source_documents"] == 47853
    assert entry["mapping_hash"] == "hash1"


def test_a_changed_mapping_invalidates_the_skip_list(tmp_path):
    """The whole reason the hash is in there.

    If the mapping changed, the batches it skipped are exactly the ones whose
    content needs rebuilding -- so a receipt from the old mapping must claim
    nothing.
    """
    path = tmp_path / "r.json"
    Receipt.load(path).record("Object", "old-hash", batches_done=12, docs_indexed=3000, total=47853)
    assert Receipt.load(path).batches_done("Object", "new-hash") == 0
    # and the old hash still works, so this is invalidation and not data loss
    assert Receipt.load(path).batches_done("Object", "old-hash") == 12


def test_an_unknown_module_has_nothing_done(tmp_path):
    path = tmp_path / "r.json"
    Receipt.load(path).record("Object", "h", 3, 750, 47853)
    assert Receipt.load(path).batches_done("Person", "h") == 0


def test_recording_one_module_preserves_another(tmp_path):
    """Modules are indexed in sequence; each update must not reset the others."""
    path = tmp_path / "r.json"
    receipt = Receipt.load(path)
    receipt.record("Object", "h", 10, 2500, 47853)
    receipt.record("Person", "h", 4, 1000, 5799)

    reloaded = Receipt.load(path)
    assert reloaded.batches_done("Object", "h") == 10
    assert reloaded.batches_done("Person", "h") == 4


def test_recording_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "r.json"
    Receipt.load(path).record("Object", "h", 1, 250, 100)
    assert path.exists()


def test_recording_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "r.json"
    Receipt.load(path).record("Object", "h", 1, 250, 100)
    assert path.exists()
    assert not (tmp_path / "r.json.tmp").exists()


def test_a_receipt_is_never_left_half_written(tmp_path, monkeypatch):
    """The atomicity claim, tested by breaking the move.

    The write goes beside the target and is moved into place, so a process that
    dies mid-write cannot leave a truncated receipt in place claiming work that
    was never done. Here the move fails on purpose: the old, truthful receipt
    must still be what is on disk.
    """
    path = tmp_path / "r.json"
    Receipt.load(path).record("Object", "h", 5, 1250, 47853)
    good = path.read_text(encoding="utf-8")

    import os as _os

    def explode(src, dst):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(_os, "replace", explode)
    with pytest.raises(OSError):
        Receipt.load(path).record("Object", "h", 6, 1500, 47853)

    assert path.read_text(encoding="utf-8") == good
    assert json.loads(path.read_text(encoding="utf-8"))["modules"]["Object"]["batches_done"] == 5


# -- the mapping hash ------------------------------------------------------


def test_the_mapping_hash_is_stable_across_loads(config):
    from myfind.config import load

    assert mapping_hash(config) == mapping_hash(load("myfind.toml"))


def test_the_mapping_hash_changes_when_a_field_role_changes(config):
    """Roles decide what lands in the index, so they are part of the hash."""
    import dataclasses

    from myfind.config import FieldSpec

    module = config.module("Object")
    fields = list(module.fields)
    fields[0] = FieldSpec(**{**fields[0].__dict__, "roles": ("display",)})
    altered = dataclasses.replace(
        config, modules=tuple(dataclasses.replace(m, fields=tuple(fields)) if m.name == "Object" else m
                              for m in config.modules)
    )
    assert mapping_hash(altered) != mapping_hash(config)


def test_the_mapping_hash_changes_when_a_link_changes(config):
    import dataclasses

    from myfind.config import LinkSpec

    altered = dataclasses.replace(
        config,
        modules=tuple(
            dataclasses.replace(m, links=m.links + (LinkSpec("x", "SomeRef", "Person"),))
            if m.name == "Object"
            else m
            for m in config.modules
        ),
    )
    assert mapping_hash(altered) != mapping_hash(config)


def test_the_mapping_hash_ignores_things_that_do_not_affect_documents(config):
    """The cache key must not move because a page size changed."""
    import dataclasses

    from myfind.config import SiteConfig

    altered = dataclasses.replace(config, site=SiteConfig(name="x", subtitle="y", page_size=100))
    assert mapping_hash(altered) == mapping_hash(config)
