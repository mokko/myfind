"""Tests against the real BaseX store and the real Solr index.

Everything else in this suite fakes both backends, which is why it is fast and
runs anywhere -- but it also means the unit tests cannot see a wrong XPath, a
missing document, a schema that will not sort, or a facet whose counts are
wrong. Those are exactly the failures this project has produced, so they are
tested here, against the live services.

Marked `integration`. Each test skips itself when its backend is unreachable, so
a bare `pytest` still runs anywhere:

    pytest -m "not integration"     # the fast loop
    pytest -m integration           # only the live ones
"""

from __future__ import annotations

import pytest

from myfind.basex import BaseXClient, BaseXError
from myfind.config import load
from myfind.ria import extract_links, extract_values, item_of, record_id
from myfind.solr import SolrClient, SolrError

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def config():
    return load("myfind.toml")


@pytest.fixture(scope="module")
def live_basex(config):
    with BaseXClient(
        url=config.basex.url,
        user=config.basex.user,
        password=config.basex.password,
        timeout=config.basex.timeout,
    ) as bx:
        if not bx.ping():
            pytest.skip(f"BaseX not answering at {config.basex.url}")
        yield bx


@pytest.fixture(scope="module")
def live_solr(config):
    with SolrClient(config.solr.url) as solr:
        if not solr.alive():
            pytest.skip(f"Solr not answering at {config.solr.url}")
        yield solr


# -- BaseX -----------------------------------------------------------------


def test_the_store_holds_all_three_modules(live_basex, config):
    """db:list() is a name lookup, not a scan -- unlike count(collection(...))."""
    for module in config.modules:
        names = [n for n in live_basex.query(f"db:list('{module.database}')").split("\n") if n.strip()]
        assert names, f"{module.database} is empty"
        # A sanity floor, not the exact count: a partial re-ingest should be
        # visible here rather than silently halving the portal.
        assert len(names) > 100, f"{module.database} holds only {len(names)} documents"


def test_a_real_record_round_trips_by_document_name(live_basex, config):
    """The O(1) lookup the whole record-page design rests on."""
    obj = config.module("Object")
    node = live_basex.fetch_record(obj.database, "Object-447469.xml")
    assert node is not None
    item = item_of(node)
    assert record_id(node) == "447469"
    assert extract_values(item, [f for f in obj.fields if f.field == "object_number"][0]) == [
        "KdZ 5653"
    ]


def test_an_absent_document_returns_none_rather_than_raising(live_basex, config):
    """The `doc-available()` guard, live.

    A bare `doc()` raises `[basex:doc] Database path ... yields no documents` and
    REST answers 500 -- so without the guard, following one dangling link is a
    server fault instead of a graceful "not in this index".
    """
    obj = config.module("Object")
    assert live_basex.fetch_record(obj.database, "Object-999999999.xml") is None


def test_links_resolve_to_documents_that_exist(live_basex, config):
    """Resolves a real record's real links, and reports the dangling ones.

    Links that point at a module never ingested are the expected case here, not
    a failure -- what is asserted is that `existing_documents` agrees with a
    direct name lookup for every target.
    """
    obj = config.module("Object")
    node = live_basex.fetch_record(obj.database, "Object-447469.xml")
    item = item_of(node)

    checked = 0
    for link in obj.links:
        targets = [t["id"] for t in extract_links(item, link)]
        if not targets:
            continue
        target = config.find(link.target)
        claimed = (
            live_basex.existing_documents(target.database, target.name, targets)
            if target
            else set()
        )
        for tid in targets:
            # The stored document is named for the MODULE, but lives in the
            # DATABASE -- which is `sync_Person`, not `Person`. Passing the
            # module name as the database silently looks in the wrong place.
            direct = live_basex.fetch_record(
                target.database if target else link.target,
                f"{link.target}-{tid}.xml",
            )
            assert (tid in claimed) == (direct is not None), f"{link.field} {tid}"
            checked += 1
    assert checked > 0, "no links found on the sample record -- mapping or traversal broke"


# -- Solr ------------------------------------------------------------------


def test_the_index_covers_every_module(live_solr, config):
    """The shortfall check: a silently missing module is the failure a receipt
    makes easy to miss."""
    for module in config.modules:
        res = live_solr.search(q="*:*", rows=0, module=module.name)
        assert res["response"]["numFound"] > 0, f"{module.name} is not indexed"


def test_sorting_by_a_sortable_field_is_accepted_and_ordered(live_solr, config):
    """The bug that shipped: `object_number_sort` was multi-valued analysed text,
    Solr cannot sort that, and every sorted request was a 400.

    Sorting is also asserted to be *ordered*, since a field of the wrong type can
    be accepted and still come back in an arbitrary order.
    """
    res = live_solr.search(
        q="*:*", rows=5, module="Object", sort="object_number_sort asc",
        fields="object_number_sort",
    )
    values = [d["object_number_sort"] for d in res["response"]["docs"]]
    assert len(values) == 5
    assert values == sorted(values), values


def test_facet_counts_exclude_the_facets_own_filter(live_solr):
    """With one value selected, the facet must still offer the others.

    If counts did not exclude the facet's own filter, the type facet would
    collapse to the single selected term and a second value could never be
    chosen -- a one-way door, and no error anywhere.
    """
    field = "object_type"
    baseline = live_solr.search(q="*:*", rows=0, facets=[field], module="Object")
    values = baseline["facet_counts"]["facet_fields"][f"{field}_facet"]
    assert len(values) > 2, "need at least two facet terms for this test to mean anything"
    first = values[0]

    filtered = live_solr.search(
        q="*:*", rows=0, facets=[field], filters=[(field, first)], module="Object"
    )
    after = filtered["facet_counts"]["facet_fields"][f"{field}_facet"]
    offered = [after[i] for i in range(0, len(after), 2)]
    others = [v for v in offered if v != first]
    assert others, f"facet collapsed to only {first!r}: exclusion is not working"


def test_a_facet_count_matches_a_plain_query_for_that_value(live_solr):
    """A facet count is a promise about how many records a click will show."""
    res = live_solr.search(q="*:*", rows=0, facets=["object_type"], module="Object")
    values = res["facet_counts"]["facet_fields"]["object_type_facet"]
    for i in range(0, min(len(values), 6), 2):
        value, count = values[i], values[i + 1]
        direct = live_solr.search(
            q="*:*", rows=0, filters=[("object_type", value)], module="Object"
        )
        assert direct["response"]["numFound"] == count, f"{value}: {count} vs {direct}"
