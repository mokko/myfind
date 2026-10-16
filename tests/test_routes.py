"""The Flask routes, rendered -- with both backends faked.

These exist because the route layer was the least-covered code in the project and
the source of every bug that shipped: two record pages 500ing on a Jinja name
collision, every asset headed "Untitled record", and every asset page linkless.
None of those needed a live backend; they needed the page rendered and inspected.
"""

from __future__ import annotations

import json
import re

import pytest

from myfind import app as app_module

from .fakes import FakeBaseX, FakeBaseXDown, FakeSolr, FakeSolrDown
from .records import REAL_MULTIMEDIA, REAL_OBJECT


@pytest.fixture
def wired(monkeypatch):
    """An app whose Solr and BaseX are the fakes. Returns (client, solr, basex)."""
    solr = FakeSolr("http://fake/solr/myfind")
    basex = FakeBaseX()

    monkeypatch.setattr(app_module, "SolrClient", lambda url, **kw: solr)
    monkeypatch.setattr(app_module, "BaseXClient", lambda **kw: basex)

    app = app_module.create_app("myfind.toml")
    app.config["TESTING"] = True
    return app.test_client(), solr, basex


@pytest.fixture
def loaded(wired):
    """A wired client with real records, arranged like the real store.

    The maker (218316) and the asset's owning object (449615) resolve; the
    three asset ids on the Object do not, and neither does the owner (Address,
    never ingested) -- the same mix the real store has, where 14 referenced
    modules are absent.
    """
    client, solr, basex = wired
    basex.add("Object-447469.xml", REAL_OBJECT)
    basex.add("Multimedia-268711.xml", REAL_MULTIMEDIA)
    basex.add("Object-449615.xml", REAL_OBJECT)          # the asset's owning object
    basex.add(
        "Person-218316.xml",
        "<application><modules><module name='Person'>"
        "<moduleItem id='218316'><dataField name='PerNameTxt'>"
        "<value>Watt</value></dataField></moduleItem></module></modules></application>",
    )
    return client, solr, basex


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", html)


# -- record pages ----------------------------------------------------------


def test_an_object_record_page_renders_at_all(loaded):
    """REGRESSION: this returned HTTP 500.

    The context held a dict with a `values` key, and Jinja resolves `f.values`
    to `dict.values` -- the bound method -- so `f.values | join(', ')` raised
    "'builtin_function_or_method' object is not iterable". A dataclass fixed it;
    only rendering the page catches it.
    """
    client, _, _ = loaded
    resp = client.get("/Object/447469")
    assert resp.status_code == 200
    body = _text(resp.data.decode())
    assert "Sitzende junge Frau" in body
    assert "KdZ 5653" in body
    assert "Zeichnung" in body


def test_every_entity_record_page_renders(loaded):
    """A 500 in any of the three is the bug class this file exists for."""
    client, _, _ = loaded
    for path in ("/Object/447469", "/Multimedia/268711", "/Person/218316"):
        assert client.get(path).status_code == 200, path


def test_an_asset_record_is_headed_by_its_file_name(loaded):
    """REGRESSION: every asset said "Untitled record".

    The heading chain was title -> object_number, and an Asset has neither, so
    the value that identifies it (the file name) was ignored while sitting
    visibly below the wrong heading.
    """
    client, _, _ = loaded
    body = _text(client.get("/Multimedia/268711").data.decode())
    assert "20001815-p.jpg" in body
    assert "Untitled record" not in body


def test_an_asset_record_links_to_its_owning_object(loaded):
    """REGRESSION: every asset page was linkless.

    An asset's MulObjectRef is nested at
    moduleItem > composite > compositeItem > moduleReference, and the lookup
    searched direct children -- so the whole module lost its links silently,
    because zero links is a valid answer.
    """
    client, _, _ = loaded
    body = client.get("/Multimedia/268711").data.decode()
    assert "/Object/449615" in body
    assert "Objects" in _text(body)


def test_a_resolvable_link_becomes_a_link(loaded):
    client, _, _ = loaded
    body = client.get("/Object/447469").data.decode()
    assert 'href="/Person/218316"' in body


