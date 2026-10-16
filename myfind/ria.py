"""MuseumPlus RIA records: reading values out of a stored `moduleItem`.

The stored shape, once per record:

    <application><modules><module name="Object" totalSize="1000">
      <moduleItem id="447469">
        <systemField name="__lastModified"><value>2026-08-24 07:11:33.54</value></systemField>
        <dataField name="ObjObjectNumberTxt"><value>KdZ 5653</value></dataField>
        <virtualField name="ObjObjectTitleVrt"><value>Sitzende junge Frau</value></virtualField>
        <vocabularyReference name="ObjOrgGroupVoc">
          <vocabularyReferenceItem id="1632860" name="KK-Fran/Span/Eng Zeichnung">
            <formattedValue language="de">KK-Fran/Span/Eng Zeichnung</formattedValue>
          </vocabularyReferenceItem>
        </vocabularyReference>
        <moduleReference name="ObjPerAssociationRef" targetModule="Person">
          <moduleReferenceItem moduleItemId="218316" seqNo="0">
            <formattedValue language="de">Ausführung: Jean Antoine Watteau (1684-1721), Zeichner*in</formattedValue>
          </moduleReferenceItem>
        </moduleReference>
      </moduleItem>
    </module></modules></application>

Two rules that come straight from the data:

- **Empty is not missing.** The export emits `<virtualField name="X"><value/></virtualField>`
  for every record and leaves it empty when there is nothing, so a field is only
  a value when `normalize-space()` is non-empty. Measured: the title element is
  present on 47,853 objects and empty on 611 of them; the date on 7,971.
- **`Tab`/`Cr`/etc. are data, not noise.** Value text is only stripped of
  surrounding whitespace; interior whitespace is left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from .config import FieldSpec, LinkSpec

# Element kinds that carry their value in a child <value>.
_VALUE_KINDS = {"dataField", "virtualField", "systemField", "composite"}


def record_id(record: ET.Element) -> str | None:
    """The moduleItem id of a stored record, as a string."""
    item = record.find(".//moduleItem")
    if item is None:
        return None
    return item.get("id")


def module_name(record: ET.Element) -> str | None:
    module = record.find(".//module")
    return module.get("name") if module is not None else None


def item_of(record: ET.Element) -> ET.Element | None:
    return record.find(".//moduleItem")


def _text_of(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return (element.text or "").strip()


# Two rules govern how a record's own values are found, and both are load-bearing.
#
# **1. Search at ANY depth, not just direct children.** The export nests
# arbitrarily. In the Multimedia module a link lives at
#     moduleItem > composite[@name='MulReferencesCre'] > compositeItem > moduleReference
# so a direct-child search finds NO links at all and every asset renders with no
# owning object and no photographer -- silently, because zero links is a valid
# answer. Object happens to keep its references as direct children, which is what
# makes the bug look module-specific rather than systemic.
#
# **2. Exclude anything inside a moduleReference.** A reference item carries its
# own fields: every moduleReferenceItem has a `SortLnu`, and association items
# carry `RoleVoc`/`AttributionVoc` vocabularies. Reading those would fold a
# referenced record's data into this one.
#
# The exclusion is done in Python rather than in the XPath, because ElementTree
# supports only a subset of XPath -- `[not(ancestor::moduleReference)]` raises
# SyntaxError. It has no parent pointers either, so the set of enclosed elements
# is collected once by walking each reference downwards.
def _enclosed_in_reference(item: ET.Element) -> set[int]:
    """Identity set of every element that sits inside a moduleReference."""
    enclosed: set[int] = set()
    for ref in item.iter("moduleReference"):
        for node in ref.iter():
            enclosed.add(id(node))
    return enclosed


def _own_elements(item: ET.Element, kind: str, name: str) -> list[ET.Element]:
    """Descendants of `kind` with @name, excluding a reference's own contents."""
    enclosed = _enclosed_in_reference(item)
    return [
        el
        for el in item.iter(kind)
        if el.get("name") == name and id(el) not in enclosed
    ]


def extract_values(item: ET.Element, spec: FieldSpec) -> list[str]:
    """Every non-empty value of one field on one record, in document order.

    A field may legitimately repeat (MuseumPlus fields are often repeatable), so
    this always returns a list; the caller decides whether to join or facet.
    """
    if spec.kind in _VALUE_KINDS:
        raw = [_text_of(el.find("value")) for el in _own_elements(item, spec.kind, spec.name)]
    elif spec.kind == "vocabularyReference":
        # The item's @name is the clean value; formattedValue is the display
        # string and is what to fall back to when @name is absent.
        raw = []
        for ref in _own_elements(item, "vocabularyReference", spec.name):
            for vi in ref.findall("vocabularyReferenceItem"):
                value = vi.get("name") or _text_of(vi.find("formattedValue"))
                raw.append(value)
    else:
        raise ValueError(f"unsupported element kind: {spec.kind!r}")

    # Deduplicate while preserving order: the same value repeated is one facet
    # term, and the export does repeat values across language variants.
    seen: dict[str, None] = {}
    for value in raw:
        value = value.strip()
        if value:
            seen.setdefault(value, None)
    return list(seen)


def extract_links(item: ET.Element, spec: LinkSpec) -> list[dict[str, str]]:
    """Linked records, as {id, label}.

    The label is taken from the reference item itself and is present **even when
    the target record was never ingested** -- "Kupferstichkabinett" comes from an
    Address reference on every object, and there is no Address database. So the
    label is what a dangling link renders, and the id is what it tries to resolve
    to.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    # Any depth, not direct children: an asset's MulObjectRef sits two levels
    # down inside a composite, and a direct-child search silently yields zero
    # links for that whole module. See _own_elements.
    for ref in item.iter("moduleReference"):
        if ref.get("name") != spec.reference:
            continue
        for ri in ref.findall("moduleReferenceItem"):
            target_id = ri.get("moduleItemId")
            if not target_id or target_id in seen:
                continue
            seen.add(target_id)
            out.append(
                {
                    "id": target_id,
                    "label": _text_of(ri.find("formattedValue")),
                }
            )
    return out


def normalize_timestamp(raw: str, offset: str) -> str:
    """Local wall clock -> ISO 8601 UTC, or "" when unparseable.

    The source is a *local* timestamp with no zone ("2026-08-24 07:11:33.54").
    Appending "Z" would label it as UTC and be silently two hours wrong, which
    breaks sorting and any incremental logic downstream. Convert properly, and
    return "" rather than raising so one bad value cannot fail a whole batch.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    iso = raw.replace(" ", "T", 1)
    try:
        naive = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    try:
        delta = timedelta(
            hours=int(offset[1:3]), minutes=int(offset[4:6]) if len(offset) >= 6 else 0
        )
        if offset[0] == "-":
            delta = -delta
    except (ValueError, IndexError):
        delta = timedelta(0)
    aware = naive.replace(tzinfo=timezone(delta))
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
