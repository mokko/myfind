"""Sort keys and display labels.

The sort bug these cover raised no error anywhere until a sorted request was
made: `object_number_sort` was indexed as multi-valued analysed text, and Solr
cannot sort that, so the page 400s. The guard that keeps an arbitrary query
string out of the sort clause is the other half.
"""

from __future__ import annotations

from myfind.app import _sort_param, label_for


def _module(config, name="Object"):
    return config.module(name)


def test_default_sort_is_relevance_then_recency(config):
    assert _sort_param(None, _module(config)) == "score desc, last_modified desc"
    assert _sort_param("", _module(config)) == "score desc, last_modified desc"


def test_last_modified_sorts_descending_by_default(config):
    assert _sort_param("last_modified", _module(config)) == "last_modified desc"


def test_leading_minus_reverses_direction(config):
    assert _sort_param("-last_modified", _module(config)) == "last_modified asc"


def test_sortable_field_sorts_on_the_field_itself_not_a_facet_copy(config):
    """Sorting a `_facet` field would be a 400: facet fields are multi-valued."""
    assert _sort_param("object_number_sort", _module(config)) == "object_number_sort desc"
    assert _sort_param("-object_number_sort", _module(config)) == "object_number_sort asc"
    assert "_facet" not in _sort_param("object_number_sort", _module(config))


def test_a_field_that_is_not_declared_sortable_is_refused(config):
    """The allow-list: the query string cannot name a sort clause.

    `title` is searchable and displayable but not sortable, so it falls back to
    the default rather than being passed through to Solr.
    """
    default = "score desc, last_modified desc"
    assert _sort_param("title", _module(config)) == default
    assert _sort_param("score,", _module(config)) == default
    assert _sort_param("last_modified asc, title desc", _module(config)) == default
    assert _sort_param("nonexistent", _module(config)) == default


def test_an_unknown_module_cannot_smuggle_a_sort(config):
    """Person has no object_number_sort, so it is refused there too."""
    person = config.module("Person")
    assert _sort_param("object_number_sort", person) == "score desc, last_modified desc"


# -- labels ----------------------------------------------------------------


def test_known_fields_get_readable_labels():
    assert label_for("object_number") == "Object number"
    assert label_for("date_display") == "Date"
    assert label_for("material_technique") == "Material / technique"


def test_the_two_maker_forms_are_labelled_differently():
    """Otherwise the record page shows "Maker" on two consecutive rows."""
    assert label_for("maker") != label_for("maker_display")


def test_link_fields_are_labelled_plural():
    assert label_for("asset_ids") == "Assets"
    assert label_for("maker_ids") == "Makers"
    assert label_for("object_ids") == "Objects"


def test_an_unmapped_field_falls_back_to_title_case():
    assert label_for("some_new_field") == "Some new field"