def test_a_link_whose_target_is_absent_is_marked_not_dropped(loaded):
    """The owner reference is on every object and there is no Address module.

    It must render its label and say so -- 404ing or omitting it would lose the
    holding institution from every record page.
    """
    client, _, _ = loaded
    body = _text(client.get("/Object/447469").data.decode())
    assert "Kupferstichkabinett, Staatliche Museen zu Berlin" in body
    assert "not in this index" in body
    assert 'href="/Address/112264"' not in body


def test_an_unknown_record_is_404_not_500(loaded):
    """A missing document is a normal answer; it must not look like a fault."""
    client, _, _ = loaded
    assert client.get("/Object/999999999").status_code == 404


def test_an_unknown_entity_is_404(loaded):
    client, _, _ = loaded
    assert client.get("/Nonsense/").status_code == 404
    assert client.get("/Nonsense/1").status_code == 404


# -- search pages ----------------------------------------------------------


def _seed_objects(solr, n=3, total=42):
    solr.docs = [
        {
            "id": f"Object-{1000 + i}",
            "module": "Object",
            "record_id": str(1000 + i),
            "title": [f"Werk {i}"],
            "object_number": [f"KdZ {i}"],
            "maker": ["Watteau, Antoine Jean"],
            "object_type": ["Zeichnung"],
        }
        for i in range(n)
    ]
    solr.counts = {"Object": total}
    solr.facets = {
        "Object": {"object_type": ["Zeichnung", 20520, "Druck", 8204]}
    }


def test_a_search_page_lists_results_with_their_fields(wired):
    client, solr, _ = wired
    _seed_objects(solr)
    body = _text(client.get("/Object/").data.decode())
    assert "Werk 0" in body
    assert "KdZ 0" in body
    assert "42" in body  # the total, not just the page


def test_facets_are_rendered_with_counts(wired):
    client, solr, _ = wired
    _seed_objects(solr)
    body = _text(client.get("/Object/").data.decode())
    assert "Zeichnung" in body
    assert "20520" in body
    assert "Druck" in body


def test_an_applied_facet_becomes_a_removable_chip(wired):
    client, solr, _ = wired
    _seed_objects(solr)
    body = _text(client.get("/Object/?object_type=Zeichnung").data.decode())
    assert "Filtered by" in body
    # The chip removes exactly that value, leaving the rest of the query.
    assert 'class="chip" href="/Object/"' in body.replace("  ", " ")


def test_the_app_passes_the_selected_facet_to_the_client_as_a_filter(wired):
    """The app's half of faceting.

    The app hands the client a `filters` list and the entity name; the CLIENT
    turns those into the `{!tag=...}` / `{!ex=...}` pair (asserted against the
    real client in test_solr_params.py). Asserting on `fq` here would be
    asserting on the fake.
    """
    client, solr, _ = wired
    _seed_objects(solr)
    client.get("/Object/?object_type=Zeichnung")
    params = solr.last_call()
    assert params["filters"] == [("object_type", "Zeichnung")]
    assert params["module"] == "Object"
    assert "object_type" in params["facets"]


def test_a_facet_may_be_selected_more_than_once_within_the_same_field(wired):
    """Repeating a field is an OR within it -- two types, not a contradiction."""
    client, solr, _ = wired
    _seed_objects(solr)
    client.get("/Object/?object_type=Zeichnung&object_type=Druck")
    assert solr.last_call()["filters"] == [
        ("object_type", "Zeichnung"),
        ("object_type", "Druck"),
    ]


def test_the_query_survives_a_facet_click(wired):
    """Losing the search terms when narrowing is what makes a portal feel broken."""
    client, solr, _ = wired
    _seed_objects(solr)
    body = client.get("/Object/?q=Watteau&object_type=Zeichnung").data.decode()
    assert "q=Watteau" in body


def test_only_real_facets_are_accepted_as_filters(wired):
    """REGRESSION: `?limit=2` became a Solr filter and returned a 400.

    `undefined field limit_facet` -- an allow-list of the entity's own facets
    prevents any stray query parameter reaching Solr as a filter.
    """
    client, solr, _ = wired
    _seed_objects(solr)
    resp = client.get("/Object/?limit=2&bogus=x&utm_source=mail")
    assert resp.status_code == 200
    params = solr.last_call()
    assert "fq" not in params or all("bogus" not in f for f in params["fq"])


