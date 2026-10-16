"""The indexing loop: batching, resuming, and stopping.

`index_module` decides how the corpus is cut up and when the receipt may be
trusted. Both are the kind of thing that fails quietly -- a shortfall looks like
a smaller collection, and a bad receipt skip looks like missing records -- so the
counts are asserted rather than the absence of exceptions.
"""

from __future__ import annotations

import dataclasses

import pytest

from myfind.config import IndexConfig
from myfind.indexer import Receipt, index_module, mapping_hash


def obj(i: int, title: str | None = None) -> str:
    title = title if title is not None else f"Werk {i}"
    return (
        '<application><modules><module name="Object">'
        f'<moduleItem id="{i}">'
        f'<virtualField name="ObjObjectTitleVrt"><value>{title}</value></virtualField>'
        f'<dataField name="ObjObjectNumberTxt"><value>KdZ {i}</value></dataField>'
        "</moduleItem></module></modules></application>"
    )


# A record the mapping produces no document for -- variant 1 of the export.
EMPTY_RECORD = '<application><modules><module name="Object"/></modules></application>'


class SliceBaseX:
    """Serves db:list() and then slices of it, like the real BATCH_QUERY does."""

    def __init__(self, module_name: str, records: list[str]) -> None:
        self.module_name = module_name
        self.records = records
        self.batch_requests: list[tuple[int, int]] = []
        self.list_calls = 0

    def query(self, xquery: str, **variables) -> str:
        if not variables:
            self.list_calls += 1
            return "\n".join(
                f"{self.module_name}-{i}.xml" for i in range(len(self.records))
            )
        start = int(variables["start"]) - 1
        count = int(variables["count"])
        self.batch_requests.append((start, count))
        return "\n".join(self.records[start : start + count])


class RecordingSolr:
    def __init__(self) -> None:
        self.indexed: list[dict] = []
        self.commits = 0
        self.calls: list[int] = []

    def index(self, docs: list[dict], commit: bool = False) -> int:
        self.calls.append(len(docs))
        self.indexed.extend(docs)
        return len(docs)

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def small(config, tmp_path):
    """A config with a small batch size and a receipt in tmp_path."""
    return dataclasses.replace(
        config,
        index=IndexConfig(batch=4, receipt=str(tmp_path / "receipt.json")),
    )


def run(small, config_module="Object", records=None, **kwargs):
    module = small.module(config_module)
    bx = SliceBaseX(module.name, records if records is not None else [obj(i) for i in range(10)])
    solr = RecordingSolr()
    summary = index_module(small, module, bx, solr, verbose=False, **kwargs)
    return summary, bx, solr


# -- batching --------------------------------------------------------------


def test_the_whole_corpus_is_indexed_in_batches(small):
    summary, bx, solr = run(small)
    assert summary["source_documents"] == 10
    assert summary["documents_indexed"] == 10
    assert len(solr.indexed) == 10
    # 10 records, batch 4 -> requests of 4, 4, 2, in that order and with no gap.
    assert bx.batch_requests == [(0, 4), (4, 4), (8, 2)]


def test_the_document_list_is_fetched_once(small):
    """db:list() is called for enumeration, not per batch."""
    _, bx, _ = run(small)
    assert bx.list_calls == 1


def test_a_batch_larger_than_the_corpus_is_a_single_batch(small):
    config = dataclasses.replace(small, index=IndexConfig(batch=500, receipt=small.index.receipt))
    _, bx, _ = run(config, records=[obj(i) for i in range(3)])
    assert bx.batch_requests == [(0, 3)]


def test_a_nonsensical_batch_size_still_makes_progress(small):
    """batch=0 would divide by zero or loop forever; it is floored at 1."""
    config = dataclasses.replace(small, index=IndexConfig(batch=0, receipt=small.index.receipt))
    summary, bx, _ = run(config, records=[obj(i) for i in range(3)])
    assert summary["documents_indexed"] == 3
    assert len(bx.batch_requests) == 3


def test_an_empty_corpus_indexes_nothing_and_does_not_hang(small):
    summary, bx, solr = run(small, records=[])
    assert summary["documents_indexed"] == 0
    assert summary["source_documents"] == 0
    assert bx.batch_requests == []
    assert solr.commits == 1  # one commit even on nothing, so the call site is uniform


def test_commit_happens_once_after_all_batches(small):
    _, _, solr = run(small)
    assert solr.commits == 1
    assert solr.calls == [4, 4, 2]


# -- records that yield nothing -------------------------------------------


def test_a_record_the_mapping_cannot_read_is_skipped_not_counted(small):
    """A record with no moduleItem yields no document.

    This is the difference between "the file has fewer records" and "my guard
    dropped them" -- so the shortfall is reported rather than hidden.
    """
    records = [obj(1), EMPTY_RECORD, obj(3)]
    summary, _, solr = run(small, records=records)
    assert summary["source_documents"] == 3
    assert summary["documents_indexed"] == 2
    assert len(solr.indexed) == 2


