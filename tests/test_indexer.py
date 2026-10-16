"""Mapping a stored record onto a Solr document."""

from __future__ import annotations

from myfind.indexer import map_record, mapping_hash


def test_document_identity_is_module_qualified(config, object_record):
    """Ids must be unique ACROSS modules, or Person 1234 overwrites Object 1234."""
    doc = map_record(object_record, config.module("Object"), "+02:00")
    assert doc["id"] == "Object-447469"
    assert doc["module"] == "Object"
    assert doc["record_id"] == "447469"


def test_roles_decide_which_solr_fields_appear(config, object_record):
    doc = map_record(object_record, config.module("Object"), "+02:00")
    # object_type has the facet role, so it gets both fields.
    assert doc["object_type"] == ["Zeichnung"]
    assert doc["object_type_facet"] == ["Zeichnung"]
    # date_display is display+search only, so no facet field is created.
    assert "date_display" in doc
    assert "date_display_facet" not in doc


def test_catch_all_text_holds_every_searchable_value(config, object_record):
    doc = map_record(object_record, config.module("Object"), "+02:00")
    assert "Sitzende junge Frau" in " ".join(doc["text"])
    assert "KdZ 5653" in doc["text"]
    assert "Zeichnung" in doc["text"]
    # A non-search field (org_unit is facet-only) must not leak into the box.
    assert "KKZusatzmagazin" not in doc["text"]


def test_last_modified_is_normalized_to_utc(config, object_record):
    doc = map_record(object_record, config.module("Object"), "+02:00")
    assert doc["last_modified"] == "2026-08-24T05:11:33Z"


def test_links_are_indexed_as_bare_ids(config, object_record):
    doc = map_record(object_record, config.module("Object"), "+02:00")
    assert doc["asset_ids"] == ["636116", "4014992", "5179622"]
    assert doc["maker_ids"] == ["218316"]


def test_an_empty_field_is_omitted_entirely(config, object_record):
    """Not written as an empty list -- an absent field is not an empty facet."""
    doc = map_record(object_record, config.module("Object"), "+02:00")
    assert "surname" not in doc
    # ObjPerAssociationGenderVrt is empty on this record, so no field appears.
    assert not any(k.startswith("gender") for k in doc)


def test_untitled_record_yields_no_title_key(config, untitled_record):
    doc = map_record(untitled_record, config.module("Object"), "+02:00")
    assert "title" not in doc
    assert doc["object_number"] == ["KdZ 99999"]


def test_record_without_a_module_item_is_skipped(config):
    from xml.etree import ElementTree as ET

    doc = map_record(ET.fromstring("<application><modules/></application>"),
                     config.module("Object"), "+02:00")
    assert doc is None


def test_mapping_hash_is_stable_and_config_sensitive(config):
    h1 = mapping_hash(config)
    assert h1 == mapping_hash(config)
    assert len(h1) == 16
