"""Schema generation from the mapping, and the client's transport.

The schema is generated rather than hand-written so a field cannot be declared
in the mapping and missing from the index. That makes the *types* the whole
contract -- and a wrong type is silent until someone clicks a facet or a sort
control. The sort-field rules here are the ones whose violation shipped a 400.
"""

from __future__ import annotations

import httpx
import pytest

from myfind.solr import SolrClient, SolrError, _escape


def capture_schema(config, monkeypatch, fail_with: str | None = None):
    """Run ensure_schema with _post captured instead of sent."""
    client = SolrClient("http://fake/solr/myfind")
    captured: list[dict] = []

    def fake_post(path, payload, ctype="application/json"):
        captured.append(payload)
        if fail_with:
            raise SolrError(fail_with)
        return {}

    monkeypatch.setattr(client, "_post", fake_post)
    result = client.ensure_schema(config)
    return captured, result


def spec_for(captured: list[dict], name: str) -> dict | None:
    for payload in captured:
        spec = payload.get("add-field")
        if spec and spec["name"] == name:
            return spec
    return None


# -- the base fields -------------------------------------------------------


def test_the_identifier_is_the_unique_key(config, monkeypatch):
    captured, _ = capture_schema(config, monkeypatch)
    ident = spec_for(captured, "id")
    assert ident["uniqueKey"] is True
    assert ident["required"] is True
    assert ident["type"] == "string"


def test_the_discriminator_and_record_id_exist(config, monkeypatch):
    captured, _ = capture_schema(config, monkeypatch)
    assert spec_for(captured, "module")["type"] == "string"
    assert spec_for(captured, "record_id")["type"] == "string"


def test_the_catch_all_text_field_is_analysed_and_multi_valued(config, monkeypatch):
    captured, _ = capture_schema(config, monkeypatch)
    text = spec_for(captured, "text")
    assert text["type"] == "text_general"
    assert text["multiValued"] is True


def test_last_modified_is_a_date_and_is_declared_exactly_once(config, monkeypatch):
    """Declared twice, the second add-field either fails or wins the race and
    leaves a date indexed as text -- so it sorts lexically and filters wrongly."""
    captured, _ = capture_schema(config, monkeypatch)
    specs = [p["add-field"] for p in captured if "add-field" in p]
    named = [s for s in specs if s["name"] == "last_modified"]
    assert len(named) == 1, f"last_modified declared {len(named)} times"
    assert named[0]["type"] == "pdate"


# -- field roles -----------------------------------------------------------


def test_a_facetable_field_is_created_twice_with_different_types(config, monkeypatch):
    """Analysed for search, `string` for the facet terms.

    A facet term is an exact value: analysed text would split "KdZ 5653" into two
    meaningless facet values, and nothing errors.
    """
    captured, _ = capture_schema(config, monkeypatch)
    searchable = spec_for(captured, "object_type")
    facet = spec_for(captured, "object_type_facet")
    assert searchable["type"] == "text_general"
    assert searchable["multiValued"] is True
    assert facet["type"] == "string"
    assert facet["multiValued"] is True
    assert facet["docValues"] is True


def test_a_field_that_is_not_facetable_gets_no_facet_copy(config, monkeypatch):
    """date_display is display+search only; a stray `<field>_facet` would be a
    facet that exists in Solr and is never populated."""
    captured, _ = capture_schema(config, monkeypatch)
    assert spec_for(captured, "date_display") is not None
    assert spec_for(captured, "date_display_facet") is None


def test_a_sort_only_field_is_single_valued_string_with_doc_values(config, monkeypatch):
    """THE regression. `object_number_sort` used to be multi-valued analysed
    text, and Solr cannot sort a multi-valued field at all -- so every sorted
    request was a 400, invisible until the sort control was clicked."""
    captured, _ = capture_schema(config, monkeypatch)
    sort = spec_for(captured, "object_number_sort")
    assert sort["type"] == "string"
    assert sort["multiValued"] is False
    assert sort["docValues"] is True


