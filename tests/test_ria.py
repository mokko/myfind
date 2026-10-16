"""Reading RIA values out of a stored record."""

from __future__ import annotations

from myfind import ria
from myfind.config import FieldSpec, LinkSpec


def _item(record):
    return ria.item_of(record)


def test_record_and_module_identity(object_record):
    assert ria.record_id(object_record) == "447469"
    assert ria.module_name(object_record) == "Object"


def test_datafield_and_virtualfield_values(object_record):
    item = _item(object_record)
    assert ria.extract_values(item, FieldSpec("title", "virtualField", "ObjObjectTitleVrt")) == [
        'Sitzende junge Frau ("Briefleserin")'
    ]
    assert ria.extract_values(
        item, FieldSpec("object_number", "dataField", "ObjObjectNumberTxt")
    ) == ["KdZ 5653"]


def test_vocabulary_reference_prefers_the_item_name(object_record):
    """`@name` is the clean value; formattedValue is the display string."""
    item = _item(object_record)
    assert ria.extract_values(
        item, FieldSpec("collection_group", "vocabularyReference", "ObjOrgGroupVoc")
    ) == ["KK-Fran/Span/Eng Zeichnung"]


def test_vocabulary_reference_falls_back_to_formatted_value(object_record):
    """ObjNormalLocationVoc has no @name on its item, only a formattedValue."""
    item = _item(object_record)
    assert ria.extract_values(
        item, FieldSpec("location", "vocabularyReference", "ObjNormalLocationVoc")
    ) == ["FT 5/10"]


def test_empty_element_is_not_a_value(object_record):
    """The export emits every virtualField; an empty one must yield nothing.

    This is the trap that makes a "100% coverage" field render as a blank line:
    ObjPerAssociationSurnameVrt is present on every record and empty on all of
    them, so a presence count reports 100%.
    """
    item = _item(object_record)
    spec = FieldSpec("surname", "virtualField", "ObjPerAssociationSurnameVrt")
    assert ria.extract_values(item, spec) == []


def test_untitled_record_yields_no_title(untitled_record):
    assert ria.extract_values(
        ria.item_of(untitled_record), FieldSpec("title", "virtualField", "ObjObjectTitleVrt")
    ) == []


def test_missing_field_is_not_an_error(object_record):
    item = _item(object_record)
    assert ria.extract_values(item, FieldSpec("nope", "dataField", "DoesNotExist")) == []


def test_repeated_values_are_deduplicated_in_order(object_record):
    item = _item(object_record)
    spec = FieldSpec("maker", "virtualField", "ObjPerAssociationMainParticipantNameVrt")
    assert ria.extract_values(item, spec) == ["Watteau, Antoine Jean"]


def test_links_carry_the_label_even_when_the_target_is_absent(object_record):
    """The owner reference is on every object and there is no Address database.

    The label is what a dangling link has to render, so it must come out of the
    reference item rather than from resolving the target.
    """
    item = _item(object_record)
    links = ria.extract_links(item, LinkSpec("owner_ids", "ObjOwnerRef", "Address"))
    assert links == [
        {"id": "112264", "label": "Kupferstichkabinett, Staatliche Museen zu Berlin"}
    ]


def test_multiple_links_keep_document_order(object_record):
    item = _item(object_record)
    links = ria.extract_links(item, LinkSpec("asset_ids", "ObjMultimediaRef", "Multimedia"))
    assert [l["id"] for l in links] == ["636116", "4014992", "5179622"]


def test_a_link_nested_inside_a_composite_is_still_found(multimedia_record):
    """The bug that made every asset linkless.

    An asset's only link to its object lives at
    moduleItem > composite > compositeItem > moduleReference, so a direct-child
    search returns zero links -- silently, because zero is a valid answer. Objects
    keep references as direct children, which is why only Assets broke.
    """
    item = ria.item_of(multimedia_record)
    links = ria.extract_links(item, LinkSpec("object_ids", "MulObjectRef", "Object"))
    assert [l["id"] for l in links] == ["449615"]
    assert links[0]["label"] == "KdZ 5653"


def test_a_field_inside_a_reference_is_not_the_records_own_value(multimedia_record):
    """A reference item carries its own fields and vocabularies.

    Every moduleReferenceItem has a `SortLnu`, and association items carry
    `RoleVoc`. Those belong to the referenced record, not this one, so they must
    not be read as the record's own values.
    """
    item = ria.item_of(multimedia_record)
    assert ria.extract_values(item, FieldSpec("sort", "dataField", "SortLnu")) == []
    assert ria.extract_values(item, FieldSpec("role", "vocabularyReference", "RoleVoc")) == []


def test_the_records_own_fields_are_read_alongside_a_nested_link(multimedia_record):
    item = ria.item_of(multimedia_record)
    assert ria.extract_values(
        item, FieldSpec("file", "dataField", "MulOriginalFileTxt")
    ) == ["20001815-p.jpg"]
    assert ria.extract_values(
        item, FieldSpec("asset_type", "vocabularyReference", "MulTypeVoc")
    ) == ["image"]


def test_link_labels_are_empty_when_the_reference_item_is_bare(object_record):
    item = _item(object_record)
    links = ria.extract_links(item, LinkSpec("asset_ids", "ObjMultimediaRef", "Multimedia"))
    assert links[0]["label"] == ""


# -- timestamps ------------------------------------------------------------


def test_timestamp_is_shifted_to_utc_not_merely_labelled():
    """CEST is +02:00, so 07:11 local is 05:11 UTC -- not 07:11Z."""
    assert ria.normalize_timestamp("2026-08-24 07:11:33.54", "+02:00") == "2026-08-24T05:11:33Z"


def test_timestamp_without_fractional_seconds():
    assert ria.normalize_timestamp("2026-08-24 07:11:33", "+02:00") == "2026-08-24T05:11:33Z"


def test_timestamp_negative_offset():
    assert ria.normalize_timestamp("2026-08-24 07:11:33", "-05:00") == "2026-08-24T12:11:33Z"


def test_timestamp_zero_offset_is_unchanged_apart_from_the_separator():
    assert ria.normalize_timestamp("2026-08-24 07:11:33", "+00:00") == "2026-08-24T07:11:33Z"


def test_unparseable_timestamp_returns_empty_rather_than_raising():
    """One bad value must not fail a whole indexing batch."""
    assert ria.normalize_timestamp("not a date", "+02:00") == ""
    assert ria.normalize_timestamp("", "+02:00") == ""


def test_epoch_placeholder_date_is_kept_as_is():
    """__created is 1970-01-01 on records that never recorded it; it parses."""
    assert ria.normalize_timestamp("1970-01-01 00:00:00.0", "+01:00") == "1969-12-31T23:00:00Z"