def test_a_query_with_no_matches_says_so_rather_than_looking_empty(wired):
    client, solr, _ = wired
    solr.docs = []
    solr.counts = {"Object": 0}
    body = _text(client.get("/Object/?q=nothingmatchesthis").data.decode())
    assert "No objects matched" in body
    assert "removing a filter" not in body  # no filters were applied


def test_pagination_appears_when_there_are_more_pages(wired):
    client, solr, _ = wired
    _seed_objects(solr, n=3, total=42)
    body = _text(client.get("/Object/").data.decode())
    assert "Page 1 of 3" in body
    assert "page=2" in body


def test_no_pager_when_everything_fits_on_one_page(wired):
    client, solr, _ = wired
    _seed_objects(solr, n=3, total=3)
    body = _text(client.get("/Object/").data.decode())
    assert "Page 1 of" not in body


def test_the_global_search_page_renders(wired):
    client, solr, _ = wired
    _seed_objects(solr)
    assert client.get("/search?q=Watteau").status_code == 200


def test_home_lists_each_entity_with_its_count(wired):
    client, solr, _ = wired
    solr.counts = {"Object": 47853, "Person": 5799, "Multimedia": 127551}
    body = _text(client.get("/").data.decode())
    assert "47,853" in body
    assert "127,551" in body


# -- the JSON API ----------------------------------------------------------


def test_the_api_returns_json_and_keeps_limit_out_of_the_filters(wired):
    client, solr, _ = wired
    _seed_objects(solr)
    resp = client.get("/api/Object?q=Watteau&limit=2")
    assert resp.status_code == 200
    payload = json.loads(resp.data)
    assert payload["entity"] == "Object"
    assert payload["numFound"] == 42
    assert "fq" not in solr.last_call() or all(
        "limit" not in f for f in solr.last_call().get("fq", [])
    )


def test_the_api_rejects_an_unknown_entity(wired):
    client, _, _ = wired
    assert client.get("/api/Nonsense").status_code == 404


# -- failure modes ---------------------------------------------------------


def test_healthz_reports_both_backends(wired):
    client, _, _ = wired
    payload = json.loads(client.get("/healthz").data)
    assert payload == {"solr": True, "solr_documents": 0, "basex": True}


def test_a_solr_that_is_down_gives_503_not_500(monkeypatch):
    """REGRESSION: a Solr that was not *listening* raised httpx.ConnectError
    straight past the handler written for exactly that case."""
    monkeypatch.setattr(app_module, "SolrClient", lambda url, **kw: FakeSolrDown(url))
    monkeypatch.setattr(app_module, "BaseXClient", lambda **kw: FakeBaseX())
    client = app_module.create_app("myfind.toml").test_client()

    resp = client.get("/Object/")
    assert resp.status_code == 503
    assert b"Solr is not answering" in resp.data


def test_a_basex_that_is_down_gives_503_not_500(monkeypatch):
    monkeypatch.setattr(app_module, "SolrClient", lambda url, **kw: FakeSolr(url))
    monkeypatch.setattr(app_module, "BaseXClient", lambda **kw: FakeBaseXDown())
    client = app_module.create_app("myfind.toml").test_client()

    resp = client.get("/Object/447469")
    assert resp.status_code == 503
    assert b"BaseX is not answering" in resp.data


def test_a_backend_outage_never_renders_as_an_empty_result_set(monkeypatch):
    """The worst outcome: "no matches" when the truth is "the index is down"."""
    monkeypatch.setattr(app_module, "SolrClient", lambda url, **kw: FakeSolrDown(url))
    monkeypatch.setattr(app_module, "BaseXClient", lambda **kw: FakeBaseX())
    client = app_module.create_app("myfind.toml").test_client()

    body = _text(client.get("/Object/?q=Watteau").data.decode())
    assert "No objects matched" not in body
    assert "not answering" in body