def test_link_fields_are_string_sets(config, monkeypatch):
    captured, _ = capture_schema(config, monkeypatch)
    for link_field in ("maker_ids", "asset_ids", "owner_ids", "object_ids"):
        spec = spec_for(captured, link_field)
        assert spec is not None, link_field
        assert spec["type"] == "string"
        assert spec["multiValued"] is True


def test_every_mapped_field_gets_a_solr_definition(config, monkeypatch):
    """The reason the schema is generated: a field in the mapping and absent from
    the index is a facet that silently returns nothing."""
    captured, _ = capture_schema(config, monkeypatch)
    declared = {p["add-field"]["name"] for p in captured if "add-field" in p}

    for module in config.modules:
        for spec in module.fields:
            assert spec.field in declared, f"{module.name}.{spec.field}"
            if spec.has("facet"):
                assert f"{spec.field}_facet" in declared
        for link in module.links:
            assert link.field in declared


def test_no_field_is_declared_twice(config, monkeypatch):
    """Two definitions of one name means one of them silently does not apply.

    `title`, `org_unit` and `object_ids` are each mapped in more than one module,
    so this was three real duplicates before the merge was added.
    """
    captured, _ = capture_schema(config, monkeypatch)
    names = [p["add-field"]["name"] for p in captured if "add-field" in p]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"declared more than once: {sorted(duplicates)}"


def test_a_field_mapped_by_two_modules_is_declared_once(config, monkeypatch):
    """Shared names collapse when the definitions agree."""
    captured, _ = capture_schema(config, monkeypatch)
    names = [p["add-field"]["name"] for p in captured if "add-field" in p]
    assert names.count("org_unit") == 1
    assert names.count("title") == 1
    assert names.count("object_ids") == 1


def test_two_modules_disagreeing_about_one_field_is_an_error(config, monkeypatch):
    """The failure mode this prevents.

    If Object declared `org_unit` sort-only while Person declared it analysed,
    whichever module were iterated first would silently decide the type -- and a
    facet would be missing, or a sort would 400, with nothing to point at.
    """
    import dataclasses

    from myfind.config import FieldSpec

    def as_sort_only(module):
        return dataclasses.replace(
            module,
            fields=tuple(
                FieldSpec(f.field, f.kind, f.name, ("sort",))
                if f.field == "org_unit"
                else f
                for f in module.fields
            ),
        )

    clashing = dataclasses.replace(
        config,
        modules=tuple(as_sort_only(m) if m.name == "Object" else m for m in config.modules),
    )
    with pytest.raises(ValueError, match="two different definitions"):
        capture_schema(clashing, monkeypatch)


def test_the_reported_count_matches_what_was_sent(config, monkeypatch):
    captured, result = capture_schema(config, monkeypatch)
    assert result["created"] == len(captured)
    assert result["existing"] == 0


# -- idempotency -----------------------------------------------------------


def test_an_already_existing_field_is_not_an_error(config, monkeypatch):
    """Re-running the schema step is the normal case, and every field will
    already exist."""
    captured, result = capture_schema(config, monkeypatch, fail_with="Field 'id' already exists.")
    assert result["existing"] == len(captured)
    assert result["created"] == 0


def test_an_unexpected_schema_error_propagates(config, monkeypatch):
    """A real failure must not be swallowed as idempotency."""
    with pytest.raises(SolrError, match="insufficient permissions"):
        capture_schema(config, monkeypatch, fail_with="HTTP 403: insufficient permissions")


# -- transport -------------------------------------------------------------


class Boom:
    """An httpx client whose every call fails to connect."""

    def get(self, *a, **kw):
        raise httpx.ConnectError("connection refused")

    def post(self, *a, **kw):
        raise httpx.ConnectError("connection refused")


class CannedResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode()

    def json(self):
        return {"responseHeader": {"status": 0}, "response": {"numFound": 0, "docs": []}}


