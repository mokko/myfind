"""The Solr request the app builds -- especially facet exclusion.

These assert on the parameters rather than on a live Solr, because the thing
that breaks silently is the *shape* of the request: no error is raised when a
facet forgets to exclude its own filter, the counts are just wrong.
"""

from __future__ import annotations

from myfind.solr import SolrClient, _escape


def _capture(monkeypatch):
    seen: dict = {}

    def fake_get(self, path, **params):
        seen["path"] = path
        seen["params"] = params
        return {"response": {"numFound": 0, "docs": []}}

    monkeypatch.setattr(SolrClient, "_get", fake_get)
    return seen


def test_no_filters_sends_no_fq(monkeypatch):
    seen = _capture(monkeypatch)
    SolrClient("http://x/solr/c").search(q="*:*", facets=["object_type"])
    assert "fq" not in seen["params"]
    assert seen["params"]["facet.field"] == ["object_type_facet"]


def test_a_selected_facet_is_tagged_and_excluded(monkeypatch):
    """The pair that makes a facet a door rather than a one-way door.

    Without `{!ex=...}` on the facet and a matching `{!tag=...}` on the filter,
    selecting one value collapses that facet to a single term and the user can
    never pick a second one.
    """
    seen = _capture(monkeypatch)
    SolrClient("http://x/solr/c").search(
        q="*:*",
        facets=["object_type", "maker"],
        filters=[("object_type", "Zeichnung")],
    )
    assert seen["params"]["fq"] == ['{!tag=object_type}object_type_facet:"Zeichnung"']
    assert seen["params"]["facet.field"] == [
        "{!ex=object_type}object_type_facet",
        "maker_facet",
    ]


def test_only_the_filtered_facet_is_excluded(monkeypatch):
    seen = _capture(monkeypatch)
    SolrClient("http://x/solr/c").search(
        q="*:*", facets=["object_type", "maker"], filters=[("maker", "Watteau, Antoine Jean")]
    )
    assert seen["params"]["facet.field"] == [
        "object_type_facet",
        "{!ex=maker}maker_facet",
    ]


def test_module_filter_is_applied(monkeypatch):
    seen = _capture(monkeypatch)
    SolrClient("http://x/solr/c").search(q="*:*", module="Object")
    assert seen["params"]["fq"] == ['module:"Object"']


def test_an_empty_filter_pairs_with_no_values(monkeypatch):
    """An empty string is not the same as absent: `field = ''` must not match all."""
    seen = _capture(monkeypatch)
    SolrClient("http://x/solr/c").search(q="*:*", filters=[("object_type", "")])
    assert seen["params"]["fq"] == ['{!tag=object_type}object_type_facet:""']


# -- escaping --------------------------------------------------------------


def test_query_syntax_characters_are_escaped():
    """A value is data. `KdZ 5653: a` must not be read as a fielded query."""
    assert _escape("KdZ 5653") == "KdZ 5653"
    assert _escape("a:b") == "a\\:b"
    assert _escape("Fran/Span") == "Fran\\/Span"
    assert _escape('say "hi"') == 'say \\"hi\\"'
    assert _escape("Watteau, Antoine Jean") == "Watteau, Antoine Jean"


def test_parentheses_and_colons_in_a_title_are_escaped():
    assert _escape('Sitzende junge Frau ("Briefleserin")') == (
        'Sitzende junge Frau \\(\\"Briefleserin\\"\\)'
    )