def test_a_batch_of_only_skipped_records_still_advances_the_receipt(small):
    """Otherwise a resumed run re-reads that batch forever."""
    records = [EMPTY_RECORD] * 5
    summary, bx, _ = run(small, records=records)
    assert summary["documents_indexed"] == 0
    assert summary["batches"] == 2  # 5 records at batch 4
    receipt = Receipt.load(small.path.parent / small.index.receipt)
    assert receipt.batches_done("Object", mapping_hash(small)) == 2


# -- limits ----------------------------------------------------------------


def test_a_limit_indexes_only_that_many_documents(small):
    summary, bx, _ = run(small, limit=6)
    assert summary["source_documents"] == 6
    assert summary["documents_indexed"] == 6
    assert sum(c for _, c in bx.batch_requests) == 6


def test_a_limit_larger_than_the_corpus_is_harmless(small):
    summary, _, _ = run(small, limit=999)
    assert summary["documents_indexed"] == 10


# -- the receipt -----------------------------------------------------------


def test_progress_is_recorded_so_a_rerun_can_resume(small):
    run(small)
    receipt = Receipt.load(small.path.parent / small.index.receipt)
    entry = receipt.data["modules"]["Object"]
    assert entry["batches_done"] == 3
    assert entry["docs_indexed"] == 10
    assert entry["mapping_hash"] == mapping_hash(small)


def test_a_resumed_run_skips_the_batches_already_done(small):
    """The point of the receipt: a re-run costs the remainder, not the corpus."""
    first, _, _ = run(small)
    assert first["batches"] == 3

    # Second run over the same corpus and the same mapping.
    summary, bx, solr = run(small)
    assert bx.batch_requests == [], "a completed run should request no batches"
    assert summary["batches"] == 3  # still reports the full extent


def test_a_resumed_run_picks_up_from_a_partial_receipt(small):
    """Interrupted after two batches: the third is the only one fetched."""
    path = small.path.parent / small.index.receipt
    Receipt.load(path).record("Object", mapping_hash(small), batches_done=2, docs_indexed=8, total=10)

    summary, bx, solr = run(small)
    assert bx.batch_requests == [(8, 2)]
    assert len(solr.indexed) == 2
    assert summary["documents_indexed"] == 2


def test_a_changed_mapping_makes_a_resumed_run_start_over(small):
    """A receipt from the old mapping must not skip the batches that changed."""
    path = small.path.parent / small.index.receipt
    Receipt.load(path).record("Object", "stale-hash", batches_done=2, docs_indexed=8, total=10)

    _, bx, _ = run(small)
    assert bx.batch_requests == [(0, 4), (4, 4), (8, 2)]


def test_resume_false_ignores_the_receipt_entirely(small):
    """`--reset` semantics: rebuild regardless of what the receipt claims."""
    run(small)
    _, bx, _ = run(small, resume=False)
    assert bx.batch_requests == [(0, 4), (4, 4), (8, 2)]


def test_the_receipt_is_not_advanced_by_a_batch_that_failed(small):
    """A batch that did not parse must not be recorded as done.

    If it were, a re-run would skip exactly the records that were never written.
    """
    path = small.path.parent / small.index.receipt
    Receipt.load(path).record("Object", mapping_hash(small), batches_done=1, docs_indexed=4, total=10)

    module = small.module("Object")
    # Batch 0 (offset 0) is recorded as done, so the run starts at batch 1 --
    # and that batch is the malformed one.
    records = [obj(i) for i in range(4)] + ["<broken"] + [obj(i) for i in range(5, 10)]
    bx = SliceBaseX(module.name, records)
    with pytest.raises(RuntimeError, match="XML"):
        index_module(small, module, bx, RecordingSolr(), verbose=False)

    receipt = Receipt.load(path)
    assert receipt.batches_done("Object", mapping_hash(small)) == 1


def test_a_failed_batch_raises_something_readable(small):
    module = small.module("Object")
    bx = SliceBaseX(module.name, ["<broken"])
    with pytest.raises(RuntimeError) as exc:
        index_module(small, module, bx, RecordingSolr(), verbose=False)
    assert "batch 0 was not XML" in str(exc.value)


# -- the summary -----------------------------------------------------------


def test_the_summary_names_what_it_did(small):
    summary, _, _ = run(small)
    assert summary == {
        "module": "Object",
        "source_documents": 10,
        "documents_indexed": 10,
        "batches": 3,
        "mapping_hash": mapping_hash(small),
    }


def test_each_module_gets_its_own_receipt_entry(small):
    run(small, "Object")
    run(small, "Person", records=[obj(i) for i in range(2)])
    receipt = Receipt.load(small.path.parent / small.index.receipt)
    assert receipt.batches_done("Object", mapping_hash(small)) == 3
    assert receipt.batches_done("Person", mapping_hash(small)) == 1