class Canned:
    def __init__(self, resp: CannedResponse) -> None:
        self.resp = resp
        self.paths: list[str] = []

    def get(self, path, **kw):
        self.paths.append(path)
        return self.resp

    def post(self, path, **kw):
        self.paths.append(path)
        return self.resp


def test_a_solr_that_is_down_becomes_a_solr_error(monkeypatch):
    """REGRESSION: an unreachable Solr raised httpx.ConnectError straight past
    the app's 503 handler, so the one case the handler existed for was a 500."""
    client = SolrClient("http://127.0.0.1:9/solr/x")
    monkeypatch.setattr(client, "_client", Boom())

    with pytest.raises(SolrError, match="cannot reach Solr"):
        client._get("select")
    with pytest.raises(SolrError, match="cannot reach Solr"):
        client._post("update", {})


def test_alive_reports_false_rather_than_raising_when_unreachable(monkeypatch):
    """/healthz has to be able to report the outage, not fail with it."""
    client = SolrClient("http://127.0.0.1:9/solr/x")
    monkeypatch.setattr(client, "_client", Boom())
    assert client.alive() is False


def test_an_error_response_keeps_the_backends_own_message(monkeypatch):
    """Solr's message is what made the facet 400 diagnosable. Losing it turns a
    one-line fix into a guessing game."""
    client = SolrClient("http://fake/solr/x")
    monkeypatch.setattr(
        client, "_client", Canned(CannedResponse(400, '{"error":{"msg":"undefined field x_facet"}}'))
    )
    with pytest.raises(SolrError) as exc:
        client._get("select", q="*:*")
    assert "HTTP 400" in str(exc.value)
    assert "undefined field x_facet" in str(exc.value)


# -- indexing --------------------------------------------------------------


def test_indexing_nothing_does_not_call_solr(config, monkeypatch):
    client = SolrClient("http://fake/solr/x")
    sent: list = []
    monkeypatch.setattr(client, "_post", lambda *a, **kw: sent.append(a) or {})
    assert client.index([]) == 0
    assert sent == []


def test_indexing_returns_how_many_were_sent_and_can_commit(config, monkeypatch):
    client = SolrClient("http://fake/solr/x")
    sent: list = []

    def fake_post(path, payload, ctype="application/json"):
        sent.append((path, payload))
        return {"responseHeader": {"status": 0}}

    monkeypatch.setattr(client, "_post", fake_post)
    docs = [{"id": "Object-1"}, {"id": "Object-2"}]
    assert client.index(docs) == 2
    assert sent[0][0] == "update"
    assert client.index(docs, commit=True) == 2
    assert sent[1][0] == "update?commit=true"


def test_a_non_zero_update_status_is_an_error(monkeypatch):
    client = SolrClient("http://fake/solr/x")
    monkeypatch.setattr(
        client, "_post", lambda *a, **kw: {"responseHeader": {"status": 400}}
    )
    with pytest.raises(SolrError, match="update failed"):
        client.index([{"id": "x"}])


def test_delete_all_clears_everything(monkeypatch):
    client = SolrClient("http://fake/solr/x")
    sent: list = []
    monkeypatch.setattr(client, "_post", lambda path, payload, ctype="application/json": sent.append(payload) or {})
    client.delete_all()
    assert sent == [{"delete": {"query": "*:*"}}]


def test_count_reads_the_num_found(monkeypatch):
    client = SolrClient("http://fake/solr/x")
    monkeypatch.setattr(
        client, "_get", lambda path, **kw: {"response": {"numFound": 181203, "docs": []}}
    )
    assert client.count() == 181203


# -- escaping ---------------------------------------------------------------


def test_a_facet_value_with_syntax_characters_is_escaped():
    """A facet value is data: a title with a colon must not become a fielded query."""
    assert _escape("Kunst: Grafik") == "Kunst\\: Grafik"
    assert _escape("a/b") == "a\\/b"
    assert _escape("normal value") == "normal value"
